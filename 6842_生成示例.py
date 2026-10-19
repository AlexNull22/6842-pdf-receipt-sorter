"""生成完全自制、固定种子且可重现的票据 PDF，不调用合并引擎。"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import random
import tempfile

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


SEED = 6842
NAVY = colors.HexColor("#173348")
TEAL = colors.HexColor("#087F8C")
MUTED = colors.HexColor("#5A7182")
PALE = colors.HexColor("#EDF4F6")
PAGE_WIDTH, PAGE_HEIGHT = A4


def _text(pdf, x, y, text, size=10, color=NAVY, bold=False):
    pdf.setFillColor(color)
    pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    pdf.drawString(x, y, str(text))


def _right(pdf, x, y, text, size=10, color=NAVY, bold=False):
    pdf.setFillColor(color)
    pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    pdf.drawRightString(x, y, str(text))


def _chrome(pdf, identifier, issued, page_number, pages, continuation=False):
    pdf.setFillColor(NAVY)
    pdf.rect(0, PAGE_HEIGHT - 158, PAGE_WIDTH, 158, stroke=0, fill=1)
    _text(pdf, 44, PAGE_HEIGHT - 39, "NORTHLINE / SAMPLE OPERATIONS", 10, colors.white, True)
    _text(pdf, 44, PAGE_HEIGHT - 80, "INVOICE" if not continuation else "SUPPORTING RECORD", 29, colors.white, True)
    _text(pdf, 44, PAGE_HEIGHT - 105, "SYNTHETIC - PORTFOLIO DEMO", 10, colors.HexColor("#B2E3E5"), True)
    _right(pdf, PAGE_WIDTH - 44, PAGE_HEIGHT - 76, identifier, 13, colors.white, True)
    _right(pdf, PAGE_WIDTH - 44, PAGE_HEIGHT - 105, f"Invoice Date: {issued.isoformat()}", 11, colors.white)
    pdf.setFillColor(TEAL)
    pdf.rect(0, PAGE_HEIGHT - 163, PAGE_WIDTH, 5, stroke=0, fill=1)
    pdf.setStrokeColor(colors.HexColor("#D4E0E6"))
    pdf.line(44, 64, PAGE_WIDTH - 44, 64)
    _text(pdf, 44, 46, "Self-generated sample. No customer information. No payment requested.", 8, MUTED)
    _text(pdf, 44, 32, f"{identifier} | SOURCE PAGE {page_number}/{pages}", 8, MUTED)
    _right(pdf, PAGE_WIDTH - 44, 32, f"Page {page_number} of {pages}", 8, MUTED)


def _write_sample(path, number, issued, pages, rng):
    identifier = f"DOC-6842-{number:04d}"
    pdf = canvas.Canvas(str(path), pagesize=A4, invariant=1, pageCompression=1)
    pdf.setTitle(f"Synthetic invoice {identifier}")
    pdf.setAuthor("6842 Portfolio Demo")
    pdf.setSubject("Self-generated test document; no real client data")
    _chrome(pdf, identifier, issued, 1, pages)
    y = PAGE_HEIGHT - 205
    _text(pdf, 44, y, "ISSUED BY", 9, TEAL, True)
    _text(pdf, 308, y, "BILL TO", 9, TEAL, True)
    for offset, line in enumerate(("Northline Sample Operations", "100 Example Avenue", "Demo City, DC 00000")):
        _text(pdf, 44, y - 22 - offset * 17, line, 11 if offset == 0 else 10, bold=offset == 0)
    for offset, line in enumerate((f"Demonstration Account {number:04d}", "200 Fictional Street", "Sample Region, SR 00000")):
        _text(pdf, 308, y - 22 - offset * 17, line, 11 if offset == 0 else 10, bold=offset == 0)
    table_y = y - 112
    pdf.setFillColor(PALE)
    pdf.roundRect(44, table_y - 29, PAGE_WIDTH - 88, 31, 4, stroke=0, fill=1)
    _text(pdf, 56, table_y - 18, "DESCRIPTION", 9, TEAL, True)
    _right(pdf, 365, table_y - 18, "QTY", 9, TEAL, True)
    _right(pdf, 446, table_y - 18, "UNIT / USD", 9, TEAL, True)
    _right(pdf, PAGE_WIDTH - 55, table_y - 18, "TOTAL / USD", 9, TEAL, True)
    labels = ("Document intake and validation", "Index preparation", "Quality review and delivery")
    subtotal = Decimal("0")
    row_y = table_y - 59
    for label in labels:
        quantity = rng.randint(1, 4)
        price = Decimal(rng.randint(1200, 8900)) / 100
        total = price * quantity
        subtotal += total
        _text(pdf, 56, row_y, label, 10)
        _right(pdf, 365, row_y, quantity, 10)
        _right(pdf, 446, row_y, f"{price:,.2f}", 10)
        _right(pdf, PAGE_WIDTH - 55, row_y, f"{total:,.2f}", 10)
        pdf.setStrokeColor(colors.HexColor("#E0E8EC"))
        pdf.line(44, row_y - 14, PAGE_WIDTH - 44, row_y - 14)
        row_y -= 44
    tax = (subtotal * Decimal("0.05")).quantize(Decimal("0.01"))
    _right(pdf, 438, row_y - 7, "Subtotal", 10, MUTED)
    _right(pdf, PAGE_WIDTH - 55, row_y - 7, f"{subtotal:,.2f}", 10)
    _right(pdf, 438, row_y - 32, "Demo tax (5%)", 10, MUTED)
    _right(pdf, PAGE_WIDTH - 55, row_y - 32, f"{tax:,.2f}", 10)
    pdf.setFillColor(PALE)
    pdf.roundRect(308, row_y - 80, PAGE_WIDTH - 352, 35, 4, stroke=0, fill=1)
    _text(pdf, 320, row_y - 67, "DEMO TOTAL", 10, TEAL, True)
    _right(pdf, PAGE_WIDTH - 55, row_y - 67, f"USD {subtotal + tax:,.2f}", 12, NAVY, True)
    _text(pdf, 44, 161, "PURPOSE OF THIS SAMPLE", 9, TEAL, True)
    _text(pdf, 44, 140, "A readable, text-based PDF for date extraction and chronological merging.", 10)
    _text(pdf, 44, 122, "All names, addresses, line items and amounts are fictional.", 10, MUTED)
    if pages == 2:
        pdf.showPage()
        _chrome(pdf, identifier, issued, 2, pages, continuation=True)
        _text(pdf, 44, PAGE_HEIGHT - 211, "DELIVERY AND REVIEW NOTES", 13, TEAL, True)
        notes = (
            "This continuation page belongs to the preceding synthetic invoice.",
            "A multi-page source must remain adjacent and in its original page order.",
            "The merged index should map this document to a two-page range.",
            "The document bookmark should point to its first merged page.",
        )
        for index, note in enumerate(notes):
            y = PAGE_HEIGHT - 253 - index * 53
            pdf.setFillColor(PALE)
            pdf.circle(54, y + 3, 10, stroke=0, fill=1)
            _text(pdf, 51, y, str(index + 1), 9, TEAL, True)
            _text(pdf, 76, y, note, 10)
        _text(pdf, 44, 205, "TRACEABILITY", 9, TEAL, True)
        _text(pdf, 44, 184, f"Source identifier: {identifier}", 11)
        _text(pdf, 44, 163, f"Document date: {issued.isoformat()}", 11)
        _text(pdf, 44, 142, "This page contains no additional or conflicting invoice date.", 10, MUTED)
    pdf.save()


def generate_samples(output_dir, count=300):
    """生成固定种子的PDF；默认300份，其中12份双页，共312页。拒绝覆盖。"""
    if type(count) is not int or not 1 <= count <= 10000:
        raise ValueError("count must be an integer from 1 to 10000")
    output = Path(output_dir).absolute()
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("output directory must be new or empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    issued_dates = [date(2026, 1, 1) + timedelta(days=index % 31) for index in range(count)]
    rng.shuffle(issued_dates)
    double_pages = set(rng.sample(range(count), min(12, count)))
    documents = []
    with tempfile.TemporaryDirectory(prefix="6842_生成暂存_", dir=output.parent) as stage_name:
        stage = Path(stage_name)
        for index, issued in enumerate(issued_dates):
            filename = f"6842_票据_{index + 1:04d}.pdf"
            pages = 2 if index in double_pages else 1
            path = stage / filename
            _write_sample(path, index + 1, issued, pages, rng)
            documents.append({"filename": filename, "date": issued.isoformat(), "pages": pages,
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        if output.exists():
            output.rmdir()
        os.replace(stage, output)
    return {"seed": SEED, "count": count, "pages": count + len(double_pages), "documents": documents}


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成完全自制且可重现的票据PDF样例")
    parser.add_argument("--output", required=True, help="新建或空的输出目录")
    parser.add_argument("--count", type=int, default=300, help="PDF份数，默认300")
    args = parser.parse_args(argv)
    try:
        result = generate_samples(args.output, args.count)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(json.dumps({key: result[key] for key in ("seed", "count", "pages")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
