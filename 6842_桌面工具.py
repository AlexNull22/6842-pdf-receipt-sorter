"""Tk desktop interface for the receipt workflow. Run --smoke-test for GUI QA."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser


def load_engine():
    source = Path(__file__).with_name("6842_票据合并.py")
    spec = importlib.util.spec_from_file_location("receipt_workflow_6842", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load workflow engine: {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ReceiptStudio:
    """Only date/include edits live here; validation belongs to the engine."""

    BG = "#f3f6fa"
    INK = "#152638"
    MUTED = "#65758a"
    BLUE = "#1767d2"

    def __init__(self, root: tk.Tk, engine, *, quiet: bool = False):
        self.root, self.engine, self.quiet = root, engine, quiet
        self.root.title("Receipt Batch Studio | PDF sorting & audit trail")
        self.root.geometry("1160x840")
        self.root.minsize(1000, 760)
        self.root.configure(bg=self.BG)
        self.jobs = queue.Queue()
        self.busy = False
        self.rows: list[dict] = []
        self.records: dict[str, dict] = {}
        self.headers: list[str] = []
        self.selected: str | None = None
        self.manifest_path: Path | None = None
        self.manifest_digest: str | None = None
        self.scanned_dir: Path | None = None
        self.output_dir: Path | None = None
        self.last_result: dict | None = None
        self.last_error: Exception | None = None
        self.edited_files: set[str] = set()
        self.dirty = False
        self.input_value = tk.StringVar()
        self.date_value = tk.StringVar()
        self.include_value = tk.BooleanVar(value=False)
        self.status_value = tk.StringVar(value="Select a folder to begin.")
        self.manifest_value = tk.StringVar(value="No manifest created yet")
        self.selection_value = tk.StringVar(value="Select a document to review its date and inclusion.")
        self.result_value = tk.StringVar(value="Your merged PDF and audit report will appear here.")
        self._style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(60, self._poll)

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=self.BG)
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background=self.BG, foreground=self.INK, font=("Segoe UI", -14))
        style.configure("Card.TLabel", background="white")
        style.configure("Muted.TLabel", foreground=self.MUTED, font=("Segoe UI", -12))
        style.configure("CardMuted.TLabel", background="white", foreground=self.MUTED, font=("Segoe UI", -12))
        style.configure("Section.TLabel", background="white", font=("Segoe UI", -15, "bold"))
        style.configure("TButton", font=("Segoe UI", -14), padding=(13, 8), borderwidth=0)
        style.configure("Primary.TButton", background=self.BLUE, foreground="white")
        style.map("Primary.TButton", background=[("active", "#1055b2"), ("disabled", "#c3d2e4")], foreground=[("disabled", "#f9fbfd")])
        style.configure("TEntry", padding=(8, 7), fieldbackground="white", font=("Segoe UI", -14))
        style.configure("TCheckbutton", background="white", font=("Segoe UI", -14))
        style.map("TCheckbutton", background=[("active", "white")])
        style.configure("Treeview", font=("Segoe UI", -14), rowheight=32, background="white", fieldbackground="white", borderwidth=0)
        style.configure("Treeview.Heading", background="#e9eff7", foreground="#35485f", font=("Segoe UI", -12, "bold"), padding=(8, 10), relief="flat")
        style.map("Treeview", background=[("selected", "#d9e9ff")], foreground=[("selected", self.INK)])
        style.configure("Horizontal.TProgressbar", background=self.BLUE, troughcolor="#dfe7f0", borderwidth=0, thickness=4)

    def _build(self):
        header = tk.Frame(self.root, bg="#12263c", padx=26, pady=17)
        header.pack(fill="x")
        tk.Label(header, text="RECEIPT BATCH STUDIO", bg="#12263c", fg="#82b8ff", font=("Segoe UI", -12, "bold")).pack(anchor="w")
        tk.Label(header, text="From loose receipts to one clear record.", bg="#12263c", fg="white", font=("Segoe UI", -27, "bold")).pack(anchor="w", pady=(4, 3))
        tk.Label(header, text="Sort PDF receipts by date, review uncertain files, and export a traceable bundle.", bg="#12263c", fg="#b9c9db", font=("Segoe UI", -14)).pack(anchor="w")
        body = ttk.Frame(self.root, padding=(24, 16))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        source = ttk.Frame(body, style="Card.TFrame", padding=(18, 13))
        source.grid(row=0, column=0, sticky="ew")
        ttk.Label(source, text="1   Select & scan", style="Section.TLabel").pack(anchor="w")
        source_row = ttk.Frame(source, style="Card.TFrame")
        source_row.pack(fill="x", pady=(9, 5))
        self.input_entry = ttk.Entry(source_row, textvariable=self.input_value, state="readonly")
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.browse_button = ttk.Button(source_row, text="Choose folder", command=self._choose_input)
        self.browse_button.pack(side="left", padx=(0, 8))
        self.scan_button = ttk.Button(source_row, text="Scan PDFs", style="Primary.TButton", command=self._scan_clicked)
        self.scan_button.pack(side="left")
        ttk.Label(source, textvariable=self.manifest_value, style="CardMuted.TLabel", wraplength=1000).pack(anchor="w")
        metrics = ttk.Frame(body)
        metrics.grid(row=1, column=0, sticky="ew", pady=12)
        self.metric_values = []
        for index, label in enumerate(("DOCUMENTS", "AUTO-DATED", "REVIEW FLAGS", "INCLUDED PAGES")):
            card = ttk.Frame(metrics, style="Card.TFrame", padding=(16, 10))
            card.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 8, 0))
            metrics.columnconfigure(index, weight=1)
            value = tk.StringVar(value="—")
            self.metric_values.append(value)
            ttk.Label(card, text=label, style="CardMuted.TLabel").pack(anchor="w")
            tk.Label(card, textvariable=value, bg="white", fg=self.BLUE if index != 2 else "#ae6812", font=("Segoe UI", -27, "bold")).pack(anchor="w")
        review = ttk.Frame(body, style="Card.TFrame", padding=(18, 13))
        review.grid(row=2, column=0, sticky="nsew")
        title_row = ttk.Frame(review, style="Card.TFrame")
        title_row.pack(fill="x")
        ttk.Label(title_row, text="2   Review the manifest", style="Section.TLabel").pack(side="left")
        ttk.Label(title_row, text="Original PDF files stay unchanged", style="CardMuted.TLabel").pack(side="right")
        tree_frame = ttk.Frame(review, style="Card.TFrame")
        tree_frame.pack(fill="both", expand=True, pady=(10, 8))
        columns = ("filename", "date", "include", "status", "pages")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse", height=7)
        for name, title, width, anchor in (
            ("filename", "DOCUMENT", 420, "w"), ("date", "DATE", 120, "center"),
            ("include", "INCLUDE", 85, "center"), ("status", "STATUS", 105, "center"),
            ("pages", "PAGES", 70, "center"),
        ):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=65, anchor=anchor, stretch=name == "filename")
        self.tree.tag_configure("review", foreground="#9e641b")
        self.tree.tag_configure("ready", foreground="#28634e")
        self.tree.tag_configure("edited", foreground=self.BLUE)
        scroll_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll_y.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll_y.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        ttk.Label(review, textvariable=self.selection_value, style="CardMuted.TLabel", wraplength=1000).pack(anchor="w", pady=(0, 7))
        editor = ttk.Frame(review, style="Card.TFrame")
        editor.pack(fill="x")
        ttk.Label(editor, text="Receipt date", style="Card.TLabel").pack(side="left")
        self.date_entry = ttk.Entry(editor, textvariable=self.date_value, width=16)
        self.date_entry.pack(side="left", padx=(8, 5))
        ttk.Label(editor, text="YYYY-MM-DD", style="CardMuted.TLabel").pack(side="left", padx=(0, 18))
        self.include_check = ttk.Checkbutton(editor, text="Include in bundle", variable=self.include_value)
        self.include_check.pack(side="left")
        self.apply_button = ttk.Button(editor, text="Apply edit", command=self._apply_edit)
        self.apply_button.pack(side="left", padx=12)
        self.save_button = ttk.Button(editor, text="Save manifest", command=self._save_clicked)
        self.save_button.pack(side="right")
        ttk.Label(review, text="Uncertain dates start excluded. Set a date and include the file after checking it; the workflow validates the saved manifest before merging.", style="CardMuted.TLabel", wraplength=1000).pack(anchor="w", pady=(9, 0))
        export = ttk.Frame(body, style="Card.TFrame", padding=(18, 12))
        export.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        export_top = ttk.Frame(export, style="Card.TFrame")
        export_top.pack(fill="x")
        ttk.Label(export_top, text="3   Build your bundle", style="Section.TLabel").pack(side="left")
        self.merge_button = ttk.Button(export_top, text="Save & merge PDFs", style="Primary.TButton", command=self._merge_clicked)
        self.merge_button.pack(side="right")
        ttk.Label(export, textvariable=self.result_value, style="CardMuted.TLabel", wraplength=1000).pack(anchor="w", pady=(5, 6))
        links = ttk.Frame(export, style="Card.TFrame")
        links.pack(fill="x")
        self.pdf_button = ttk.Button(links, text="Open merged PDF", command=lambda: self._open_output("pdf"))
        self.pdf_button.pack(side="left", padx=(0, 8))
        self.report_button = ttk.Button(links, text="Open audit report", command=lambda: self._open_output("html"))
        self.report_button.pack(side="left")
        self.progress = ttk.Progressbar(body, mode="indeterminate")
        self.progress.grid(row=4, column=0, sticky="ew", pady=(13, 5))
        ttk.Label(body, textvariable=self.status_value, style="Muted.TLabel", wraplength=1080).grid(row=5, column=0, sticky="w")
        self._set_buttons()

    def _set_buttons(self):
        available = self.manifest_path is not None and bool(self.rows) and not self.busy
        for widget in (self.browse_button, self.scan_button):
            widget.configure(state="disabled" if self.busy else "normal")
        for widget in (self.save_button, self.merge_button):
            widget.configure(state="normal" if available else "disabled")
        for widget in (self.date_entry, self.include_check, self.apply_button):
            widget.configure(state="normal" if available and self.selected is not None else "disabled")
        for widget, key in ((self.pdf_button, "pdf"), (self.report_button, "html")):
            ready = not self.busy and self.last_result is not None and bool(self.last_result.get("outputs", {}).get(key))
            widget.configure(state="normal" if ready else "disabled")

    def _choose_input(self):
        path = filedialog.askdirectory(parent=self.root, title="Choose the folder containing PDF receipts", mustexist=True)
        if path:
            self.input_value.set(path)
            if self.scanned_dir is not None and Path(path).resolve() != self.scanned_dir:
                self.status_value.set("Folder changed. Scan it to create a fresh manifest.")

    def _scan_clicked(self):
        if not self.input_value.get():
            self._choose_input()
        if not self.input_value.get():
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="Create a NEW review manifest", defaultextension=".csv", initialfile="6842_排序清单.csv", filetypes=[("CSV manifest", "*.csv")], confirmoverwrite=False)
        if not path:
            return
        if Path(path).exists():
            self._error("Create manifest", RuntimeError("That manifest already exists. Choose a new filename to protect your previous review."))
            return
        self._start_scan(Path(self.input_value.get()), Path(path))

    def _start_scan(self, input_dir: Path, manifest_path: Path):
        self.input_value.set(str(input_dir))
        self._run("Scanning PDF files…", lambda: self.engine.scan_folder(input_dir, manifest_path), lambda result: self._scan_finished(result, input_dir, manifest_path))

    def _scan_finished(self, result: dict, input_dir: Path, manifest_path: Path):
        self.scanned_dir = input_dir.resolve()
        self.manifest_path = manifest_path.resolve()
        raw = self.manifest_path.read_bytes()
        self.manifest_digest = hashlib.sha256(raw).hexdigest()
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        self.headers = list(reader.fieldnames or [])
        self.rows = list(reader)
        self.records = {str(record["filename"]): record for record in result["records"]}
        self.selected = None
        self.edited_files.clear()
        self.dirty = False
        self.last_result = None
        self.output_dir = None
        self.manifest_value.set(f"Manifest: {self.manifest_path}")
        self.result_value.set("Ready to export. Select a new or empty output folder when merging.")
        self.selection_value.set("Select a document to review its date and inclusion.")
        self.date_value.set("")
        self.include_value.set(False)
        self._refresh_table()
        self.status_value.set(f"Scanned {len(self.rows)} PDFs. Review uncertain files, then save and merge.")

    @staticmethod
    def _included(row: dict) -> bool:
        return str(row.get("include", "")).strip().lower() in ("true", "1", "yes")

    def _refresh_table(self):
        selected = self.selected
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(self.rows):
            record = self.records.get(row["filename"], {})
            status = "edited" if row["filename"] in self.edited_files else record.get("status", "review")
            self.tree.insert("", "end", iid=str(index), values=(row["filename"], row.get("date") or "—", "Yes" if self._included(row) else "No", status.title(), record.get("pages", "—")), tags=(status,))
        if selected is not None and int(selected) < len(self.rows):
            self.tree.selection_set(selected)
            self.tree.focus(selected)
        counts = [len(self.rows), sum(record.get("status") == "ready" for record in self.records.values()), sum(record.get("status") == "review" for record in self.records.values()), sum(int(self.records.get(row["filename"], {}).get("pages", 0)) for row in self.rows if self._included(row))]
        for variable, value in zip(self.metric_values, counts):
            variable.set(str(value))
        self._set_buttons()

    def _capture_edit(self):
        if self.selected is None:
            return
        row = self.rows[int(self.selected)]
        date = self.date_value.get().strip()
        include = "true" if self.include_value.get() else "false"
        if date != row.get("date", "") or include != str(row.get("include", "")).lower():
            row["date"], row["include"] = date, include
            self.edited_files.add(row["filename"])
            self.dirty = True
            if self.tree.exists(self.selected):
                self.tree.item(self.selected, values=(row["filename"], date or "—", "Yes" if self._included(row) else "No", "Edited", self.records.get(row["filename"], {}).get("pages", "—")), tags=("edited",))
            self.metric_values[3].set(str(sum(int(self.records.get(item["filename"], {}).get("pages", 0)) for item in self.rows if self._included(item))))

    def _selection_changed(self, _event=None):
        chosen = self.tree.selection()
        if not chosen or chosen[0] == self.selected:
            return
        self._capture_edit()
        self.selected = chosen[0]
        row = self.rows[int(self.selected)]
        self.date_value.set(row.get("date", ""))
        self.include_value.set(self._included(row))
        record = self.records.get(row["filename"], {})
        issues = record.get("issues") or []
        self.selection_value.set(f"{row['filename']}" + ("  •  " + "; ".join(map(str, issues)) if issues else "  •  Date detected from labelled PDF text"))
        self._set_buttons()

    def _apply_edit(self):
        self._capture_edit()
        self._refresh_table()
        self.status_value.set("Review updated. Save the manifest or continue editing; dates are checked when merging.")

    def _save_manifest(self):
        if self.manifest_path is None:
            raise RuntimeError("Scan a folder before saving the manifest.")
        self._capture_edit()
        current = hashlib.sha256(self.manifest_path.read_bytes()).hexdigest()
        if current != self.manifest_digest:
            raise RuntimeError("The CSV was changed outside this window. Its contents have been preserved. Scan into a new manifest before continuing.")
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=self.headers, lineterminator="\n")
        writer.writeheader()
        writer.writerows(self.rows)
        data = buffer.getvalue().encode("utf-8-sig")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", prefix="6842_清单保存_", suffix=".tmp", dir=self.manifest_path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.manifest_path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        self.manifest_digest = hashlib.sha256(data).hexdigest()
        self.dirty = False
        self._refresh_table()
        self.status_value.set("Manifest saved. The workflow will validate the file before exporting.")

    def _save_clicked(self):
        try:
            self._save_manifest()
        except Exception as exc:
            self._error("Save manifest", exc)

    def _merge_clicked(self):
        try:
            self._check_scanned_input()
            self._save_manifest()
        except Exception as exc:
            self._error("Prepare merge", exc)
            return
        output = filedialog.askdirectory(parent=self.root, title="Choose a NEW or EMPTY output folder", mustexist=False)
        if output:
            try:
                self._start_merge(Path(output))
            except Exception as exc:
                self._error("Prepare merge", exc)

    def _check_scanned_input(self):
        if self.scanned_dir is None or Path(self.input_value.get()).resolve() != self.scanned_dir:
            raise RuntimeError("The selected folder has changed. Scan it into a new manifest before merging.")

    def _start_merge(self, output_dir: Path):
        self._check_scanned_input()
        self._save_manifest()
        self.last_result = None
        self.output_dir = None
        self.result_value.set("Building the merged PDF, source map, and audit report…")
        self._run("Validating and merging PDFs…", lambda: self.engine.merge_manifest(self.scanned_dir, self.manifest_path, output_dir), lambda result: self._merge_finished(result, output_dir))

    def _merge_finished(self, result: dict, output_dir: Path):
        self.last_result = result
        self.output_dir = output_dir.resolve()
        counts = result["counts"]
        self.result_value.set(f"Complete • {counts['documents']} documents • {counts['pages']} pages • {counts['excluded']} excluded\nSaved to {self.output_dir}")
        self.status_value.set("Bundle complete. Open the merged PDF or its audit report below.")

    def _open_output(self, key: str):
        if self.last_result is None or self.output_dir is None:
            return
        try:
            path = self.output_dir / self.last_result["outputs"][key]
            if not path.is_file():
                raise FileNotFoundError(f"Output not found: {path}")
            if sys.platform == "win32":
                os.startfile(str(path))
            else:
                webbrowser.open(path.as_uri())
        except Exception as exc:
            self._error("Open output", exc)

    def _run(self, status: str, operation, on_success):
        if self.busy:
            return
        self.busy = True
        self.last_error = None
        self.status_value.set(status)
        self.progress.start(12)
        self._set_buttons()

        def worker():
            try:
                self.jobs.put((True, operation(), on_success))
            except Exception as exc:
                self.jobs.put((False, exc, on_success))

        threading.Thread(target=worker, name="6842-pdf-worker", daemon=True).start()

    def _poll(self):
        try:
            success, value, callback = self.jobs.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.progress.stop()
            if success:
                try:
                    callback(value)
                except Exception as exc:
                    self._error("Read workflow result", exc)
            else:
                self._error("Workflow needs attention", value)
            self._set_buttons()
        self.root.after(60, self._poll)

    def _error(self, title: str, error: Exception):
        self.last_error = error
        issues = getattr(error, "issues", None)
        detail = "\n".join(map(str, issues)) if issues else str(error)
        self.status_value.set(f"{title}: {detail.splitlines()[0] if detail else type(error).__name__}")
        if not self.quiet:
            messagebox.showerror(title, detail, parent=self.root)

    def _close(self):
        if self.busy:
            if not messagebox.askyesno("Workflow running", "A workflow is still running. Close this window?", parent=self.root):
                return
        self._capture_edit()
        if self.dirty and not messagebox.askyesno("Unsaved review", "Close without saving your latest manifest edits?", parent=self.root):
            return
        self.root.destroy()


def smoke_test(engine) -> int:
    """Drive real worker callbacks and the Tk event loop with disposable PDFs."""
    from reportlab.pdfgen import canvas
    from pypdf import PdfReader

    result = {"status": "failed", "events": []}
    with tempfile.TemporaryDirectory(prefix="6842_gui_smoke_") as temporary:
        base = Path(temporary)
        inputs = base / "input"
        inputs.mkdir()
        for name in ("2026-10-01 Office.pdf", "Undated receipt.pdf"):
            pdf = canvas.Canvas(str(inputs / name))
            pdf.drawString(72, 740, name)
            if name.startswith("2026"):
                pdf.drawString(72, 715, "Receipt Date: 2026-10-01")
            pdf.save()
        manifest = base / "review.csv"
        output = base / "output"
        root = tk.Tk()
        root.withdraw()
        app = ReceiptStudio(root, engine, quiet=True)
        started = time.monotonic()
        phase = "scan"

        def finish(error=None):
            if error is not None:
                result["error"] = f"{type(error).__name__}: {error}"
                result["traceback"] = traceback.format_exc().splitlines()
            root.destroy()

        def advance():
            nonlocal phase
            try:
                if time.monotonic() - started > 35:
                    raise TimeoutError("GUI smoke test exceeded 35 seconds")
                if app.last_error is not None:
                    raise app.last_error
                if app.busy:
                    root.after(30, advance)
                    return
                if phase == "scan":
                    assert len(app.rows) == 2, "Expected two scanned documents"
                    result["events"].append("scan_callback")
                    index = next(i for i, row in enumerate(app.rows) if row["filename"] == "Undated receipt.pdf")
                    assert not app._included(app.rows[index]), "Undated receipt should start excluded"
                    app.tree.selection_set(str(index))
                    app._selection_changed()
                    app.date_value.set("2026-10-02")
                    app.include_value.set(True)
                    app._apply_edit()
                    app._save_manifest()
                    result["events"].append("edit_and_save_manifest")
                    with manifest.open(encoding="utf-8-sig", newline="") as handle:
                        saved = list(csv.DictReader(handle))
                    edited = next(row for row in saved if row["filename"] == "Undated receipt.pdf")
                    assert edited["date"] == "2026-10-02" and edited["include"] == "true"
                    phase = "merge"
                    app._start_merge(output)
                elif phase == "merge":
                    assert app.last_result is not None, "Merge callback did not complete"
                    assert app.last_result["status"] == "complete"
                    assert app.last_result["counts"]["documents"] == 2
                    assert app.last_result["counts"]["pages"] == 2
                    for name in app.last_result["outputs"].values():
                        assert (output / name).is_file(), f"Missing output: {name}"
                    assert len(PdfReader(output / app.last_result["outputs"]["pdf"]).pages) == 2
                    assert str(app.pdf_button.cget("state")) == "normal"
                    assert str(app.report_button.cget("state")) == "normal"
                    result.update(status="passed", counts=app.last_result["counts"], gui_event_loop=True)
                    result["events"].append("merge_callback_and_output_buttons")
                    finish()
                    return
                root.after(30, advance)
            except Exception as exc:
                finish(exc)

        root.after(0, lambda: app._start_scan(inputs, manifest))
        root.after(40, advance)
        root.mainloop()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Receipt Batch Studio desktop interface")
    parser.add_argument("--smoke-test", action="store_true", help="Run an automatic GUI event-loop test and exit")
    args = parser.parse_args()
    try:
        engine = load_engine()
        if args.smoke_test:
            return smoke_test(engine)
        root = tk.Tk()
        ReceiptStudio(root, engine)
        root.mainloop()
        return 0
    except Exception as exc:
        if args.smoke_test:
            print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        else:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Receipt Batch Studio", str(exc), parent=root)
            root.destroy()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
