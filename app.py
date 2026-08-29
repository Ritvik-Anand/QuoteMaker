import os
import json
import re
import secrets
import requests
import difflib
from datetime import date
from pathlib import Path
from flask import (
    Flask, request, jsonify, render_template,
    send_file, session, redirect, url_for, flash
)
import io

# Load .env from project root (local dev only)
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from database import init_db, get_db
from pdf_parser import parse_catalog_pdf
from export import generate_pdf, generate_excel
from auth import (
    login_required, admin_required, marker_required, payroll_viewer_required,
    verify_user, create_user, change_password,
)
import calendar

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Vercel serverless functions hard-cap request bodies at ~4.5MB, so catalog
# PDFs above that go browser -> Vercel Blob -> here (bypassing that limit)
# instead of the normal multipart upload. See upload_catalog_blob() below.
BLOB_API_BASE = "https://blob.vercel-storage.com"


def _next_quote_number():
    conn = get_db()
    row = conn.execute(
        "SELECT quote_number FROM quotations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return f"BM-{date.today().year}-001"
    last = row["quote_number"]
    parts = last.rsplit("-", 1)
    try:
        num = int(parts[-1]) + 1
        return f"{parts[0]}-{num:03d}"
    except ValueError:
        return f"BM-{date.today().year}-001"


# ── Auth pages ─────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET"])
def login_page():
    if session.get("user_id"):
        return redirect(request.args.get("next") or url_for("index"))
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login_post():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = verify_user(username, password)
    if not user:
        return render_template("login.html", error="Invalid username or password.")
    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["is_admin"] = bool(user["is_admin"])
    session["role"] = user.get("role") or "staff"
    next_url = request.args.get("next") or url_for("index")
    return redirect(next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password_page():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        user = verify_user(session["username"], current)
        if not user:
            return render_template("change_password.html", error="Current password is wrong.")
        if len(new_pw) < 6:
            return render_template("change_password.html", error="New password must be at least 6 characters.")
        if new_pw != confirm:
            return render_template("change_password.html", error="Passwords do not match.")
        change_password(session["user_id"], new_pw)
        return render_template("change_password.html", success="Password changed successfully.")
    return render_template("change_password.html")


# ── Admin ──────────────────────────────────────────────────────────────────────

@app.route("/admin/users")
@admin_required
def admin_users_page():
    return render_template("admin_users.html")


_VALID_ROLES = {"staff", "ops"}


@app.route("/api/admin/users", methods=["GET"])
@admin_required
def api_list_users():
    conn = get_db()
    rows = conn.execute(
        "SELECT u.id, u.username, u.is_admin, u.role, u.created_at, "
        "(SELECT COUNT(*) FROM quotations q WHERE q.created_by = u.username) as quote_count "
        "FROM users u ORDER BY u.id"
    ).fetchall()
    conn.close()
    return jsonify(rows)


@app.route("/api/admin/users", methods=["POST"])
@admin_required
def api_create_user():
    data = request.json
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    is_admin = bool(data.get("is_admin", False))
    role = (data.get("role") or "staff").strip()
    if role not in _VALID_ROLES:
        role = "staff"
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    try:
        create_user(username, password, is_admin, role)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/admin/users/<int:uid>/role", methods=["PUT"])
@admin_required
def api_set_user_role(uid):
    data = request.json
    role = (data.get("role") or "staff").strip()
    if role not in _VALID_ROLES:
        return jsonify({"error": "Invalid role"}), 400
    conn = get_db()
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/users/<int:uid>", methods=["DELETE"])
@admin_required
def api_delete_user(uid):
    if uid == session["user_id"]:
        return jsonify({"error": "Cannot delete your own account"}), 400
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/users/<int:uid>/reset-password", methods=["POST"])
@admin_required
def api_reset_password(uid):
    data = request.json
    new_pw = (data.get("password") or "").strip()
    if len(new_pw) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    change_password(uid, new_pw)
    return jsonify({"ok": True})


# ── Pages ──────────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/catalogs")
@admin_required
def catalogs_page():
    return render_template("catalogs.html")


@app.route("/quotation/new")
@login_required
def new_quotation_page():
    return render_template("quotation.html")


@app.route("/quotation/<int:qid>/edit")
@login_required
def edit_quotation_page(qid):
    return render_template("quotation.html", qid=qid)


@app.route("/history")
@login_required
def history_page():
    return render_template("history.html")


@app.route("/projects")
@login_required
def projects_page():
    return render_template("projects.html")


@app.route("/projects/new")
@login_required
def new_project_page():
    return render_template("project_edit.html")


@app.route("/projects/<int:pid>/edit")
@login_required
def edit_project_page(pid):
    return render_template("project_edit.html", pid=pid)


@app.route("/orders")
@login_required
def orders_page():
    return render_template("orders.html")


@app.route("/orders/<int:oid>")
@login_required
def order_detail_page(oid):
    return render_template("order_detail.html", oid=oid)


@app.route("/attendance")
@marker_required
def attendance_page():
    return render_template("attendance.html")


@app.route("/payroll")
@payroll_viewer_required
def payroll_page():
    return render_template("payroll.html")


@app.route("/admin/employees")
@admin_required
def admin_employees_page():
    return render_template("admin_employees.html")


# ── Suppliers API ──────────────────────────────────────────────────────────────

@app.route("/api/suppliers", methods=["GET"])
@login_required
def get_suppliers():
    conn = get_db()
    rows = conn.execute(
        "SELECT s.id, s.name, s.created_at, COUNT(i.id) as item_count "
        "FROM suppliers s LEFT JOIN items i ON i.supplier_id = s.id "
        "GROUP BY s.id ORDER BY s.name"
    ).fetchall()
    conn.close()
    return jsonify(rows)


@app.route("/api/suppliers", methods=["POST"])
@admin_required
def create_supplier():
    data = request.json
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    conn = get_db()
    try:
        sid = conn.insert("INSERT INTO suppliers (name) VALUES (?)", (name,))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 400
    conn.close()
    return jsonify({"id": sid, "name": name})


@app.route("/api/suppliers/<int:sid>", methods=["DELETE"])
@admin_required
def delete_supplier(sid):
    conn = get_db()
    conn.execute("DELETE FROM suppliers WHERE id = ?", (sid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Catalog API ────────────────────────────────────────────────────────────────

@app.route("/api/suppliers/<int:sid>/catalogs", methods=["GET"])
@admin_required
def get_catalogs(sid):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM catalogs WHERE supplier_id = ? ORDER BY uploaded_at DESC", (sid,)
    ).fetchall()
    conn.close()
    return jsonify(rows)


def _ingest_catalog(sid, supplier_name, pdf_bytes, catalog_name, catalog_id):
    """Parse a catalog PDF and upsert its items. Returns a (body, status) jsonify-able tuple."""
    try:
        items = parse_catalog_pdf(pdf_bytes, supplier_name)
    except Exception as e:
        return {"error": f"Failed to parse PDF: {str(e)}"}, 500

    if not items:
        return {"error": "No items extracted from PDF"}, 400

    conn = get_db()
    if catalog_id:
        conn.execute("DELETE FROM items WHERE catalog_id = ?", (catalog_id,))
        conn.execute(
            "UPDATE catalogs SET name=?, item_count=?, uploaded_at=CURRENT_TIMESTAMP WHERE id=?",
            (catalog_name, len(items), catalog_id)
        )
    else:
        catalog_id = conn.insert(
            "INSERT INTO catalogs (supplier_id, name, item_count) VALUES (?,?,?)",
            (sid, catalog_name, len(items))
        )

    conn.executemany(
        "INSERT INTO items (supplier_id, catalog_id, code, description, unit, base_price) VALUES (?,?,?,?,?,?)",
        [(sid, catalog_id, i["code"], i["description"], i["unit"], i["base_price"]) for i in items]
    )
    conn.commit()
    conn.close()
    return {"imported": len(items), "catalog_id": catalog_id, "catalog_name": catalog_name}, 200


@app.route("/api/suppliers/<int:sid>/upload", methods=["POST"])
@admin_required
def upload_catalog(sid):
    conn = get_db()
    supplier = conn.execute("SELECT * FROM suppliers WHERE id = ?", (sid,)).fetchone()
    conn.close()
    if not supplier:
        return jsonify({"error": "Supplier not found"}), 404

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    catalog_name = request.form.get("catalog_name", "").strip() or f.filename
    catalog_id = request.form.get("catalog_id", "").strip()

    body, status = _ingest_catalog(sid, supplier["name"], f.read(), catalog_name, catalog_id)
    return jsonify(body), status


@app.route("/api/blob/upload-token", methods=["GET"])
@admin_required
def blob_upload_token():
    token = os.environ.get("BLOB_READ_WRITE_TOKEN")
    if not token:
        return jsonify({"error": "Blob storage is not configured"}), 500
    return jsonify({"token": token, "api_base": BLOB_API_BASE})


@app.route("/api/suppliers/<int:sid>/upload-blob", methods=["POST"])
@admin_required
def upload_catalog_blob(sid):
    """Companion to upload_catalog() for large PDFs: the browser has already
    PUT the file directly to Vercel Blob (bypassing the ~4.5MB serverless
    request-body limit); we fetch it from there, parse it, and clean up."""
    conn = get_db()
    supplier = conn.execute("SELECT * FROM suppliers WHERE id = ?", (sid,)).fetchone()
    conn.close()
    if not supplier:
        return jsonify({"error": "Supplier not found"}), 404

    data = request.get_json(force=True) or {}
    blob_url = str(data.get("blob_url") or "").strip()
    if not blob_url:
        return jsonify({"error": "No file provided"}), 400

    catalog_name = str(data.get("catalog_name") or "").strip() or "Catalog"
    catalog_id = str(data.get("catalog_id") or "").strip()

    try:
        r = requests.get(blob_url, timeout=60)
        r.raise_for_status()
        pdf_bytes = r.content
    except Exception as e:
        return jsonify({"error": f"Failed to retrieve uploaded file: {str(e)}"}), 500

    body, status = _ingest_catalog(sid, supplier["name"], pdf_bytes, catalog_name, catalog_id)

    token = os.environ.get("BLOB_READ_WRITE_TOKEN")
    if token:
        try:
            requests.post(
                f"{BLOB_API_BASE}/delete",
                headers={"authorization": f"Bearer {token}", "content-type": "application/json", "x-api-version": "7"},
                json={"urls": [blob_url]},
                timeout=15,
            )
        except Exception:
            pass  # best-effort cleanup; leftover blobs don't affect app data

    return jsonify(body), status


@app.route("/api/catalogs/<int:cid>", methods=["DELETE"])
@admin_required
def delete_catalog(cid):
    conn = get_db()
    conn.execute("DELETE FROM catalogs WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Items API ──────────────────────────────────────────────────────────────────

def _normalize_text(s: str) -> str:
    """Lowercase and strip punctuation/whitespace so 'SQ.MM', 'Sq mm' and
    'sqmm' all compare equal — catalogs are wildly inconsistent about this."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _normalize_sql(col: str) -> str:
    """SQL equivalent of _normalize_text() for a column/expression, so DB-side
    filtering stays consistent with how query tokens are normalized in Python."""
    expr = f"LOWER({col})"
    for ch in (".", "-", "/", ",", " ", "''", '"', "(", ")"):
        expr = f"REPLACE({expr}, '{ch}', '')"
    return expr


_NUM_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)?$")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _is_numeric_token(tok: str) -> bool:
    return bool(_NUM_TOKEN_RE.match(tok))


def _extract_numbers(s: str) -> list:
    """All standalone numbers appearing in s, e.g. '1.5 sq mm, 150m coil' -> [1.5, 150]."""
    return [float(n) for n in _NUM_RE.findall(s or "")]


def _numbers_match(query_values, target_text) -> bool:
    """Every numeric token in the query must equal (exactly, not as a substring)
    one of the numbers in the target text. This stops '1.5' from matching '150'
    just because '15' happens to be a substring of '150' — a real mismatch that
    would misquote a completely different cable size."""
    if not query_values:
        return True
    target_nums = _extract_numbers(target_text)
    return all(any(abs(v - n) < 1e-9 for n in target_nums) for v in query_values)


_ITEM_SELECT = (
    "SELECT i.id, i.code, i.description, i.unit, i.base_price, i.catalog_id, "
    "s.id as supplier_id, s.name as supplier_name, c.name as catalog_name "
    "FROM items i JOIN suppliers s ON s.id = i.supplier_id "
    "LEFT JOIN catalogs c ON c.id = i.catalog_id "
)


@app.route("/api/items", methods=["GET"])
@login_required
def search_items():
    q = request.args.get("q", "").strip()
    supplier_id = request.args.get("supplier_id")
    limit = int(request.args.get("limit", 50))

    supplier_where, supplier_params = [], []
    if supplier_id:
        supplier_where.append("i.supplier_id = ?")
        supplier_params.append(supplier_id)

    # Primary search: every whitespace-separated token must appear somewhere
    # in the description or code, independent of order, spacing, or
    # punctuation — "sqmm fr" now matches "SQ.MM ... - FR" even though
    # neither the spacing nor punctuation lines up exactly.
    #
    # Numbers are handled separately from text: a numeric token (e.g. "1.5")
    # must match a *whole* number in the target, not just a substring of a
    # longer one — otherwise "1.5 sq mm" would wrongly match "150 sq mm"
    # because the digits "15" are a plain substring of "150". Getting a
    # cable size wrong here is exactly the kind of mistake that costs real
    # money, so numbers get exact-value comparison, not fuzzy substring.
    raw_tokens = q.split()
    numeric_values = [float(t) for t in raw_tokens if _is_numeric_token(t)]
    text_tokens = [_normalize_text(t) for t in raw_tokens if not _is_numeric_token(t)]
    text_tokens = [t for t in text_tokens if t]

    where, params = list(supplier_where), list(supplier_params)
    if text_tokens:
        desc_norm, code_norm = _normalize_sql("i.description"), _normalize_sql("i.code")
        for tok in text_tokens:
            where.append(f"({desc_norm} LIKE ? OR {code_norm} LIKE ?)")
            params += [f"%{tok}%", f"%{tok}%"]

    sql = _ITEM_SELECT
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY i.description "
    # Numeric tokens are checked exactly in Python below, so when any are
    # present fetch a wider pool before trimming to `limit`.
    sql += "LIMIT ?"
    sql_limit = 2000 if numeric_values else limit

    conn = get_db()
    rows = conn.execute(sql, params + [sql_limit]).fetchall()

    if numeric_values:
        rows = [r for r in rows if _numbers_match(numeric_values, f"{r['description']} {r.get('code') or ''}")]
    rows = rows[:limit]

    # Fallback: the strict token match found nothing, but the query is
    # substantial enough to be a genuine near-miss (typo, abbreviation,
    # mis-remembered wording) rather than gibberish — rank a broader
    # candidate pool by per-token fuzzy similarity instead of leaving the
    # user at a dead end. Matches word-by-word (not the whole string at
    # once) so a typo in one word doesn't get diluted by a long, otherwise
    # exact, description.
    if not rows and (text_tokens or numeric_values):
        cand_sql = _ITEM_SELECT
        if supplier_where:
            cand_sql += "WHERE " + " AND ".join(supplier_where) + " "
        cand_sql += "ORDER BY i.description LIMIT 5000"
        candidates = conn.execute(cand_sql, supplier_params).fetchall()

        scored = []
        for row in candidates:
            item = dict(row)
            target_text = f"{item['description']} {item.get('code') or ''}"
            # Numbers stay a hard, exact requirement even in the fuzzy
            # fallback — a typo-tolerant match on a cable size is how "1.5"
            # ends up quoting "150" by mistake.
            if not _numbers_match(numeric_values, target_text):
                continue
            if text_tokens:
                target_words = re.findall(r"[a-z0-9]+", target_text.lower())
                if not target_words:
                    continue
                token_scores = [
                    max((difflib.SequenceMatcher(None, tok, w).ratio() for w in target_words), default=0)
                    for tok in text_tokens
                ]
                avg_score = sum(token_scores) / len(token_scores)
                if avg_score >= 0.72 and min(token_scores) >= 0.5:
                    scored.append((avg_score, item))
            else:
                # Numeric-only query that had no exact match at the
                # strict-search size and did match here exactly.
                scored.append((1.0, item))
        scored.sort(key=lambda x: -x[0])
        rows = [item for _, item in scored[:limit]]

    conn.close()
    return jsonify(rows)


@app.route("/api/items/<int:iid>", methods=["PUT"])
@admin_required
def update_item(iid):
    data = request.json
    conn = get_db()
    conn.execute(
        "UPDATE items SET code=?, description=?, unit=?, base_price=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (data.get("code", ""), data["description"], data.get("unit", "Nos"), float(data["base_price"]), iid)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/items/<int:iid>", methods=["DELETE"])
@admin_required
def delete_item(iid):
    conn = get_db()
    conn.execute("DELETE FROM items WHERE id = ?", (iid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/suppliers/<int:sid>/items", methods=["GET"])
@admin_required
def get_supplier_items(sid):
    conn = get_db()
    rows = conn.execute(
        "SELECT i.*, c.name as catalog_name FROM items i "
        "LEFT JOIN catalogs c ON c.id = i.catalog_id "
        "WHERE i.supplier_id = ? ORDER BY i.description", (sid,)
    ).fetchall()
    conn.close()
    return jsonify(rows)


# ── Quotations API ─────────────────────────────────────────────────────────────

@app.route("/api/quotations", methods=["GET"])
@login_required
def list_quotations():
    conn = get_db()
    rows = conn.execute(
        "SELECT q.*, COUNT(qi.id) as item_count, "
        "COALESCE(SUM(qi.quantity * qi.final_price), 0) as subtotal "
        "FROM quotations q LEFT JOIN quotation_items qi ON qi.quotation_id = q.id "
        "GROUP BY q.id ORDER BY q.id DESC"
    ).fetchall()
    conn.close()
    return jsonify(rows)


@app.route("/api/quotations/next-number", methods=["GET"])
@login_required
def next_quote_number():
    return jsonify({"quote_number": _next_quote_number()})


@app.route("/api/quotations", methods=["POST"])
@login_required
def create_quotation():
    data = request.json
    conn = get_db()
    try:
        qid = conn.insert(
            "INSERT INTO quotations (quote_number, client_name, client_address, date, gst_rate, notes, tags, "
            "cash_discount, created_by, updated_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (data["quote_number"], data["client_name"], data.get("client_address", ""),
             data["date"], float(data.get("gst_rate", 18)), data.get("notes", ""), data.get("tags", ""),
             int(bool(data.get("cash_discount"))), session["username"], session["username"])
        )
        conn.commit()
        for idx, item in enumerate(data.get("items", [])):
            conn.execute(
                "INSERT INTO quotation_items "
                "(quotation_id, item_id, description, code, unit, quantity, base_price, "
                "adjustment_type, adjustment_value, final_price, sort_order, supplier_name) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (qid, item.get("item_id"), item["description"], item.get("code", ""),
                 item.get("unit", "Nos"), float(item["quantity"]), float(item["base_price"]),
                 item.get("adjustment_type", "none"), float(item.get("adjustment_value", 0)),
                 float(item["final_price"]), idx, item.get("supplier_name", ""))
            )
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 400
    conn.close()
    return jsonify({"id": qid})


@app.route("/api/quotations/<int:qid>", methods=["GET"])
@login_required
def get_quotation(qid):
    conn = get_db()
    q = conn.execute("SELECT * FROM quotations WHERE id = ?", (qid,)).fetchone()
    if not q:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    items = conn.execute(
        "SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY sort_order", (qid,)
    ).fetchall()
    order = conn.execute("SELECT id FROM orders WHERE quotation_id=?", (qid,)).fetchone()
    conn.close()
    return jsonify({"quotation": q, "items": items, "order_id": order["id"] if order else None})


@app.route("/api/quotations/<int:qid>", methods=["PUT"])
@login_required
def update_quotation(qid):
    data = request.json
    conn = get_db()
    try:
        conn.execute(
            "UPDATE quotations SET quote_number=?, client_name=?, client_address=?, date=?, "
            "gst_rate=?, notes=?, tags=?, cash_discount=?, updated_by=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (data["quote_number"], data["client_name"], data.get("client_address", ""), data["date"],
             float(data.get("gst_rate", 18)), data.get("notes", ""), data.get("tags", ""),
             int(bool(data.get("cash_discount"))), session["username"], qid)
        )
        conn.execute("DELETE FROM quotation_items WHERE quotation_id = ?", (qid,))
        for idx, item in enumerate(data.get("items", [])):
            conn.execute(
                "INSERT INTO quotation_items "
                "(quotation_id, item_id, description, code, unit, quantity, base_price, "
                "adjustment_type, adjustment_value, final_price, sort_order, supplier_name) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (qid, item.get("item_id"), item["description"], item.get("code", ""),
                 item.get("unit", "Nos"), float(item["quantity"]), float(item["base_price"]),
                 item.get("adjustment_type", "none"), float(item.get("adjustment_value", 0)),
                 float(item["final_price"]), idx, item.get("supplier_name", ""))
            )
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 400
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/quotations/<int:qid>", methods=["DELETE"])
@login_required
def delete_quotation(qid):
    conn = get_db()
    conn.execute("DELETE FROM quotations WHERE id = ?", (qid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/quotations/<int:qid>/accept", methods=["POST"])
@login_required
def accept_quotation(qid):
    conn = get_db()
    q = conn.execute("SELECT * FROM quotations WHERE id=?", (qid,)).fetchone()
    if not q:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    existing = conn.execute("SELECT id FROM orders WHERE quotation_id=?", (qid,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"order_id": existing["id"]})

    items = conn.execute(
        "SELECT * FROM quotation_items WHERE quotation_id=? ORDER BY sort_order", (qid,)
    ).fetchall()
    if not items:
        conn.close()
        return jsonify({"error": "Add at least one item before accepting this quotation"}), 400

    oid = conn.insert(
        "INSERT INTO orders (quotation_id, quote_number, client_name, client_address, notes, created_by) "
        "VALUES (?,?,?,?,?,?)",
        (qid, q["quote_number"], q["client_name"], q["client_address"] or "", q["notes"] or "",
         session["username"])
    )
    for idx, item in enumerate(items):
        conn.execute(
            "INSERT INTO order_items (order_id, description, code, unit, quantity, unit_price, sort_order) "
            "VALUES (?,?,?,?,?,?,?)",
            (oid, item["description"], item["code"] or "", item["unit"], item["quantity"],
             item["final_price"], idx)
        )
    conn.execute(
        "UPDATE quotations SET status='accepted', accepted_by=?, accepted_at=CURRENT_TIMESTAMP WHERE id=?",
        (session["username"], qid)
    )
    conn.commit()
    conn.close()
    return jsonify({"order_id": oid})


# ── Export API ─────────────────────────────────────────────────────────────────

@app.route("/api/quotations/<int:qid>/export/pdf")
@login_required
def export_pdf(qid):
    conn = get_db()
    q = conn.execute("SELECT * FROM quotations WHERE id = ?", (qid,)).fetchone()
    items = conn.execute(
        "SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY sort_order", (qid,)
    ).fetchall()
    conn.close()
    if not q:
        return jsonify({"error": "Not found"}), 404
    pdf_bytes = generate_pdf(q, items)
    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf",
                     as_attachment=True, download_name=f"{q['quote_number']}.pdf")


@app.route("/api/quotations/<int:qid>/export/excel")
@login_required
def export_excel(qid):
    conn = get_db()
    q = conn.execute("SELECT * FROM quotations WHERE id = ?", (qid,)).fetchone()
    items = conn.execute(
        "SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY sort_order", (qid,)
    ).fetchall()
    conn.close()
    if not q:
        return jsonify({"error": "Not found"}), 404
    xl_bytes = generate_excel(q, items)
    return send_file(io.BytesIO(xl_bytes),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"{q['quote_number']}.xlsx")


# ── Employees API ──────────────────────────────────────────────────────────────

@app.route("/api/employees", methods=["GET"])
@login_required
def get_employees():
    # Readable by any logged-in user — the attendance marker needs the roster
    # to mark attendance, and payroll viewers need names/salaries. Writes below
    # are admin-only.
    conn = get_db()
    rows = conn.execute("SELECT * FROM employees ORDER BY active DESC, name").fetchall()
    conn.close()
    return jsonify(rows)


@app.route("/api/employees", methods=["POST"])
@admin_required
def create_employee():
    data = request.json
    name = (data.get("name") or "").strip()
    try:
        salary = float(data.get("monthly_salary"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid monthly salary"}), 400
    if not name:
        return jsonify({"error": "Name required"}), 400
    if salary <= 0:
        return jsonify({"error": "Monthly salary must be greater than 0"}), 400
    conn = get_db()
    eid = conn.insert(
        "INSERT INTO employees (name, monthly_salary) VALUES (?,?)", (name, salary)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": eid})


@app.route("/api/employees/<int:eid>", methods=["PUT"])
@admin_required
def update_employee(eid):
    data = request.json
    name = (data.get("name") or "").strip()
    try:
        salary = float(data.get("monthly_salary"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid monthly salary"}), 400
    if not name:
        return jsonify({"error": "Name required"}), 400
    if salary <= 0:
        return jsonify({"error": "Monthly salary must be greater than 0"}), 400
    active = 1 if data.get("active", True) else 0
    conn = get_db()
    conn.execute(
        "UPDATE employees SET name=?, monthly_salary=?, active=? WHERE id=?",
        (name, salary, active, eid)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/employees/<int:eid>", methods=["DELETE"])
@admin_required
def delete_employee(eid):
    conn = get_db()
    conn.execute("DELETE FROM employees WHERE id=?", (eid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Attendance API ─────────────────────────────────────────────────────────────

@app.route("/api/attendance/calendar", methods=["GET"])
@marker_required
def get_attendance_calendar():
    try:
        eid = int(request.args.get("employee_id"))
        year = int(request.args.get("year"))
        month = int(request.args.get("month"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid employee_id/year/month"}), 400
    if not (1 <= month <= 12):
        return jsonify({"error": "Invalid month"}), 400

    conn = get_db()
    emp = conn.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    if not emp:
        conn.close()
        return jsonify({"error": "Employee not found"}), 404

    prefix = f"{year:04d}-{month:02d}-"
    marked = {
        r["date"]: r["status"]
        for r in conn.execute(
            "SELECT date, status FROM attendance WHERE employee_id=? AND date LIKE ?",
            (eid, prefix + "%")
        ).fetchall()
    }
    conn.close()

    cal_days = calendar.monthrange(year, month)[1]
    is_admin = bool(session.get("is_admin"))
    today = date.today()
    days = []
    for d in range(1, cal_days + 1):
        date_str = f"{prefix}{d:02d}"
        day_date = date(year, month, d)
        status = marked.get(date_str)
        is_sunday = day_date.weekday() == 6
        is_future = day_date > today
        # Sundays and days that haven't happened yet are never markable by
        # anyone, admin included — there's nothing to correct, so this isn't
        # the same "only an admin can fix it" lock as an already-marked day.
        off_limits = is_sunday or is_future
        # A day nobody's admin has already marked is locked for a non-admin
        # marker — they can set it once, but only an admin can correct a
        # mistake after the fact, so an attendance record can't quietly
        # drift after the fact without an admin's say-so.
        locked = off_limits or (bool(status) and not is_admin)
        days.append({
            "date": date_str, "status": status, "locked": locked,
            "is_sunday": is_sunday, "is_future": is_future,
        })

    return jsonify({
        "employee_id": eid, "employee_name": emp["name"],
        "year": year, "month": month, "days": days
    })


@app.route("/api/attendance/mark", methods=["POST"])
@marker_required
def mark_attendance_day():
    data = request.json
    try:
        eid = int(data.get("employee_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid employee_id"}), 400
    date_str = (data.get("date") or "").strip()
    status = data.get("status")
    if status not in ("present", "absent"):
        return jsonify({"error": "Status must be present or absent"}), 400
    if not date_str:
        return jsonify({"error": "Date required"}), 400
    try:
        day_date = date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400
    if day_date.weekday() == 6:
        return jsonify({"error": "Sundays aren't markable"}), 400
    if day_date > date.today():
        return jsonify({"error": "Can't mark attendance for a day that hasn't happened yet"}), 400

    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM attendance WHERE employee_id=? AND date=?", (eid, date_str)
    ).fetchone()
    if existing and not session.get("is_admin"):
        conn.close()
        return jsonify({"error": "This day is already marked — only an admin can change it"}), 403

    conn.execute(
        "INSERT INTO attendance (employee_id, date, status, marked_by) VALUES (?,?,?,?) "
        "ON CONFLICT (employee_id, date) DO UPDATE SET "
        "status = excluded.status, marked_by = excluded.marked_by, marked_at = CURRENT_TIMESTAMP",
        (eid, date_str, status, session["username"])
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "status": status})


# ── Payroll API ────────────────────────────────────────────────────────────────
# Deduction per absent day = monthly_salary / calendar_days_in_month, so it
# self-adjusts for shorter/longer months (Feb vs a 31-day month) rather than
# using a fixed divisor. Only days explicitly marked "absent" count against
# pay — a day nobody marked is neither charged nor assumed worked, and shows
# up as "unmarked" so the admin can see gaps before finalizing, rather than
# the deduction silently guessing at a policy nobody specified.

@app.route("/api/payroll/generate", methods=["POST"])
@admin_required
def generate_payroll():
    data = request.json
    try:
        year = int(data.get("year"))
        month = int(data.get("month"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid year/month"}), 400
    if not (1 <= month <= 12):
        return jsonify({"error": "Invalid month"}), 400

    conn = get_db()
    employees = conn.execute("SELECT * FROM employees WHERE active = 1 ORDER BY name").fetchall()
    cal_days = calendar.monthrange(year, month)[1]
    prefix = f"{year:04d}-{month:02d}-"

    for emp in employees:
        existing = conn.execute(
            "SELECT * FROM payroll WHERE employee_id=? AND year=? AND month=?",
            (emp["id"], year, month)
        ).fetchone()
        # Never silently overwrite a period the admin already sent to the
        # accountant or that's already been paid out.
        if existing and existing["status"] != "draft":
            continue

        present = conn.execute(
            "SELECT COUNT(*) as n FROM attendance WHERE employee_id=? AND date LIKE ? AND status='present'",
            (emp["id"], prefix + "%")
        ).fetchone()["n"]
        absent = conn.execute(
            "SELECT COUNT(*) as n FROM attendance WHERE employee_id=? AND date LIKE ? AND status='absent'",
            (emp["id"], prefix + "%")
        ).fetchone()["n"]
        salary = emp["monthly_salary"]
        deduction = (salary / cal_days) * absent
        computed = round(salary - deduction, 2)

        if existing:
            # Recompute from attendance, but never touch final_pay here — if
            # the admin already typed in an override, a routine re-generate
            # (e.g. after a late attendance correction) must not silently
            # wipe it out. The admin sees computed_pay change and decides.
            conn.execute(
                "UPDATE payroll SET monthly_salary=?, calendar_days=?, present_days=?, "
                "absent_days=?, computed_pay=?, generated_at=CURRENT_TIMESTAMP WHERE id=?",
                (salary, cal_days, present, absent, computed, existing["id"])
            )
        else:
            conn.insert(
                "INSERT INTO payroll (employee_id, year, month, monthly_salary, calendar_days, "
                "present_days, absent_days, computed_pay, final_pay) VALUES (?,?,?,?,?,?,?,?,?)",
                (emp["id"], year, month, salary, cal_days, present, absent, computed, computed)
            )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/payroll", methods=["GET"])
@payroll_viewer_required
def list_payroll():
    year = request.args.get("year")
    month = request.args.get("month")
    conn = get_db()
    where, params = [], []
    if year:
        where.append("p.year = ?"); params.append(int(year))
    if month:
        where.append("p.month = ?"); params.append(int(month))
    # Non-admins (accountant role) only ever see periods actually sent to
    # them — a draft is the admin's own working copy, not payroll-queue data.
    if not session.get("is_admin"):
        where.append("p.status IN ('finalized','paid')")
    sql = "SELECT p.*, e.name as employee_name FROM payroll p JOIN employees e ON e.id = p.employee_id "
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY e.name"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify(rows)


@app.route("/api/payroll/<int:pid>", methods=["PUT"])
@admin_required
def update_payroll(pid):
    data = request.json
    conn = get_db()
    row = conn.execute("SELECT * FROM payroll WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    if row["status"] != "draft":
        conn.close()
        return jsonify({"error": "Only draft payroll can be edited — revert it to draft first"}), 400
    try:
        final_pay = float(data.get("final_pay"))
    except (TypeError, ValueError):
        conn.close()
        return jsonify({"error": "Invalid final pay"}), 400
    if final_pay < 0:
        conn.close()
        return jsonify({"error": "Final pay cannot be negative"}), 400
    conn.execute("UPDATE payroll SET final_pay=? WHERE id=?", (final_pay, pid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/payroll/finalize", methods=["POST"])
@admin_required
def finalize_payroll():
    data = request.json
    try:
        year = int(data.get("year"))
        month = int(data.get("month"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid year/month"}), 400
    conn = get_db()
    conn.execute(
        "UPDATE payroll SET status='finalized', finalized_by=?, finalized_at=CURRENT_TIMESTAMP "
        "WHERE year=? AND month=? AND status='draft'",
        (session["username"], year, month)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/payroll/<int:pid>/revert", methods=["POST"])
@admin_required
def revert_payroll(pid):
    conn = get_db()
    row = conn.execute("SELECT * FROM payroll WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    if row["status"] == "paid":
        conn.close()
        return jsonify({"error": "Already marked paid — cannot revert"}), 400
    conn.execute(
        "UPDATE payroll SET status='draft', finalized_by='', finalized_at=NULL WHERE id=?", (pid,)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/payroll/<int:pid>/mark-paid", methods=["POST"])
@payroll_viewer_required
def mark_payroll_paid(pid):
    conn = get_db()
    row = conn.execute("SELECT * FROM payroll WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    if row["status"] != "finalized":
        conn.close()
        return jsonify({"error": "Only finalized payroll can be marked paid"}), 400
    conn.execute(
        "UPDATE payroll SET status='paid', paid_by=?, paid_at=CURRENT_TIMESTAMP WHERE id=?",
        (session["username"], pid)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Projects API ───────────────────────────────────────────────────────────────
# A Project is the internal costing workspace where multiple suppliers' makes
# get compared for the same spec line — purchase discount (what a supplier
# quotes us) vs. sale discount (what we quote the client) both apply against
# the same underlying list price, so margin is just sale_rate - purchase_rate.
# None of this ever touches the quotations/quotation_items tables until
# "Generate Quotation" is explicitly run, so cost/margin data can never leak
# onto a client-facing PDF or Excel export by accident.

def _compute_price(base: float, adj_type: str, adj_value: float) -> float:
    if adj_type == "markup":
        price = base * (1 + adj_value / 100)
    elif adj_type == "discount":
        price = base * (1 - adj_value / 100)
    else:
        price = base
    return round(price, 2)


@app.route("/api/projects", methods=["GET"])
@login_required
def list_projects():
    conn = get_db()
    rows = conn.execute(
        "SELECT p.*, "
        "(SELECT COUNT(*) FROM project_items pi WHERE pi.project_id = p.id) as item_count "
        "FROM projects p ORDER BY p.updated_at DESC"
    ).fetchall()
    conn.close()
    return jsonify(rows)


@app.route("/api/projects/stats", methods=["GET"])
@admin_required
def project_stats():
    conn = get_db()
    # Only lines with a chosen winner count toward the totals — same rule
    # each project editor uses for its own purchase/sale/margin summary,
    # so this is just that same math rolled up across every project.
    rows = conn.execute(
        "SELECT pi.quantity, pio.list_price, pio.purchase_adj_type, pio.purchase_adj_value, "
        "pio.sale_adj_type, pio.sale_adj_value "
        "FROM project_items pi JOIN project_item_options pio ON pio.id = pi.selected_option_id "
        "WHERE pi.selected_option_id IS NOT NULL"
    ).fetchall()
    project_count = conn.execute("SELECT COUNT(*) as n FROM projects").fetchone()["n"]
    conn.close()

    total_purchase = 0.0
    total_sale = 0.0
    for r in rows:
        qty = r["quantity"]
        total_purchase += _compute_price(r["list_price"], r["purchase_adj_type"], r["purchase_adj_value"]) * qty
        total_sale += _compute_price(r["list_price"], r["sale_adj_type"], r["sale_adj_value"]) * qty
    margin = total_sale - total_purchase
    margin_pct = (margin / total_sale * 100) if total_sale > 0 else 0
    return jsonify({
        "project_count": project_count,
        "priced_line_count": len(rows),
        "total_purchase": round(total_purchase, 2),
        "total_sale": round(total_sale, 2),
        "total_margin": round(margin, 2),
        "margin_pct": round(margin_pct, 1),
    })


@app.route("/api/projects", methods=["POST"])
@login_required
def create_project():
    data = request.json
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Project name required"}), 400
    conn = get_db()
    pid = conn.insert(
        "INSERT INTO projects (name, client_name, notes, created_by) VALUES (?,?,?,?)",
        (name, (data.get("client_name") or "").strip(), data.get("notes", ""), session["username"])
    )
    conn.commit()
    conn.close()
    return jsonify({"id": pid})


@app.route("/api/projects/<int:pid>", methods=["GET"])
@login_required
def get_project(pid):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not project:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    items = conn.execute(
        "SELECT * FROM project_items WHERE project_id=? ORDER BY sort_order", (pid,)
    ).fetchall()
    for item in items:
        item["options"] = conn.execute(
            "SELECT * FROM project_item_options WHERE project_item_id=? ORDER BY id", (item["id"],)
        ).fetchall()
    conn.close()
    return jsonify({"project": project, "items": items})


@app.route("/api/projects/<int:pid>", methods=["PUT"])
@login_required
def save_project(pid):
    data = request.json
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Project name required"}), 400
    conn = get_db()
    if not conn.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone():
        conn.close()
        return jsonify({"error": "Not found"}), 404

    conn.execute(
        "UPDATE projects SET name=?, client_name=?, notes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (name, (data.get("client_name") or "").strip(), data.get("notes", ""), pid)
    )
    # Full replace, same pattern as quotation saves — the whole item/option
    # tree is small enough that re-inserting it fresh is simpler and safer
    # than diffing, and ON DELETE CASCADE takes the options with the items.
    conn.execute("DELETE FROM project_items WHERE project_id=?", (pid,))
    for idx, item in enumerate(data.get("items", [])):
        item_id = conn.insert(
            "INSERT INTO project_items (project_id, description, unit, quantity, "
            "sale_adj_type, sale_adj_value, sort_order) VALUES (?,?,?,?,?,?,?)",
            (pid, item["description"], item.get("unit", "Nos"), float(item.get("quantity", 1)),
             item.get("sale_adj_type", "none"), float(item.get("sale_adj_value", 0)), idx)
        )
        option_ids = []
        for opt in item.get("options", []):
            # Sale terms are per option (per make, per line) — a new option
            # not yet given its own sale terms inherits the line's default
            # so it isn't silently priced at 0, but each one is independently
            # editable from then on.
            oid = conn.insert(
                "INSERT INTO project_item_options (project_item_id, supplier_id, supplier_name, "
                "item_id, code, description, unit, list_price, purchase_adj_type, purchase_adj_value, "
                "sale_adj_type, sale_adj_value) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, opt.get("supplier_id"), opt.get("supplier_name", ""), opt.get("item_id"),
                 opt.get("code", ""), opt["description"], opt.get("unit", "Nos"),
                 float(opt.get("list_price", 0)), opt.get("purchase_adj_type", "discount"),
                 float(opt.get("purchase_adj_value", 0)),
                 opt.get("sale_adj_type") or item.get("sale_adj_type", "discount"),
                 float(opt.get("sale_adj_value") if opt.get("sale_adj_value") is not None else item.get("sale_adj_value", 0)))
            )
            option_ids.append(oid)
        sel_idx = item.get("selected_option_index")
        if sel_idx is not None and 0 <= sel_idx < len(option_ids):
            conn.execute(
                "UPDATE project_items SET selected_option_id=? WHERE id=?",
                (option_ids[sel_idx], item_id)
            )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/projects/<int:pid>", methods=["DELETE"])
@login_required
def delete_project(pid):
    conn = get_db()
    conn.execute("DELETE FROM projects WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/projects/<int:pid>/generate-quotation", methods=["POST"])
@login_required
def generate_quotation_from_project(pid):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not project:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    items = conn.execute(
        "SELECT * FROM project_items WHERE project_id=? ORDER BY sort_order", (pid,)
    ).fetchall()
    if not items:
        conn.close()
        return jsonify({"error": "Add at least one item before generating a quotation"}), 400

    resolved = []
    missing = []
    for item in items:
        if not item["selected_option_id"]:
            missing.append(item["description"])
            continue
        opt = conn.execute(
            "SELECT * FROM project_item_options WHERE id=?", (item["selected_option_id"],)
        ).fetchone()
        if not opt:
            missing.append(item["description"])
            continue
        resolved.append((item, opt))
    if missing:
        conn.close()
        return jsonify({
            "error": "Pick a make for every line before generating a quotation — missing: "
                     + ", ".join(missing[:5]) + ("…" if len(missing) > 5 else "")
        }), 400

    qid = conn.insert(
        "INSERT INTO quotations (quote_number, client_name, date, created_by, updated_by) "
        "VALUES (?,?,?,?,?)",
        (_next_quote_number(), project["client_name"] or project["name"], date.today().isoformat(),
         session["username"], session["username"])
    )
    for idx, (item, opt) in enumerate(resolved):
        final_price = _compute_price(opt["list_price"], opt["sale_adj_type"], opt["sale_adj_value"])
        conn.execute(
            "INSERT INTO quotation_items "
            "(quotation_id, item_id, description, code, unit, quantity, base_price, "
            "adjustment_type, adjustment_value, final_price, sort_order, supplier_name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (qid, opt["item_id"], opt["description"], opt["code"], item["unit"], item["quantity"],
             opt["list_price"], opt["sale_adj_type"], opt["sale_adj_value"], final_price, idx,
             opt["supplier_name"])
        )
    conn.execute("UPDATE projects SET quotation_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (qid, pid))
    conn.commit()
    conn.close()
    return jsonify({"quotation_id": qid})


# ── Orders API ──────────────────────────────────────────────────────────────────

_ORDER_TOTALS_JOIN = (
    "LEFT JOIN (SELECT order_id, SUM(quantity*unit_price) as total_amount, "
    "SUM(quantity) as total_qty, SUM(supplied_qty) as total_supplied "
    "FROM order_items GROUP BY order_id) oi ON oi.order_id = o.id "
    "LEFT JOIN (SELECT order_id, SUM(amount) as paid_amount "
    "FROM order_payments GROUP BY order_id) op ON op.order_id = o.id"
)


# Open/Completed is derived from the underlying rows on every read rather
# than stored, so it can never drift out of sync with the items/payments
# that actually determine it.
def _annotate_order_status(row):
    row["total_amount"] = row["total_amount"] or 0
    row["total_qty"] = row["total_qty"] or 0
    row["total_supplied"] = row["total_supplied"] or 0
    row["paid_amount"] = row["paid_amount"] or 0
    row["balance"] = row["total_amount"] - row["paid_amount"]
    fully_supplied = row["total_supplied"] >= row["total_qty"] - 1e-6
    fully_paid = row["paid_amount"] >= row["total_amount"] - 1e-6
    row["status"] = "completed" if (fully_supplied and fully_paid) else "open"
    return row


@app.route("/api/orders", methods=["GET"])
@login_required
def list_orders():
    conn = get_db()
    rows = conn.execute(
        f"SELECT o.*, oi.total_amount, oi.total_qty, oi.total_supplied, op.paid_amount "
        f"FROM orders o {_ORDER_TOTALS_JOIN} ORDER BY o.id DESC"
    ).fetchall()
    conn.close()
    return jsonify([_annotate_order_status(r) for r in rows])


@app.route("/api/orders/<int:oid>", methods=["GET"])
@login_required
def get_order(oid):
    conn = get_db()
    row = conn.execute(
        f"SELECT o.*, oi.total_amount, oi.total_qty, oi.total_supplied, op.paid_amount "
        f"FROM orders o {_ORDER_TOTALS_JOIN} WHERE o.id=?", (oid,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    order = _annotate_order_status(row)
    items = conn.execute(
        "SELECT * FROM order_items WHERE order_id=? ORDER BY sort_order", (oid,)
    ).fetchall()
    payments = conn.execute(
        "SELECT * FROM order_payments WHERE order_id=? ORDER BY payment_date DESC, id DESC", (oid,)
    ).fetchall()
    conn.close()
    return jsonify({"order": order, "items": items, "payments": payments})


@app.route("/api/orders/<int:oid>", methods=["PUT"])
@login_required
def update_order(oid):
    data = request.json
    conn = get_db()
    if not conn.execute("SELECT id FROM orders WHERE id=?", (oid,)).fetchone():
        conn.close()
        return jsonify({"error": "Not found"}), 404
    conn.execute(
        "UPDATE orders SET notes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (data.get("notes", ""), oid)
    )
    for item in data.get("items", []):
        conn.execute(
            "UPDATE order_items SET supplied_qty=? WHERE id=? AND order_id=?",
            (float(item.get("supplied_qty", 0)), item["id"], oid)
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/orders/<int:oid>", methods=["DELETE"])
@login_required
def delete_order(oid):
    conn = get_db()
    conn.execute("DELETE FROM orders WHERE id=?", (oid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/orders/<int:oid>/payments", methods=["POST"])
@login_required
def add_order_payment(oid):
    data = request.json
    conn = get_db()
    if not conn.execute("SELECT id FROM orders WHERE id=?", (oid,)).fetchone():
        conn.close()
        return jsonify({"error": "Not found"}), 404
    amount = float(data.get("amount") or 0)
    if amount <= 0:
        conn.close()
        return jsonify({"error": "Enter a payment amount greater than 0"}), 400
    pid = conn.insert(
        "INSERT INTO order_payments (order_id, amount, payment_date, note, recorded_by) "
        "VALUES (?,?,?,?,?)",
        (oid, amount, data.get("payment_date") or date.today().isoformat(),
         data.get("note", ""), session["username"])
    )
    conn.execute("UPDATE orders SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (oid,))
    conn.commit()
    conn.close()
    return jsonify({"id": pid})


@app.route("/api/orders/<int:oid>/payments/<int:pid>", methods=["DELETE"])
@login_required
def delete_order_payment(oid, pid):
    conn = get_db()
    conn.execute("DELETE FROM order_payments WHERE id=? AND order_id=?", (pid, oid))
    conn.execute("UPDATE orders SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (oid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# Runs on import so it also fires under Vercel's serverless WSGI handler
# (api/index.py imports `app` directly — the __main__ guard below never runs there).
# init_db() is idempotent (CREATE TABLE IF NOT EXISTS + column-exists checks),
# so re-running it on each cold start is safe.
init_db()

if __name__ == "__main__":
    app.run(debug=False, port=5050)
