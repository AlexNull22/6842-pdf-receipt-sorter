"""票据排序合并工具的独立业务验收；样本及输出全部使用临时目录。"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


engine = _load_module("invoice_merger_6842", "6842_票据合并.py")
samples = _load_module("invoice_samples_6842", "6842_生成示例.py")
FIELDS = ["filename", "date", "include", "title", "sha256"]


class InvoiceWorkflowAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="6842_验收_")
        self.root = Path(self.temporary.name)
        self.input = self.root / "6842_输入"
        self.input.mkdir()
        self.manifest = self.root / "6842_清单.csv"
        self.addCleanup(self.temporary.cleanup)

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def pdf(self, filename="6842_票据.pdf", date_text="Invoice Date: 2026-01-01", marker="ALPHA", pages=1, folder=None):
        path = (self.input if folder is None else Path(folder)) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        pdf = canvas.Canvas(str(path), invariant=1, pageCompression=1)
        pdf.setTitle("Synthetic acceptance fixture")
        for index in range(1, pages + 1):
            pdf.setFont("Helvetica", 13)
            pdf.drawString(45, 790, "SYNTHETIC - PORTFOLIO DEMO")
            for line_number, line in enumerate(date_text.splitlines()):
                pdf.drawString(45, 755 - line_number * 24, line)
            pdf.drawString(45, 620, f"MARKER_{marker}_PAGE_{index}")
            pdf.showPage()
        pdf.save()
        return path

    def read_manifest(self):
        with self.manifest.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames, FIELDS)
            return list(reader)

    def write_manifest(self, rows, fields=None):
        with self.manifest.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS if fields is None else fields)
            writer.writeheader()
            writer.writerows(rows)

    def scan(self):
        return engine.scan_folder(self.input, self.manifest)

    def merge(self, output_name="6842_输出"):
        output = self.root / output_name
        result = engine.merge_manifest(self.input, self.manifest, output)
        self.assertEqual(result["status"], "complete")
        paths = {key: output / value for key, value in result["outputs"].items()}
        self.assertEqual(set(paths), {"pdf", "map", "json", "html"})
        for path in paths.values():
            self.assertTrue(path.is_file(), f"缺少输出：{path.name}")
            self.assertTrue(path.name.startswith("6842_"))
            self.assertEqual(path.parent, output)
        return result, paths

    def assert_blocked(self, output_name="6842_拒绝输出"):
        output = self.root / output_name
        before = {p.name: self.digest(p) for p in self.input.glob("*.pdf")}
        with self.assertRaises(engine.WorkflowError) as caught:
            engine.merge_manifest(self.input, self.manifest, output)
        self.assertIsInstance(caught.exception.issues, list)
        self.assertTrue(str(caught.exception), "阻止运行的原因不能为空")
        self.assertFalse(output.exists() and any(output.iterdir()), "失败运行留下部分输出")
        self.assertEqual(before, {p.name: self.digest(p) for p in self.input.glob("*.pdf")})
        return caught.exception

    def test_scan_accepts_explicit_dates_and_reviews_ambiguous_values(self):
        cases = [
            ("6842_ISO票据.pdf", "Invoice Date: 2026-01-03", "2026-01-03"),
            ("6842_英文收据.pdf", "Receipt Date: January 2, 2026", "2026-01-02"),
            ("6842_英文日期.pdf", "Date: 1 January 2026", "2026-01-01"),
            ("6842_斜线日期.pdf", "Invoice Date: 01/02/2026", ""),
            ("6842_冲突日期.pdf", "Invoice Date: 2026-01-04\nReceipt Date: 2026-01-05", ""),
            ("6842_非票据日期.pdf", "Generated: 2026-01-06", ""),
        ]
        hashes = {}
        for filename, text, _ in cases:
            path = self.pdf(filename, text)
            hashes[filename] = self.digest(path)
        result = self.scan()
        self.assertEqual((result["ready"], result["review"]), (3, 3))
        self.assertTrue(self.manifest.read_bytes().startswith(b"\xef\xbb\xbf"))
        actual = {row["filename"]: row for row in result["records"]}
        manifest_rows = {row["filename"]: row for row in self.read_manifest()}
        for filename, _, expected_date in cases:
            row = actual[filename]
            self.assertEqual(row["date"], expected_date)
            self.assertEqual(row["status"], "ready" if expected_date else "review")
            self.assertEqual(row["include"], bool(expected_date))
            self.assertEqual(row["sha256"], hashes[filename])
            self.assertEqual(manifest_rows[filename]["include"], "true" if expected_date else "false")
            if not expected_date:
                self.assertTrue(row["issues"])
        self.assertEqual(hashes, {p.name: self.digest(p) for p in self.input.glob("*.pdf")})

    def test_merge_sorts_dates_then_names_and_preserves_all_page_markers(self):
        self.pdf("6842_票据_C.pdf", "Invoice Date: 2026-01-03", "C")
        self.pdf("6842_票据_B.pdf", "Invoice Date: 2026-01-01", "B", pages=2)
        self.pdf("6842_票据_A.pdf", "Invoice Date: 2026-01-01", "A")
        hashes = {p.name: self.digest(p) for p in self.input.glob("*.pdf")}
        self.scan()
        report, paths = self.merge()
        self.assertEqual(report["counts"], {"documents": 3, "pages": 4, "excluded": 0})
        self.assertEqual([row["filename"] for row in report["documents"]], ["6842_票据_A.pdf", "6842_票据_B.pdf", "6842_票据_C.pdf"])
        self.assertEqual([(row["start_page"], row["end_page"]) for row in report["documents"]], [(1, 1), (2, 3), (4, 4)])
        reader = PdfReader(paths["pdf"])
        markers = ["MARKER_A_PAGE_1", "MARKER_B_PAGE_1", "MARKER_B_PAGE_2", "MARKER_C_PAGE_1"]
        self.assertEqual(len(reader.pages), len(markers))
        for page, marker in zip(reader.pages, markers):
            self.assertIn(marker, page.extract_text())
        outline = [entry for entry in reader.outline if not isinstance(entry, list)]
        self.assertEqual(len(outline), 3)
        self.assertEqual([reader.get_destination_page_number(entry) for entry in outline], [0, 1, 3])
        saved = json.loads(paths["json"].read_text(encoding="utf-8-sig"))
        self.assertEqual(saved["documents"], report["documents"])
        with paths["map"].open(encoding="utf-8-sig", newline="") as stream:
            mapping = list(csv.DictReader(stream))
        self.assertEqual([row["filename"] for row in mapping], [row["filename"] for row in report["documents"]])
        self.assertEqual([(int(row["start_page"]), int(row["end_page"])) for row in mapping], [(1, 1), (2, 3), (4, 4)])
        self.assertNotIn(str(self.root), paths["json"].read_text(encoding="utf-8-sig"))
        self.assertEqual(hashes, {p.name: self.digest(p) for p in self.input.glob("*.pdf")})

    def test_review_date_can_be_explicitly_corrected_in_manifest(self):
        self.pdf(date_text="Invoice Date: 01/02/2026")
        self.assertEqual(self.scan()["review"], 1)
        rows = self.read_manifest()
        self.assertEqual((rows[0]["date"], rows[0]["include"]), ("", "false"))
        rows[0].update(date="2026-02-01", include="true", title="Reviewed date confirmed")
        self.write_manifest(rows)
        report, _ = self.merge()
        self.assertEqual(report["documents"][0]["date"], "2026-02-01")
        self.assertEqual(report["documents"][0]["title"], "Reviewed date confirmed")

    def test_invalid_included_dates_block_publication(self):
        self.pdf()
        self.scan()
        rows = self.read_manifest()
        for index, bad_date in enumerate(("2026-02-30", "01/02/2026", "2026-1-2", "")):
            with self.subTest(date=bad_date):
                rows[0]["date"] = bad_date
                self.write_manifest(rows)
                self.assert_blocked(f"6842_日期拒绝_{index}")

    def test_corrupt_and_encrypted_pdfs_require_review_and_cannot_be_included(self):
        corrupt = self.input / "6842_损坏票据.pdf"
        corrupt.write_bytes(b"%PDF-1.7\nDeliberately corrupt synthetic fixture\n")
        source = self.pdf("6842_加密前.pdf")
        writer = PdfWriter()
        writer.append(PdfReader(source))
        writer.encrypt("synthetic-password")
        encrypted = self.input / "6842_加密票据.pdf"
        with encrypted.open("wb") as stream:
            writer.write(stream)
        writer.close()
        source.unlink()
        scan = self.scan()
        self.assertEqual((scan["ready"], scan["review"]), (0, 2))
        rows = self.read_manifest()
        self.assertTrue(all(row["include"] == "false" for row in rows))
        for index in range(2):
            selected = [dict(row, include="true" if position == index else "false", date="2026-01-01") for position, row in enumerate(rows)]
            self.write_manifest(selected)
            self.assert_blocked(f"6842_不可读拒绝_{index}")

    def test_missing_file_blocks_publication(self):
        path = self.pdf()
        self.scan()
        path.unlink()
        self.assert_blocked()

    def test_hash_change_blocks_publication(self):
        self.pdf()
        self.scan()
        self.pdf(marker="REPLACED")
        self.assert_blocked()

    def test_path_traversal_and_absolute_paths_are_rejected(self):
        self.pdf()
        self.scan()
        legitimate = self.read_manifest()
        outside = self.pdf("6842_外部票据.pdf", folder=self.root)
        nested = self.pdf("6842_嵌套票据.pdf", folder=self.input / "6842_子目录")
        bad_paths = ["../" + outside.name, "..\\" + outside.name, str(outside), "6842_子目录/" + nested.name]
        for index, bad_path in enumerate(bad_paths):
            with self.subTest(filename=bad_path):
                actual = nested if index == 3 else outside
                self.write_manifest(legitimate + [{"filename": bad_path, "date": "2026-01-01", "include": "true", "title": "Unsafe path", "sha256": self.digest(actual)}])
                self.assert_blocked(f"6842_路径拒绝_{index}")

    def test_duplicate_filename_in_manifest_blocks_publication(self):
        self.pdf()
        self.scan()
        rows = self.read_manifest()
        self.write_manifest(rows + [rows[0]])
        self.assert_blocked()

    def test_duplicate_document_bytes_block_publication(self):
        source = self.pdf("6842_票据甲.pdf")
        shutil.copyfile(source, self.input / "6842_票据乙.pdf")
        self.scan()
        self.assert_blocked()

    def test_unlisted_current_pdf_blocks_publication(self):
        self.pdf()
        self.scan()
        self.pdf("6842_新增票据.pdf", marker="NEW")
        self.assert_blocked()

    def test_excluded_duplicate_is_omitted_and_counted(self):
        source = self.pdf("6842_保留票据.pdf")
        shutil.copyfile(source, self.input / "6842_排除副本.pdf")
        self.scan()
        rows = self.read_manifest()
        for row in rows:
            if row["filename"] == "6842_排除副本.pdf":
                row.update(include="false", date="")
        self.write_manifest(rows)
        report, paths = self.merge()
        self.assertEqual(report["counts"], {"documents": 1, "pages": 1, "excluded": 1})
        self.assertEqual(report["documents"][0]["filename"], source.name)
        self.assertEqual(len(PdfReader(paths["pdf"]).pages), 1)

    def test_all_excluded_manifest_blocks_publication(self):
        self.pdf()
        self.scan()
        rows = self.read_manifest()
        rows[0]["include"] = "false"
        self.write_manifest(rows)
        self.assert_blocked()

    def test_existing_output_is_preserved_without_partial_files(self):
        source = self.pdf()
        source_hash = self.digest(source)
        self.scan()
        output = self.root / "6842_已有输出"
        output.mkdir()
        sentinel = output / "6842_原有文件.txt"
        sentinel.write_text("keep unchanged", encoding="utf-8")
        with self.assertRaises(engine.WorkflowError):
            engine.merge_manifest(self.input, self.manifest, output)
        self.assertEqual([path.name for path in output.iterdir()], [sentinel.name])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep unchanged")
        self.assertEqual(self.digest(source), source_hash)

    def test_scanning_never_overwrites_reviewed_manifest(self):
        self.pdf()
        self.scan()
        rows = self.read_manifest()
        rows[0]["title"] = "Manually reviewed title"
        self.write_manifest(rows)
        previous = self.manifest.read_bytes()
        with self.assertRaises(engine.WorkflowError):
            self.scan()
        self.assertEqual(self.manifest.read_bytes(), previous)

    def test_invalid_manifest_header_and_include_value_block_publication(self):
        self.pdf()
        self.scan()
        rows = self.read_manifest()
        rows[0]["include"] = "maybe"
        self.write_manifest(rows)
        self.assert_blocked("6842_布尔值拒绝")
        rows[0]["include"] = "true"
        self.write_manifest([{key: value for key, value in rows[0].items() if key != "sha256"}], fields=FIELDS[:-1])
        self.assert_blocked("6842_列名拒绝")

    def test_untrusted_title_is_escaped_in_html_and_preserved_in_outline(self):
        self.pdf()
        self.scan()
        rows = self.read_manifest()
        payload = '<img src=x onerror="alert(6842)"><script>alert(6842)</script>'
        rows[0]["title"] = payload
        self.write_manifest(rows)
        report, paths = self.merge()
        document = paths["html"].read_text(encoding="utf-8-sig")
        self.assertEqual(report["documents"][0]["title"], payload)
        self.assertNotIn(payload, document)
        self.assertNotIn("<img src=x", document)
        self.assertNotIn("<script>alert(6842)", document)
        self.assertIn("&lt;img src=x", document)
        reader = PdfReader(paths["pdf"])
        self.assertIn(payload, reader.outline[0].title)

    def test_generator_is_byte_reproducible_and_refuses_overwrite(self):
        first = self.root / "6842_第一批"
        second = self.root / "6842_第二批"
        left = samples.generate_samples(first, count=8)
        right = samples.generate_samples(second, count=8)
        self.assertEqual(left, right)
        for item in left["documents"]:
            self.assertEqual((first / item["filename"]).read_bytes(), (second / item["filename"]).read_bytes())
        with self.assertRaises(ValueError):
            samples.generate_samples(first, count=8)

    def test_300_document_batch_has_312_sorted_traceable_pages(self):
        generated = samples.generate_samples(self.input, count=300)
        self.assertEqual((generated["count"], generated["pages"]), (300, 312))
        self.assertEqual(len(generated["documents"]), 300)
        self.assertEqual(sum(item["pages"] == 2 for item in generated["documents"]), 12)
        self.assertEqual({item["date"] for item in generated["documents"]}, {f"2026-01-{day:02d}" for day in range(1, 32)})
        self.assertNotEqual([item["date"] for item in generated["documents"]], sorted(item["date"] for item in generated["documents"]))
        self.assertTrue(all(item["filename"].startswith("6842_") for item in generated["documents"]))
        scan = self.scan()
        self.assertEqual((scan["ready"], scan["review"]), (300, 0))
        report, paths = self.merge()
        self.assertEqual(report["counts"], {"documents": 300, "pages": 312, "excluded": 0})
        expected = sorted(generated["documents"], key=lambda item: (item["date"], item["filename"]))
        self.assertEqual([item["filename"] for item in report["documents"]], [item["filename"] for item in expected])
        reader = PdfReader(paths["pdf"])
        self.assertEqual(len(reader.pages), 312)
        next_page = 1
        for actual, original in zip(report["documents"], expected):
            self.assertEqual(actual["start_page"], next_page)
            self.assertEqual(actual["end_page"], next_page + original["pages"] - 1)
            number = int(Path(original["filename"]).stem.rsplit("_", 1)[-1])
            for offset in range(original["pages"]):
                page_text = reader.pages[next_page + offset - 1].extract_text()
                self.assertIn(f"DOC-6842-{number:04d}", page_text)
                self.assertIn(f"SOURCE PAGE {offset + 1}/{original['pages']}", page_text)
                self.assertIn("SYNTHETIC - PORTFOLIO DEMO", page_text)
            self.assertEqual(self.digest(self.input / original["filename"]), original["sha256"])
            next_page += original["pages"]
        self.assertEqual(next_page, 313)
        self.assertEqual(len([entry for entry in reader.outline if not isinstance(entry, list)]), 300)


if __name__ == "__main__":
    unittest.main(verbosity=2)
