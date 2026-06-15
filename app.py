import os
import json
from datetime import date
from pathlib import Path

# Load .env from project root
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
from flask import Flask, request, jsonify, render_template, send_file
import io

from database import init_db, get_db
from pdf_parser import parse_catalog_pdf
from export import generate_pdf, generate_excel

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB


def _next_quote_number():
    conn = get_db()
    row = conn.execute(
        "SELECT quote_number FROM quotations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        year = date.today().year
        return f"BM-{year}-001"
    last = row["quote_number"]
    parts = last.rsplit("-", 1)
    try:
        num = int(parts[-1]) + 1
        return f"{parts[0]}-{num:03d}"
    except ValueError:
        return f"BM-{date.today().year}-001"


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/catalogs")
def catalogs_page():
    return render_template("catalogs.html")


@app.route("/quotation/new")
def new_quotation_page():
    return render_template("quotation.html")


@app.route("/quotation/<int:qid>/edit")
def edit_quotation_page(qid):
    return render_template("quotation.html", qid=qid)


@app.route("/history")
def history_page():
    return render_template("history.html")


# ── Suppliers API ──────────────────────────────────────────────────────────────

@app.route("/api/suppliers", methods=["GET"])
def get_suppliers():
    conn = get_db()
    rows = conn.execute(
        "SELECT s.id, s.name, s.created_at, COUNT(i.id) as item_count "
        "FROM suppliers s LEFT JOIN items i ON i.supplier_id = s.id "
        "GROUP BY s.id ORDER BY s.name"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/suppliers", methods=["POST"])
def create_supplier():
    data = request.json
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    conn = get_db()
    try:
        conn.execute("INSERT INTO suppliers (name) VALUES (?)", (name,))
        conn.commit()
        sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 400
    conn.close()
    return jsonify({"id": sid, "name": name})


@app.route("/api/suppliers/<int:sid>", methods=["DELETE"])
def delete_supplier(sid):
    conn = get_db()
    conn.execute("DELETE FROM suppliers WHERE id = ?", (sid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Catalog API ───────────────────────────────────────────────────────────────

@app.route("/api/suppliers/<int:sid>/catalogs", methods=["GET"])
def get_catalogs(sid):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM catalogs WHERE supplier_id = ? ORDER BY uploaded_at DESC", (sid,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/suppliers/<int:sid>/upload", methods=["POST"])
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
    catalog_id = request.form.get("catalog_id", "").strip()  # set when re-uploading

    pdf_bytes = f.read()
    try:
        items = parse_catalog_pdf(pdf_bytes, supplier["name"])
    except Exception as e:
        return jsonify({"error": f"Failed to parse PDF: {str(e)}"}), 500

    if not items:
        return jsonify({"error": "No items extracted from PDF"}), 400

    conn = get_db()
    if catalog_id:
        # Re-upload: replace items for this catalog only
        conn.execute("DELETE FROM items WHERE catalog_id = ?", (catalog_id,))
        conn.execute(
            "UPDATE catalogs SET name=?, item_count=?, uploaded_at=CURRENT_TIMESTAMP WHERE id=?",
            (catalog_name, len(items), catalog_id)
        )
    else:
        # New catalog
        conn.execute(
            "INSERT INTO catalogs (supplier_id, name, item_count) VALUES (?,?,?)",
            (sid, catalog_name, len(items))
        )
        catalog_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.executemany(
        "INSERT INTO items (supplier_id, catalog_id, code, description, unit, base_price) VALUES (?,?,?,?,?,?)",
        [(sid, catalog_id, i["code"], i["description"], i["unit"], i["base_price"]) for i in items]
    )
    conn.commit()
    conn.close()

    return jsonify({"imported": len(items), "catalog_id": catalog_id, "catalog_name": catalog_name})


@app.route("/api/catalogs/<int:cid>", methods=["DELETE"])
def delete_catalog(cid):
    conn = get_db()
    conn.execute("DELETE FROM catalogs WHERE id = ?", (cid,))  # items cascade
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Items API ─────────────────────────────────────────────────────────────────

@app.route("/api/items", methods=["GET"])
def search_items():
    q = request.args.get("q", "").strip()
    supplier_id = request.args.get("supplier_id")
    limit = int(request.args.get("limit", 50))

    sql = (
        "SELECT i.id, i.code, i.description, i.unit, i.base_price, i.catalog_id, "
        "s.id as supplier_id, s.name as supplier_name, "
        "c.name as catalog_name "
        "FROM items i JOIN suppliers s ON s.id = i.supplier_id "
        "LEFT JOIN catalogs c ON c.id = i.catalog_id "
    )
    params = []
    where = []
    if q:
        where.append("(i.description LIKE ? OR i.code LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if supplier_id:
        where.append("i.supplier_id = ?")
        params.append(supplier_id)
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY i.description LIMIT ?"
    params.append(limit)

    conn = get_db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/items/<int:iid>", methods=["PUT"])
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
def delete_item(iid):
    conn = get_db()
    conn.execute("DELETE FROM items WHERE id = ?", (iid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/suppliers/<int:sid>/items", methods=["GET"])
def get_supplier_items(sid):
    conn = get_db()
    rows = conn.execute(
        "SELECT i.*, c.name as catalog_name FROM items i "
        "LEFT JOIN catalogs c ON c.id = i.catalog_id "
        "WHERE i.supplier_id = ? ORDER BY i.description", (sid,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ── Quotations API ─────────────────────────────────────────────────────────────

@app.route("/api/quotations", methods=["GET"])
def list_quotations():
    conn = get_db()
    rows = conn.execute(
        "SELECT q.*, COUNT(qi.id) as item_count "
        "FROM quotations q LEFT JOIN quotation_items qi ON qi.quotation_id = q.id "
        "GROUP BY q.id ORDER BY q.id DESC"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/quotations/next-number", methods=["GET"])
def next_quote_number():
    return jsonify({"quote_number": _next_quote_number()})


@app.route("/api/quotations", methods=["POST"])
def create_quotation():
    data = request.json
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO quotations (quote_number, client_name, client_address, date, gst_rate, notes) "
            "VALUES (?,?,?,?,?,?)",
            (
                data["quote_number"],
                data["client_name"],
                data.get("client_address", ""),
                data["date"],
                float(data.get("gst_rate", 18)),
                data.get("notes", ""),
            )
        )
        conn.commit()
        qid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        for idx, item in enumerate(data.get("items", [])):
            conn.execute(
                "INSERT INTO quotation_items "
                "(quotation_id, item_id, description, code, unit, quantity, base_price, "
                "adjustment_type, adjustment_value, final_price, sort_order) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    qid,
                    item.get("item_id"),
                    item["description"],
                    item.get("code", ""),
                    item.get("unit", "Nos"),
                    float(item["quantity"]),
                    float(item["base_price"]),
                    item.get("adjustment_type", "none"),
                    float(item.get("adjustment_value", 0)),
                    float(item["final_price"]),
                    idx,
                )
            )
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 400
    conn.close()
    return jsonify({"id": qid})


@app.route("/api/quotations/<int:qid>", methods=["GET"])
def get_quotation(qid):
    conn = get_db()
    q = conn.execute("SELECT * FROM quotations WHERE id = ?", (qid,)).fetchone()
    if not q:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    items = conn.execute(
        "SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY sort_order", (qid,)
    ).fetchall()
    conn.close()
    return jsonify({"quotation": dict(q), "items": [dict(i) for i in items]})


@app.route("/api/quotations/<int:qid>", methods=["PUT"])
def update_quotation(qid):
    data = request.json
    conn = get_db()
    conn.execute(
        "UPDATE quotations SET client_name=?, client_address=?, date=?, gst_rate=?, notes=? WHERE id=?",
        (data["client_name"], data.get("client_address", ""), data["date"],
         float(data.get("gst_rate", 18)), data.get("notes", ""), qid)
    )
    conn.execute("DELETE FROM quotation_items WHERE quotation_id = ?", (qid,))
    for idx, item in enumerate(data.get("items", [])):
        conn.execute(
            "INSERT INTO quotation_items "
            "(quotation_id, item_id, description, code, unit, quantity, base_price, "
            "adjustment_type, adjustment_value, final_price, sort_order) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                qid,
                item.get("item_id"),
                item["description"],
                item.get("code", ""),
                item.get("unit", "Nos"),
                float(item["quantity"]),
                float(item["base_price"]),
                item.get("adjustment_type", "none"),
                float(item.get("adjustment_value", 0)),
                float(item["final_price"]),
                idx,
            )
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/quotations/<int:qid>", methods=["DELETE"])
def delete_quotation(qid):
    conn = get_db()
    conn.execute("DELETE FROM quotations WHERE id = ?", (qid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Export API ─────────────────────────────────────────────────────────────────

@app.route("/api/quotations/<int:qid>/export/pdf")
def export_pdf(qid):
    conn = get_db()
    q = conn.execute("SELECT * FROM quotations WHERE id = ?", (qid,)).fetchone()
    items = conn.execute(
        "SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY sort_order", (qid,)
    ).fetchall()
    conn.close()
    if not q:
        return jsonify({"error": "Not found"}), 404
    pdf_bytes = generate_pdf(dict(q), [dict(i) for i in items])
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{q['quote_number']}.pdf",
    )


@app.route("/api/quotations/<int:qid>/export/excel")
def export_excel(qid):
    conn = get_db()
    q = conn.execute("SELECT * FROM quotations WHERE id = ?", (qid,)).fetchone()
    items = conn.execute(
        "SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY sort_order", (qid,)
    ).fetchall()
    conn.close()
    if not q:
        return jsonify({"error": "Not found"}), 404
    xl_bytes = generate_excel(dict(q), [dict(i) for i in items])
    return send_file(
        io.BytesIO(xl_bytes),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"{q['quote_number']}.xlsx",
    )


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5050)
