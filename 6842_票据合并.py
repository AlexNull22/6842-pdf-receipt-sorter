"""Local invoice/receipt PDF sorting and merging. Dependency: pypdf>=6.10.

Scan produces a reviewable CSV; merge validates that CSV before publishing files.
Source PDFs are read as bytes and are never modified. No OCR or date guessing.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from pypdf import PdfReader, PdfWriter

FIELDS = ["filename", "date", "include", "title", "sha256"]
OUTPUTS = {"pdf": "6842_合并票据.pdf", "map": "6842_来源页码.csv",
           "json": "6842_审计报告.json", "html": "6842_运行报告.html"}
LABEL = re.compile(r"(?im)^[ \t]*(?:invoice[ \t]+date|receipt[ \t]+date|date)[ \t]*:[ \t]*([^\r\n]*)")
ISO = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
MONTH = r"(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
MONTHS = {name: n for n, name in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


class WorkflowError(Exception):
    """An actionable validation or filesystem error. No final files were published."""
    def __init__(self, message, issues=None):
        self.issues = list(issues or [])
        super().__init__(message + ("\n" + "\n".join("- " + item for item in self.issues) if self.issues else ""))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def iso_date(value):
    if not isinstance(value, str) or not ISO.fullmatch(value):
        raise ValueError("date must use YYYY-MM-DD")
    return date.fromisoformat(value).isoformat()


def labelled_date(value):
    value = value.strip()
    if ISO.fullmatch(value):
        return iso_date(value)
    match = re.fullmatch(MONTH + r"\s+(\d{1,2}),?\s+(\d{4})", value, re.I)
    if match:
        month, day, year = match.groups()
    else:
        match = re.fullmatch(r"(\d{1,2})\s+" + MONTH + r",?\s+(\d{4})", value, re.I)
        if not match:
            raise ValueError("unsupported or ambiguous labelled date")
        day, month, year = match.groups()
    return date(int(year), MONTHS[month[:3].lower()], int(day)).isoformat()


def pdf_inventory(folder):
    folder = Path(folder)
    if not folder.is_dir():
        raise WorkflowError("Input folder does not exist or is not a directory.")
    try:
        return sorted((p for p in folder.iterdir() if p.suffix.lower() == ".pdf" and (p.is_file() or p.is_symlink())), key=lambda p: (p.name.casefold(), p.name))
    except OSError as exc:
        raise WorkflowError(f"Cannot list input folder: {exc}") from exc


def pdf_snapshot(path):
    if path.is_symlink():
        raise ValueError("symbolic links are not accepted; use a regular PDF in the input folder")
    payload = path.read_bytes()
    reader = PdfReader(io.BytesIO(payload), strict=False)
    if reader.is_encrypted:
        raise ValueError("encrypted PDF; decrypt a separate copy and scan again")
    pages = len(reader.pages)
    if not pages:
        raise ValueError("PDF contains no pages")
    return payload, reader, pages


def safe_csv(value):
    """Protect display-only CSV values from spreadsheet formula interpretation."""
    text = str(value)
    return "'" + text if text.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")) else text


def scan_folder(input_dir, manifest_path):
    """Scan immediate PDF children and write a new UTF-8 BOM CSV manifest.

    records include filename/date/include/title/sha256 plus status/issues/pages.
    review rows default to include=false and an empty date, requiring human review.
    """
    folder = Path(input_dir).resolve()
    manifest = Path(manifest_path).absolute()
    if manifest.exists() or manifest.is_symlink():
        raise WorkflowError("Manifest already exists; choose a new filename to protect manual corrections.")
    if manifest.suffix.lower() != ".csv":
        raise WorkflowError("Manifest filename must end in .csv.")
    files = pdf_inventory(folder)
    if not files:
        raise WorkflowError("No PDFs found in the input folder. Subfolders are not scanned.")
    records = []
    for path in files:
        record = {"filename": path.name, "date": "", "include": False, "title": path.stem,
                  "sha256": "", "status": "review", "issues": [], "pages": 0}
        try:
            if path.is_symlink():
                raise ValueError("symbolic links are not accepted")
            payload = path.read_bytes()
            record["sha256"] = sha256(payload)
            reader = PdfReader(io.BytesIO(payload), strict=False)
            if reader.is_encrypted:
                raise ValueError("encrypted PDF; decrypt a separate copy and scan again")
            record["pages"] = len(reader.pages)
            if not record["pages"]:
                raise ValueError("PDF contains no pages")
            dates, invalid_labels, labels = set(), [], 0
            for page in reader.pages:
                for match in LABEL.finditer(page.extract_text() or ""):
                    labels += 1
                    try:
                        dates.add(labelled_date(match[1]))
                    except ValueError:
                        invalid_labels.append(match[1].strip() or "[empty date]")
            if not labels:
                record["issues"].append("No supported Invoice Date:, Receipt Date:, or Date: label found; image-only PDFs require manual review/OCR outside this tool.")
            if invalid_labels:
                record["issues"].append("Unsupported or invalid labelled date: " + "; ".join(sorted(set(invalid_labels))))
            if len(dates) > 1:
                record["issues"].append("Conflicting labelled dates: " + ", ".join(sorted(dates)))
            if len(dates) == 1 and not record["issues"]:
                record.update(date=next(iter(dates)), include=True, status="ready")
        except Exception as exc:
            record["issues"].append("Cannot inspect PDF: " + str(exc))
        records.append(record)
    repeated = {name for name in (p.name.casefold() for p in files) if sum(p.name.casefold() == name for p in files) > 1}
    for row in records:
        if row["filename"].casefold() in repeated:
            row.update(date="", include=False, status="review")
            row["issues"].append("Filename differs only by case from another PDF; rename before scanning.")
    temporary = None
    try:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix="6842_清单暂存_", suffix=".csv", dir=manifest.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            for row in records:
                writer.writerow({key: str(row[key]).lower() if key == "include" else row[key] for key in FIELDS})
        # Hard-link publication is atomic and refuses to replace an existing file.
        os.link(temporary, manifest)
    except OSError as exc:
        raise WorkflowError(f"Cannot write new manifest: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"records": records, "ready": sum(r["status"] == "ready" for r in records),
            "review": sum(r["status"] == "review" for r in records), "manifest": str(manifest)}


def read_manifest(path):
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames != FIELDS:
                raise WorkflowError("Manifest columns must be exactly: " + ", ".join(FIELDS))
            rows = list(reader)
        if not rows:
            raise WorkflowError("Manifest has no document rows.")
        return rows
    except WorkflowError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise WorkflowError(f"Cannot read manifest: {exc}") from exc


def validate_manifest(folder, rows):
    files = pdf_inventory(folder)
    inventory = {p.name: p for p in files}
    issues, names, included, excluded, hashes = [], set(), [], [], {}
    for number, row in enumerate(rows, 2):
        name = row.get("filename") or ""
        label = f"CSV row {number} ({name or 'empty filename'})"
        if None in row or any(value is None for value in row.values()):
            issues.append(label + ": row does not contain exactly five fields")
            continue
        if not name or name != name.strip() or name in {".", ".."} or re.search(r"[/\\:\x00-\x1f]", name) or Path(name).name != name or Path(name).suffix.lower() != ".pdf":
            issues.append(label + ": unsafe filename; use a plain PDF filename without a path")
            continue
        if name.casefold() in names:
            issues.append(label + ": duplicate filename")
        names.add(name.casefold())
        include_text = row["include"].strip().lower()
        if include_text not in {"true", "false", "1", "0", "yes", "no"}:
            issues.append(label + ": include must be true/false, 1/0, or yes/no")
            continue
        include = include_text in {"true", "1", "yes"}
        path = inventory.get(name)
        if path is None:
            issues.append(label + ": PDF is missing or filename case does not match")
            continue
        if path.is_symlink() or path.resolve().parent != folder:
            issues.append(label + ": symbolic links or paths outside input folder are not accepted")
            continue
        if not include:
            excluded.append(name)
            continue
        try:
            normalized_date = iso_date(row["date"].strip())
        except ValueError as exc:
            issues.append(label + ": " + str(exc))
            continue
        expected = row["sha256"].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            issues.append(label + ": missing or invalid SHA256; scan again")
            continue
        try:
            payload, reader, pages = pdf_snapshot(path)
            actual = sha256(payload)
            if actual != expected:
                issues.append(label + ": SHA256 mismatch; file changed after scanning")
                continue
            if actual in hashes:
                issues.append(label + ": duplicate PDF bytes; same content as " + hashes[actual])
                continue
            hashes[actual] = name
            included.append({"filename": name, "date": normalized_date, "title": row["title"].strip() or path.stem,
                             "sha256": actual, "pages": pages, "_reader": reader})
        except Exception as exc:
            issues.append(label + ": cannot read included PDF: " + str(exc))
    missing = sorted(set(inventory) - {row.get("filename", "") for row in rows})
    if missing:
        issues.append("Current PDFs not listed in manifest: " + ", ".join(missing))
    if not included:
        issues.append("No valid included documents remain; no merged PDF will be created.")
    if issues:
        raise WorkflowError("Manifest validation failed. Correct the listed issues and try again.", issues)
    return sorted(included, key=lambda item: (item["date"], item["filename"])), excluded, set(inventory)


def write_html(path, report):
    esc = lambda value: html.escape(str(value), quote=True)
    cards = "".join(f'<div class="card"><span>{label}</span><strong>{report["counts"][key]:,}</strong></div>' for key, label in [("documents", "MERGED DOCUMENTS"), ("pages", "TOTAL PAGES"), ("excluded", "EXCLUDED FILES")])
    rows = "".join(f'<tr><td>{i}</td><td>{esc(item["date"])}</td><td><b>{esc(item["title"])}</b><small>{esc(item["filename"])}</small></td><td>{item["start_page"]}–{item["end_page"]}</td><td>{item["pages"]}</td><td><details><summary>View SHA256</summary><code>{item["sha256"]}</code></details></td></tr>' for i, item in enumerate(report["documents"], 1))
    excluded = ", ".join(report["excluded_files"]) or "None"
    document = '''<!doctype html><html lang="en" translate="no"><meta charset="utf-8"><meta name="google" content="notranslate"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PDF sorting and merge audit</title><style>
*{box-sizing:border-box}body{margin:0;background:#f1f5f8;color:#163448;font:15px/1.5 system-ui,Segoe UI,sans-serif}main{max-width:1250px;padding:32px;margin:auto}header{border-bottom:1px solid #d4e0e8;padding:10px 0 24px}.eyebrow{color:#137f84;letter-spacing:.15em;font-size:12px;font-weight:750}h1{font-size:36px;line-height:1.15;margin:12px 0}.muted,small{color:#647b8d}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:25px 0}.card{background:white;padding:20px;border:1px solid #d7e2ea;border-radius:12px}.card span{font-size:11px;letter-spacing:.08em;color:#647b8d}.card strong{display:block;font-size:36px;font-weight:650;color:#137f84;margin-top:4px}.note{background:#e1f2ef;padding:16px 20px;border-left:4px solid #198d83;border-radius:5px}h2{font-size:21px;margin-top:30px}.table{background:white;border:1px solid #d7e2ea;overflow:auto;border-radius:10px}table{border-collapse:collapse;width:100%;text-align:left}th{background:#163448;color:white;font-size:12px;padding:13px;white-space:nowrap}td{padding:12px 13px;border-bottom:1px solid #e4ebef;vertical-align:top}small{display:block;font-size:12px;overflow-wrap:anywhere}td:nth-child(2),td:nth-child(4){white-space:nowrap}summary{font-size:12px;cursor:pointer;color:#137f84;white-space:nowrap}code{display:block;max-width:190px;overflow-wrap:anywhere;font-size:11px;margin-top:8px}footer{font-size:12px;color:#647b8d;margin-top:24px;overflow-wrap:anywhere}@media(max-width:650px){main{padding:18px}h1{font-size:29px}.cards{gap:8px}.card{padding:12px}.card strong{font-size:28px}}
</style><main><header><div class="eyebrow">LOCAL PDF WORKFLOW / VERIFIED OUTPUT</div><h1>Chronological invoice and receipt merge</h1><div class="muted">Manifest: MANIFEST<br>Generated UTC: GENERATED</div></header><section class="cards">CARDS</section><div class="note"><b>Completed with source verification.</b> Documents are sorted by ISO date, then filename. Each included PDF matched its manifest SHA256. Page count was verified before publication. A bookmark marks the start of every document.</div><h2>Source to page map</h2><div class="table"><table><thead><tr><th>#</th><th>DATE</th><th>SOURCE DOCUMENT</th><th>PAGE RANGE</th><th>PAGES</th><th>VERIFICATION</th></tr></thead><tbody>ROWS</tbody></table></div><footer>Excluded files: EXCLUDED<br>Merged PDF SHA256: PDFHASH<br>Processed locally. Original files are unchanged. No OCR, ambiguous date inference, or active execution of PDF links/scripts is performed.</footer></main></html>'''
    replacements = {"MANIFEST": esc(report["manifest"]), "GENERATED": esc(report["generated_at"]), "CARDS": cards, "ROWS": rows, "EXCLUDED": esc(excluded), "PDFHASH": report["merged_pdf_sha256"]}
    document = re.sub(r"\b(MANIFEST|GENERATED|CARDS|ROWS|EXCLUDED|PDFHASH)\b", lambda match: replacements[match[0]], document)
    path.write_text(document, encoding="utf-8")


def merge_manifest(input_dir, manifest_path, output_dir):
    """Validate all rows first, then publish a PDF, page map, JSON, and HTML atomically."""
    folder = Path(input_dir).resolve()
    destination = Path(output_dir).absolute()
    if destination.is_symlink():
        raise WorkflowError("Output directory cannot be a symbolic link.")
    destination = destination.resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise WorkflowError("Output directory must be new or empty. Existing files are never overwritten.")
    if destination == folder:
        raise WorkflowError("Output directory must differ from the input folder.")
    documents, excluded, inventory = validate_manifest(folder, read_manifest(manifest_path))
    stage, writer = None, PdfWriter()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="6842_合并暂存_", dir=destination.parent))
        page_number, audit_docs = 1, []
        for item in documents:
            writer.append(item["_reader"], outline_item=f'{item["date"]} | {item["title"]}', import_outline=False)
            audit_docs.append({key: value for key, value in item.items() if not key.startswith("_")} | {"start_page": page_number, "end_page": page_number + item["pages"] - 1})
            page_number += item["pages"]
        writer.add_metadata({"/Title": "Chronological invoices and receipts", "/Creator": "6842 Local PDF Workflow"})
        with (stage / OUTPUTS["pdf"]).open("wb") as stream:
            writer.write(stream)
        merged_bytes = (stage / OUTPUTS["pdf"]).read_bytes()
        merged_reader = PdfReader(io.BytesIO(merged_bytes))
        if len(merged_reader.pages) != page_number - 1:
            raise WorkflowError("Output page-count verification failed; no files were published.")
        report = {"schema_version": 1, "status": "complete", "input_folder": folder.name,
                  "manifest": Path(manifest_path).name, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "counts": {"documents": len(audit_docs), "pages": page_number - 1, "excluded": len(excluded)},
                  "documents": audit_docs, "excluded_files": excluded, "outputs": dict(OUTPUTS),
                  "merged_pdf_sha256": sha256(merged_bytes)}
        with (stage / OUTPUTS["map"]).open("w", encoding="utf-8-sig", newline="") as stream:
            fields = ["filename", "date", "start_page", "end_page", "pages", "sha256"]
            csv_writer = csv.DictWriter(stream, fieldnames=fields)
            csv_writer.writeheader()
            for item in audit_docs:
                csv_writer.writerow({key: safe_csv(item[key]) if key == "filename" else item[key] for key in fields})
        (stage / OUTPUTS["json"]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        write_html(stage / OUTPUTS["html"], report)
        if {p.name for p in pdf_inventory(folder)} != inventory:
            raise WorkflowError("Input PDF inventory changed during merging; scan again. No files were published.")
        if destination.exists():
            destination.rmdir()  # Only empty directories can be removed; user files are never deleted.
        os.replace(stage, destination)
        stage = None
        return report
    except WorkflowError:
        raise
    except Exception as exc:
        raise WorkflowError(f"Merge failed; no final output files were published: {exc}") from exc
    finally:
        writer.close()
        if stage is not None and stage.parent.resolve() == destination.parent and stage.name.startswith("6842_合并暂存_"):
            shutil.rmtree(stage)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Review dates, sort, and merge local PDF invoices or receipts.")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="create a new reviewable CSV manifest")
    scan.add_argument("--input", required=True)
    scan.add_argument("--manifest", required=True)
    merge = commands.add_parser("merge", help="validate manifest and atomically publish a merged PDF")
    merge.add_argument("--input", required=True)
    merge.add_argument("--manifest", required=True)
    merge.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = scan_folder(args.input, args.manifest) if args.command == "scan" else merge_manifest(args.input, args.manifest, args.output)
        summary = {key: result[key] for key in (("ready", "review", "manifest") if args.command == "scan" else ("status", "counts", "outputs"))}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except WorkflowError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print("ERROR: Unexpected failure: " + str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
