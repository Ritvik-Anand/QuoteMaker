import io
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side


BRAND_COLOR = colors.HexColor("#1a3c5e")
ACCENT_COLOR = colors.HexColor("#e8f0fe")
LIGHT_GRAY = colors.HexColor("#f5f5f5")
MID_GRAY = colors.HexColor("#cccccc")


def _compute_totals(quote_items, gst_rate):
    subtotal = sum(item["quantity"] * item["final_price"] for item in quote_items)
    gst_amount = round(subtotal * gst_rate / 100, 2)
    total = round(subtotal + gst_amount, 2)
    return round(subtotal, 2), gst_amount, total


def generate_pdf(quotation: dict, quote_items: list[dict]) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
    )

    styles = getSampleStyleSheet()
    story = []

    # Header
    header_data = [[
        Paragraph(
            '<font color="#1a3c5e" size="20"><b>BAGULA MUKHI</b></font><br/>'
            '<font color="#555555" size="9">Electrical Goods Supplier</font>',
            ParagraphStyle("h", fontName="Helvetica", alignment=TA_LEFT)
        ),
        Paragraph(
            f'<font color="#1a3c5e" size="14"><b>QUOTATION</b></font><br/>'
            f'<font color="#333333" size="9">No: <b>{quotation["quote_number"]}</b></font><br/>'
            f'<font color="#333333" size="9">Date: {quotation["date"]}</font>',
            ParagraphStyle("h2", fontName="Helvetica", alignment=TA_RIGHT)
        ),
    ]]
    header_table = Table(header_data, colWidths=[95 * mm, 85 * mm])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(header_table)
    story.append(HRFlowable(width="100%", thickness=2, color=BRAND_COLOR, spaceAfter=6))

    # Client block
    client_info = f"<b>To:</b> {quotation['client_name']}"
    if quotation.get("client_address"):
        client_info += f"<br/>{quotation['client_address'].replace(chr(10), '<br/>')}"
    story.append(Paragraph(client_info, ParagraphStyle(
        "client", fontName="Helvetica", fontSize=10, leading=14, spaceAfter=8
    )))

    # Items table
    col_widths = [12 * mm, 20 * mm, 68 * mm, 15 * mm, 15 * mm, 22 * mm, 25 * mm]
    table_data = [[
        Paragraph("<b>#</b>", ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=8, alignment=TA_CENTER)),
        Paragraph("<b>Code</b>", ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=8, alignment=TA_CENTER)),
        Paragraph("<b>Description</b>", ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=8)),
        Paragraph("<b>Unit</b>", ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=8, alignment=TA_CENTER)),
        Paragraph("<b>Qty</b>", ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=8, alignment=TA_CENTER)),
        Paragraph("<b>Rate (₹)</b>", ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=8, alignment=TA_RIGHT)),
        Paragraph("<b>Amount (₹)</b>", ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=8, alignment=TA_RIGHT)),
    ]]

    for i, item in enumerate(quote_items, 1):
        amount = item["quantity"] * item["final_price"]
        row_style = ParagraphStyle("td", fontName="Helvetica", fontSize=8, leading=11)
        row_style_r = ParagraphStyle("tdr", fontName="Helvetica", fontSize=8, leading=11, alignment=TA_RIGHT)
        row_style_c = ParagraphStyle("tdc", fontName="Helvetica", fontSize=8, leading=11, alignment=TA_CENTER)
        table_data.append([
            Paragraph(str(i), row_style_c),
            Paragraph(item.get("code") or "-", row_style_c),
            Paragraph(item["description"], row_style),
            Paragraph(item.get("unit", "Nos"), row_style_c),
            Paragraph(_fmt_num(item["quantity"]), row_style_c),
            Paragraph(f"{item['final_price']:,.2f}", row_style_r),
            Paragraph(f"{amount:,.2f}", row_style_r),
        ])

    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    ts = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_COLOR),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GRAY]),
        ("GRID", (0, 0), (-1, -1), 0.5, MID_GRAY),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ])
    items_table.setStyle(ts)
    story.append(items_table)
    story.append(Spacer(1, 4 * mm))

    # Totals
    subtotal, gst_amount, total = _compute_totals(quote_items, quotation.get("gst_rate", 18))
    gst_rate = quotation.get("gst_rate", 18)

    totals_data = [
        ["", "Subtotal", f"₹ {subtotal:,.2f}"],
        ["", f"GST ({gst_rate:.0f}%)", f"₹ {gst_amount:,.2f}"],
        ["", "TOTAL", f"₹ {total:,.2f}"],
    ]
    totals_table = Table(totals_data, colWidths=[115 * mm, 35 * mm, 27 * mm])
    totals_table.setStyle(TableStyle([
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("FONTNAME", (1, 0), (1, 1), "Helvetica"),
        ("FONTNAME", (1, 2), (-1, 2), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTSIZE", (1, 2), (-1, 2), 10),
        ("LINEABOVE", (1, 2), (-1, 2), 1, BRAND_COLOR),
        ("TEXTCOLOR", (1, 2), (-1, 2), BRAND_COLOR),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(totals_table)

    # Notes
    if quotation.get("notes"):
        story.append(Spacer(1, 4 * mm))
        story.append(HRFlowable(width="100%", thickness=0.5, color=MID_GRAY, spaceAfter=4))
        story.append(Paragraph(
            f"<b>Notes:</b> {quotation['notes']}",
            ParagraphStyle("notes", fontName="Helvetica", fontSize=8, textColor=colors.HexColor("#555555"))
        ))

    # Footer
    story.append(Spacer(1, 6 * mm))
    story.append(HRFlowable(width="100%", thickness=1, color=BRAND_COLOR, spaceAfter=3))
    story.append(Paragraph(
        "Thank you for your business. This is a computer-generated quotation.",
        ParagraphStyle("footer", fontName="Helvetica", fontSize=7, textColor=colors.HexColor("#777777"), alignment=TA_CENTER)
    ))

    doc.build(story)
    return buffer.getvalue()


def generate_excel(quotation: dict, quote_items: list[dict]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Quotation"

    brand = "1a3c5e"
    accent = "e8f0fe"
    light = "f5f5f5"

    thin = Side(style="thin", color="cccccc")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # Col widths
    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 40
    ws.column_dimensions["D"].width = 8
    ws.column_dimensions["E"].width = 8
    ws.column_dimensions["F"].width = 14
    ws.column_dimensions["G"].width = 16

    row = 1

    # Business name
    ws.merge_cells(f"A{row}:D{row}")
    c = ws[f"A{row}"]
    c.value = "BAGULA MUKHI"
    c.font = Font(name="Calibri", size=18, bold=True, color=brand)
    c.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[row].height = 28

    ws.merge_cells(f"E{row}:G{row}")
    c = ws[f"E{row}"]
    c.value = "QUOTATION"
    c.font = Font(name="Calibri", size=16, bold=True, color=brand)
    c.alignment = Alignment(horizontal="right", vertical="center")
    row += 1

    ws.merge_cells(f"A{row}:D{row}")
    ws[f"A{row}"].value = "Electrical Goods Supplier"
    ws[f"A{row}"].font = Font(name="Calibri", size=9, color="555555")

    ws.merge_cells(f"E{row}:G{row}")
    c = ws[f"E{row}"]
    c.value = f"No: {quotation['quote_number']}    Date: {quotation['date']}"
    c.font = Font(name="Calibri", size=9)
    c.alignment = Alignment(horizontal="right")
    row += 2

    # Client
    ws[f"A{row}"].value = "To:"
    ws[f"A{row}"].font = Font(bold=True, size=10)
    ws.merge_cells(f"B{row}:G{row}")
    ws[f"B{row}"].value = quotation["client_name"]
    ws[f"B{row}"].font = Font(size=10, bold=True)
    row += 1

    if quotation.get("client_address"):
        ws.merge_cells(f"B{row}:G{row}")
        ws[f"B{row}"].value = quotation["client_address"]
        ws[f"B{row}"].font = Font(size=9, color="444444")
        row += 1

    row += 1

    # Header row
    headers = ["#", "Code", "Description", "Unit", "Qty", "Rate (₹)", "Amount (₹)"]
    cols = ["A", "B", "C", "D", "E", "F", "G"]
    for col, h in zip(cols, headers):
        c = ws[f"{col}{row}"]
        c.value = h
        c.font = Font(name="Calibri", bold=True, color="FFFFFF", size=9)
        c.fill = PatternFill("solid", fgColor=brand)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = border
    ws.row_dimensions[row].height = 18
    row += 1

    # Items
    for i, item in enumerate(quote_items, 1):
        amount = item["quantity"] * item["final_price"]
        fill = PatternFill("solid", fgColor="FFFFFF") if i % 2 == 1 else PatternFill("solid", fgColor=light)
        values = [i, item.get("code") or "", item["description"], item.get("unit", "Nos"),
                  item["quantity"], item["final_price"], amount]
        aligns = ["center", "center", "left", "center", "center", "right", "right"]
        for col, val, align in zip(cols, values, aligns):
            c = ws[f"{col}{row}"]
            c.value = val
            c.font = Font(name="Calibri", size=9)
            c.fill = fill
            c.alignment = Alignment(horizontal=align, vertical="center")
            c.border = border
            if col in ("F", "G"):
                c.number_format = '#,##0.00'
        row += 1

    row += 1

    # Totals
    subtotal, gst_amount, total = _compute_totals(quote_items, quotation.get("gst_rate", 18))
    gst_rate = quotation.get("gst_rate", 18)

    for label, value in [("Subtotal", subtotal), (f"GST ({gst_rate:.0f}%)", gst_amount)]:
        ws.merge_cells(f"A{row}:F{row}")
        c = ws[f"A{row}"]
        c.value = label
        c.font = Font(name="Calibri", size=9)
        c.alignment = Alignment(horizontal="right")
        c = ws[f"G{row}"]
        c.value = value
        c.font = Font(name="Calibri", size=9)
        c.alignment = Alignment(horizontal="right")
        c.number_format = '#,##0.00'
        row += 1

    ws.merge_cells(f"A{row}:F{row}")
    c = ws[f"A{row}"]
    c.value = "TOTAL"
    c.font = Font(name="Calibri", size=11, bold=True, color=brand)
    c.alignment = Alignment(horizontal="right")
    c.fill = PatternFill("solid", fgColor=accent)
    c = ws[f"G{row}"]
    c.value = total
    c.font = Font(name="Calibri", size=11, bold=True, color=brand)
    c.alignment = Alignment(horizontal="right")
    c.fill = PatternFill("solid", fgColor=accent)
    c.number_format = '#,##0.00'
    row += 2

    # Notes
    if quotation.get("notes"):
        ws[f"A{row}"].value = "Notes:"
        ws[f"A{row}"].font = Font(bold=True, size=9)
        ws.merge_cells(f"B{row}:G{row}")
        ws[f"B{row}"].value = quotation["notes"]
        ws[f"B{row}"].font = Font(size=9, color="555555")

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _fmt_num(n):
    if n == int(n):
        return str(int(n))
    return str(n)
