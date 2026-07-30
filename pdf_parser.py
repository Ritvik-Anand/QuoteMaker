import os
import json
import re
import time
import pdfplumber
import io
from openai import OpenAI

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"
CHUNK_CHARS = 20000  # characters per DeepSeek call
MAX_RETRIES = 3


def _clean_cell(v) -> str:
    return re.sub(r"\s+", " ", (v or "")).strip()


def _table_to_text(table: list) -> str:
    """
    Render a pdfplumber table (list of rows of cells) as a clean pipe-delimited
    grid. Plain page.extract_text() interleaves multi-column price tables badly
    (e.g. catalogs with separate FR/HRFR/FRLSH/HFFR price columns per row) —
    extract_tables() keeps each cell in its real column, which the LLM needs to
    tell price columns apart correctly.
    """
    if not table:
        return ""
    ncols = max(len(r) for r in table)
    rows = [[_clean_cell(v) for v in (list(r) + [""] * (ncols - len(r)))] for r in table]

    # Drop columns that are empty on every row (merged-cell artifacts).
    keep = [c for c in range(ncols) if any(rows[r][c] for r in range(len(rows)))]
    if not keep:
        return ""
    rows = [[r[c] for c in keep] for r in rows]

    # Header rows = leading rows that are mostly non-numeric; forward-fill
    # blanks within each header row so merged group headers (e.g. a brand
    # name spanning two rate sub-columns) repeat under every sub-column.
    def numeric_ratio(row):
        vals = [v for v in row if v]
        if not vals:
            return 0
        return sum(1 for v in vals if re.match(r"^[\d.,/]+$", v)) / len(vals)

    header_end = 1
    for i, r in enumerate(rows):
        if numeric_ratio(r) > 0.5:
            header_end = i
            break
    else:
        header_end = 1

    header_rows, data_rows = rows[:header_end], rows[header_end:]

    # Full-width caption/title rows (only one cell populated, e.g. the product
    # description sentence or company address block) aren't column headers —
    # forward-filling them would smear that one cell across every column.
    # Keep them as standalone context lines instead.
    captions = [r[0] for r in header_rows if r and sum(1 for v in r if v) <= 1 and r[0]]
    real_header_rows = [r for r in header_rows if sum(1 for v in r if v) > 1]

    filled_headers = []
    for r in real_header_rows:
        filled, last = [], ""
        for v in r:
            last = v or last
            filled.append(last)
        filled_headers.append(filled)

    compound_headers = []
    for c in range(len(rows[0])):
        parts = []
        for r in filled_headers:
            v = r[c] if c < len(r) else ""
            if v and v not in parts:
                parts.append(v)
        compound_headers.append(" / ".join(parts) if parts else f"col{c+1}")

    lines = list(dict.fromkeys(captions))  # dedupe, preserve order
    lines.append(" | ".join(compound_headers))
    for r in data_rows:
        if any(r):
            lines.append(" | ".join(r))
    return "\n".join(lines)


def _extract_table_grid(page, t) -> list:
    """
    pdfplumber's table.extract() returns a cell of None (not "") when it
    can't confidently bound that region — this happens on catalogs whose
    ruling lines only span the header row, leaving data rows with no line
    under them. pdfplumber then infers a truncated row bbox from whichever
    line it did find, silently dropping whole columns (often the price
    column) even though the text is sitting right there on the page. Patch
    just those None cells back in by cropping the reference column's x-range
    against that row's own (still-accurate) y-range and reading the text
    directly — a no-op for normal tables where nothing came back None.
    """
    raw = t.extract()
    if not raw:
        return raw
    rows_meta = t.rows
    ref_cells = max(
        (r.cells for r in rows_meta if r.cells),
        key=lambda cells: sum(1 for c in cells if c),
        default=None,
    )
    if not ref_cells:
        return raw

    patched = [list(row) for row in raw]
    for ri, row_obj in enumerate(rows_meta):
        if ri >= len(patched):
            break
        row_top, row_bottom = row_obj.bbox[1], row_obj.bbox[3]
        for ci, cell in enumerate(row_obj.cells):
            if cell is not None or ci >= len(ref_cells) or not ref_cells[ci]:
                continue
            x0, x1 = ref_cells[ci][0], ref_cells[ci][2]
            try:
                text = page.crop((x0, row_top, x1, row_bottom)).extract_text(x_tolerance=2, y_tolerance=2)
            except Exception:
                text = None
            if text and ci < len(patched[ri]):
                patched[ri][ci] = text.replace("\n", " ").strip()
    return patched


# Matches a standalone heading line like "HT Single Core Power Cables..." or
# "LT Power Cables - Copper Conductor" — deliberately requires "Cable"/"Power"
# nearby so it doesn't fire on unrelated text that happens to start with the
# letters HT/LT.
_VOLTAGE_RE = re.compile(r"^(HT|LT)\b.*\b(Cables?|Power)\b")


def _extract_page_text(page, voltage_state: list) -> str:
    """
    Render one page as plain text with table regions swapped out for clean
    grids, in top-to-bottom document order. Many catalogs put the variant name
    only in a heading ABOVE a single-variant table (not inside the table at
    all) — e.g. "Heat Resistant Flame Retardant HRFR..." followed by a plain
    size/code/price table. Using only extract_tables() would silently drop
    that heading; using only extract_text() scrambles multi-column tables. So
    we keep whichever text falls outside every table's bounding box (headings,
    captions) and splice in the clean table grid where the table itself sits.

    `voltage_state` is a 1-element list holding the current HT/LT class across
    the whole document (mutated in place so it persists across pages). Some
    cable catalogs switch between HT and LT sections *mid-page*, and asking
    the model to track that itself across a long combined chunk is exactly
    how a whole HT section ends up mislabeled LT (or vice versa) — a single
    wrong voltage class is a very expensive mistake, so it's tagged onto
    every table deterministically here instead of trusted to the model's memory.
    """
    tables = page.find_tables()
    if not tables:
        text = (page.extract_text(x_tolerance=2, y_tolerance=2) or "").strip()
        for line in text.split("\n"):
            m = _VOLTAGE_RE.match(line.strip())
            if m:
                voltage_state[0] = m.group(1)
        return text

    def in_any_table(word):
        cx, cy = (word["x0"] + word["x1"]) / 2, (word["top"] + word["bottom"]) / 2
        for t in tables:
            x0, top, x1, bottom = t.bbox
            if x0 <= cx <= x1 and top <= cy <= bottom:
                return True
        return False

    outside_words = sorted(
        (w for w in page.extract_words(x_tolerance=2, y_tolerance=2) if not in_any_table(w)),
        key=lambda w: (w["top"], w["x0"]),
    )

    lines, current_top, current = [], None, []
    for w in outside_words:
        if current_top is not None and abs(w["top"] - current_top) > 3:
            lines.append((current_top, current))
            current = []
        current.append(w)
        current_top = w["top"]
    if current:
        lines.append((current_top, current))

    blocks = [(top, " ".join(w["text"] for w in ws).strip()) for top, ws in lines]
    blocks = [(top, text) for top, text in blocks if text]

    for t in tables:
        grid = _table_to_text(_extract_table_grid(page, t))
        if grid:
            blocks.append((t.bbox[1], grid))

    blocks.sort(key=lambda b: b[0])

    # Walk the page top-to-bottom (its true reading order, now that headings
    # and table grids are merged and sorted together) tagging every table
    # with whatever HT/LT heading most recently preceded it.
    tagged = []
    for _, text in blocks:
        is_table_grid = "|" in text
        if not is_table_grid:
            m = _VOLTAGE_RE.match(text.strip())
            if m:
                voltage_state[0] = m.group(1)
            tagged.append(text)
        elif voltage_state[0]:
            tagged.append(f"[Voltage class for every row in this table: {voltage_state[0]}]\n{text}")
        else:
            tagged.append(text)
    return "\n".join(tagged)


def _extract_pages(pdf_bytes: bytes) -> list[str]:
    """Return list of per-page text strings, preferring structured tables."""
    voltage_state = [None]  # shared/mutated across pages — see _extract_page_text
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [_extract_page_text(page, voltage_state) for page in pdf.pages]


def _chunk_pages(pages: list[str]) -> list[list[str]]:
    """Group pages into chunks that stay under CHUNK_CHARS. Each chunk keeps
    its page texts as a list (rather than pre-joining them) so that if the
    model's response gets truncated, the retry logic can split cleanly on
    page boundaries instead of cutting a table in half mid-row."""
    chunks, current, current_len = [], [], 0
    for text in pages:
        if current and current_len + len(text) > CHUNK_CHARS:
            chunks.append(current)
            current, current_len = [], 0
        current.append(text)
        current_len += len(text)
    if current:
        chunks.append(current)
    return chunks


def _parse_chunk(client: OpenAI, chunk_text: str, supplier_name: str) -> list[dict]:
    """Send one chunk of catalog text to DeepSeek and return parsed items."""
    prompt = f"""Extract every product from this price catalog for supplier "{supplier_name}".
The catalog text below may be given as pipe-delimited table rows ("col1 | col2 | ...")
extracted directly from the PDF's table structure — each row is one product size/spec,
and each column is labeled by its header in the first line of that table.

Return ONLY a valid JSON array. Each element:
{{"code":"<item code or empty string>","description":"<full product name>","unit":"<Nos/Mtr/Set/Box/Kg/Pcs etc>","base_price":<number>}}

Rules:
- Skip headers, footers, section titles, and rows with no price.
- If price is a range, use the lower value.
- base_price must be a plain number — no ₹ or commas.
- No markdown, no explanation, just the JSON array.

Multi-variant tables (IMPORTANT):
Some rows have SEVERAL separate price columns for different product variants of the
same size — e.g. brand names tied to a fire-retardant grade (HOMECAB=FR, CONFLAME=FRLSH,
BANFIRE=ZHFR/HFFR) or plain variant columns like FR / HRFR / FRLSH / HFFR (ZHFR).
Do NOT merge these into one item or pick just one column — create a SEPARATE item for
EVERY variant column that has a price, and append the variant name to the description,
e.g. "0.50 SQ.MM Single Core Wire - FR", "0.50 SQ.MM Single Core Wire - FRLSH".

Other catalogs instead give EACH variant its own separate table, one after another,
with the variant/product name ONLY in a plain-text heading line sitting directly above
that table (not inside the table itself) — e.g. a line like "Heat Resistant Flame
Retardant HRFR PVC Insulated Industrial Cables" immediately followed by a plain
size/code/price grid with no variant column at all. In that case, every row of that
table belongs to the variant named in the heading above it — append that variant name
(e.g. "FR", "HRFR", "FR-LSH", "HFFR") to the description of every item from that table,
even though the table's own columns never mention it. Watch for a new heading each time
the table structure resets — it means the variant has changed for all following rows
until the next heading.

Same variant, different pack length (IMPORTANT):
Sometimes the SAME variant (e.g. "FR" or "HRFR") has more than one table at different
pack lengths for overlapping sizes — the signal is the price column's own header (e.g.
"List Price per 90 m" vs "List Price per 1000 m"), or a standalone heading nearby (e.g.
"180 m" / "200 m PROJECT PACKAGING"). These are different sellable pack sizes with
different per-metre rates, not duplicates — a small-carton 90m pack is genuinely priced
differently per metre than a bulk 1000m reel. Still compute base_price as the per-Mtr
rate for each, but append the pack length so they stay distinguishable, e.g.
"0.75 SQ.MM Wire - FR - 90m Pack" vs "0.75 SQ.MM Wire - FR - 1000m Pack". Only add this
suffix when the same variant+size genuinely repeats across more than one pack-length
table (check every table, including ones further down the page or on later pages, for
the same variant name before deciding a size is unique) — don't add it when a size only
appears once for that variant.

Voltage class / LT vs HT (CRITICAL — this changes the product and the price,
never guess or drop it):
Cables are frequently split into completely separate sections or tables by
voltage class — LT (Low Tension, e.g. 1.1 kV) vs HT (High Tension, e.g. 3.3 kV,
6.6 kV, 11 kV, 22 kV, 33 kV). A single catalog routinely switches between HT
and LT sections more than once, sometimes mid-page, so do NOT assume the
class from earlier in the document still applies — re-derive it fresh for
every table. Some tables are preceded by a line we've already tagged for you
like "[Voltage class for every row in this table: HT]" immediately above the
table grid — that tag is authoritative for every row in that specific table
and OVERRIDES anything you inferred from earlier context; if a later table
has a different tag (or none), the class has changed (or is unstated) and
you must not keep reusing the earlier one. Where no such tag is present,
fall back to the heading-inheritance rule used for variant sections above
(every row belongs to whatever heading sits directly over its table). Always
carry the voltage class and/or exact kV rating through into every matching
item's description — e.g. "70 sq. mm HT Single Core Power Cable - 6.6 kV
(E)" vs "70 sq. mm HT Single Core Power Cable - 11 kV (E)" are different
products at different prices and must both appear as separate items, never
merged, never left ambiguous, and never silently carried over from a
different table's class. If a size/spec appears at multiple voltage ratings,
every one of them is a separate item.

Rate-per-Mtr vs rate-per-Coil:
If a variant has BOTH a "rate per coil"/"rate per 100 mtrs" style column AND a
"rate per mtr" column, use ONLY the per-metre rate as base_price with unit "Mtr"
(ignore the coil/100-mtr rate for that variant). If a table's rates are explicitly
labeled "per 100 Mtrs" and there is no separate per-Mtr column, divide the value by
100 to get the per-metre price, and still use unit "Mtr". Only fall back to a
coil-based unit (e.g. unit "Coil") when no per-metre price can be derived at all.

Catalog text:
{chunk_text}"""

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=16000,
                temperature=0,
                timeout=120,
            )
            raw = response.choices[0].message.content.strip()
            truncated = response.choices[0].finish_reason == "length"
            break
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)  # 1s, 2s backoff
            continue
    else:
        raise ConnectionError(
            f"Could not reach DeepSeek after {MAX_RETRIES} attempts. "
            f"Please check your internet connection and try again. "
            f"(Last error: {last_error})"
        )

    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    raw = _recover_json(raw)

    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return [], truncated

    cleaned = []
    for item in items:
        try:
            price = float(str(item.get("base_price", 0)).replace(",", ""))
            desc = str(item.get("description", "")).strip()
            if desc and price > 0:
                cleaned.append({
                    "code": str(item.get("code", "") or "").strip(),
                    "description": desc,
                    "unit": str(item.get("unit", "Nos") or "Nos").strip() or "Nos",
                    "base_price": price,
                })
        except (ValueError, TypeError):
            continue
    return cleaned, truncated


def _parse_pages_recursive(client: OpenAI, page_texts: list[str], supplier_name: str, depth: int = 0) -> list[dict]:
    """
    Parse a group of pages, automatically retrying with a smaller group
    whenever the model's response gets cut off (finish_reason "length")
    instead of silently keeping only the partial result — a dense table
    (e.g. many core-count x construction-type combinations) can produce far
    more output JSON than input text, and a catalog that's simply too big
    for one completion must never be allowed to quietly lose data past
    whatever it managed to fit before running out of tokens.
    """
    items, truncated = _parse_chunk(client, "\n\n".join(page_texts), supplier_name)
    if not truncated or depth >= 6:
        return items

    if len(page_texts) > 1:
        mid = max(len(page_texts) // 2, 1)
        first, second = page_texts[:mid], page_texts[mid:]
    else:
        # A single page's own table was too dense to complete — fall back to
        # splitting its raw text roughly in half on a line boundary. Rare in
        # practice (most individual pages fit comfortably), but still better
        # than accepting a silently-truncated result.
        text = page_texts[0]
        if len(text) < 800:
            return items
        split_at = text.rfind("\n", 0, len(text) // 2) or len(text) // 2
        first, second = [text[:split_at]], [text[split_at:]]

    return (
        _parse_pages_recursive(client, first, supplier_name, depth + 1)
        + _parse_pages_recursive(client, second, supplier_name, depth + 1)
    )


def _recover_json(raw: str) -> str:
    """
    If the JSON array is truncated, close it so we can still parse
    all the complete objects that came through.
    """
    raw = raw.strip()
    if not raw.startswith("["):
        # Try to find the start of the array
        idx = raw.find("[")
        if idx == -1:
            return "[]"
        raw = raw[idx:]

    # Already valid?
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        pass

    # Strip any trailing incomplete object then close the array
    # Find last complete object: scan for last '}' before a ',' or end
    last_close = raw.rfind("}")
    if last_close == -1:
        return "[]"
    raw = raw[: last_close + 1] + "]"
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        return "[]"


def parse_catalog_pdf(pdf_bytes: bytes, supplier_name: str) -> list[dict]:
    """
    Extract text from catalog PDF page by page, send to DeepSeek in chunks,
    and merge all results. Returns list of {code, description, unit, base_price}.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY environment variable is not set.")

    pages = _extract_pages(pdf_bytes)
    total_text = "\n".join(pages).strip()
    if not total_text:
        raise ValueError(
            "Could not extract text from this PDF. "
            "It may be a scanned/image PDF. Please use a digital (text-selectable) PDF."
        )

    chunks = _chunk_pages(pages)
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)

    all_items: list[dict] = []
    for chunk in chunks:
        if not any(p.strip() for p in chunk):
            continue
        items = _parse_pages_recursive(client, chunk, supplier_name)
        all_items.extend(items)

    # Deduplicate by (description, base_price) keeping last occurrence
    seen: dict[tuple, dict] = {}
    for item in all_items:
        key = (item["description"].lower(), item["base_price"])
        seen[key] = item

    return list(seen.values())
