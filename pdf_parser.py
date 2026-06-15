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


def _extract_pages(pdf_bytes: bytes) -> list[str]:
    """Return list of per-page text strings."""
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
            pages.append(text.strip())
    return pages


def _chunk_pages(pages: list[str]) -> list[str]:
    """Group pages into chunks that stay under CHUNK_CHARS."""
    chunks, current, current_len = [], [], 0
    for text in pages:
        if current and current_len + len(text) > CHUNK_CHARS:
            chunks.append("\n\n".join(current))
            current, current_len = [], 0
        current.append(text)
        current_len += len(text)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _parse_chunk(client: OpenAI, chunk_text: str, supplier_name: str) -> list[dict]:
    """Send one chunk of catalog text to DeepSeek and return parsed items."""
    prompt = f"""Extract every product from this price catalog for supplier "{supplier_name}".

Return ONLY a valid JSON array. Each element:
{{"code":"<item code or empty string>","description":"<full product name>","unit":"<Nos/Mtr/Set/Box/Kg/Pcs etc>","base_price":<number>}}

Rules:
- Skip headers, footers, section titles, and rows with no price.
- If price is a range, use the lower value.
- base_price must be a plain number — no ₹ or commas.
- No markdown, no explanation, just the JSON array.

Catalog text:
{chunk_text}"""

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=8192,
                temperature=0,
                timeout=60,
            )
            raw = response.choices[0].message.content.strip()
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
        return []

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
    return cleaned


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
        if not chunk.strip():
            continue
        items = _parse_chunk(client, chunk, supplier_name)
        all_items.extend(items)

    # Deduplicate by (description, base_price) keeping last occurrence
    seen: dict[tuple, dict] = {}
    for item in all_items:
        key = (item["description"].lower(), item["base_price"])
        seen[key] = item

    return list(seen.values())
