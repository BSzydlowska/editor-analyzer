from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path

import customtkinter as ctk

import analyzer
from analyzer import AnalysisError, AnalysisResult

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

_DEFAULT_THRESHOLD = 3.0
_MIN_THRESHOLD = 0.5
_MAX_THRESHOLD = 30.0

_DEFAULT_TOP_DB = 30
_MIN_TOP_DB = 10
_MAX_TOP_DB = 60


class EditorAnalyzerApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.title("EditorAnalyzer")
        self.geometry("900x640")
        self.minsize(700, 500)

        self._aaf_path: Path | None = None
        self._threshold: float = _DEFAULT_THRESHOLD
        self._top_db: int = _DEFAULT_TOP_DB
        self._running = False

        self._build_ui()

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)  # results row expands

        # ── top bar: file picker ──────────────────────────────────────────
        top = ctk.CTkFrame(self)
        top.grid(row=0, column=0, padx=16, pady=(16, 4), sticky="ew")
        top.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(top, text="Otwórz plik AAF…", command=self._pick_file).grid(
            row=0, column=0, padx=(8, 4), pady=8
        )
        self._file_label = ctk.CTkLabel(top, text="Nie wybrano pliku", anchor="w")
        self._file_label.grid(row=0, column=1, padx=4, pady=8, sticky="ew")

        self._analyze_btn = ctk.CTkButton(
            top, text="Analizuj", fg_color="green", command=self._analyze
        )
        self._analyze_btn.grid(row=0, column=2, padx=(4, 8), pady=8)

        # ── threshold row (FR-002) ────────────────────────────────────────
        threshold_row = ctk.CTkFrame(self)
        threshold_row.grid(row=1, column=0, padx=16, pady=(0, 2), sticky="ew")
        threshold_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(threshold_row, text="Minimalna długość pauzy (s):").grid(
            row=0, column=0, padx=(12, 8), pady=8
        )
        self._slider = ctk.CTkSlider(
            threshold_row,
            from_=_MIN_THRESHOLD,
            to=_MAX_THRESHOLD,
            command=self._on_slider,
        )
        self._slider.set(_DEFAULT_THRESHOLD)
        self._slider.grid(row=0, column=1, padx=4, pady=8, sticky="ew")

        self._threshold_entry = ctk.CTkEntry(threshold_row, width=60, justify="center")
        self._threshold_entry.insert(0, f"{_DEFAULT_THRESHOLD:.1f}")
        self._threshold_entry.grid(row=0, column=2, padx=(4, 12), pady=8)
        self._threshold_entry.bind("<Return>", self._on_entry)
        self._threshold_entry.bind("<FocusOut>", self._on_entry)

        # ── top_db row ────────────────────────────────────────────────────
        top_db_row = ctk.CTkFrame(self)
        top_db_row.grid(row=2, column=0, padx=16, pady=(0, 4), sticky="ew")
        top_db_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top_db_row, text="Czułość wykrywania ciszy (dB):").grid(
            row=0, column=0, padx=(12, 8), pady=8
        )
        self._top_db_slider = ctk.CTkSlider(
            top_db_row,
            from_=_MIN_TOP_DB,
            to=_MAX_TOP_DB,
            number_of_steps=_MAX_TOP_DB - _MIN_TOP_DB,
            command=self._on_top_db_slider,
        )
        self._top_db_slider.set(_DEFAULT_TOP_DB)
        self._top_db_slider.grid(row=0, column=1, padx=4, pady=8, sticky="ew")

        self._top_db_entry = ctk.CTkEntry(top_db_row, width=60, justify="center")
        self._top_db_entry.insert(0, str(_DEFAULT_TOP_DB))
        self._top_db_entry.grid(row=0, column=2, padx=(4, 12), pady=8)
        self._top_db_entry.bind("<Return>", self._on_top_db_entry)
        self._top_db_entry.bind("<FocusOut>", self._on_top_db_entry)

        # ── progress bar (hidden until analysis starts) ───────────────────
        self._progress = ctk.CTkProgressBar(self, mode="indeterminate")
        # not gridded yet — shown only during analysis

        # ── results ───────────────────────────────────────────────────────
        results_frame = ctk.CTkFrame(self)
        results_frame.grid(row=3, column=0, padx=16, pady=4, sticky="nsew")
        results_frame.grid_columnconfigure(0, weight=1)
        results_frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            results_frame, text="Wykryte pauzy", font=ctk.CTkFont(size=14, weight="bold")
        ).grid(row=0, column=0, padx=12, pady=(10, 4), sticky="w")

        self._results_box = ctk.CTkTextbox(results_frame, state="disabled")
        self._results_box.grid(row=1, column=0, padx=8, pady=(0, 4), sticky="nsew")

        self._summary_label = ctk.CTkLabel(
            results_frame, text="", anchor="w",
            font=ctk.CTkFont(size=12),
        )
        self._summary_label.grid(row=2, column=0, padx=12, pady=(0, 8), sticky="ew")

        # ── bottom bar: export + status ───────────────────────────────────
        bottom = ctk.CTkFrame(self)
        bottom.grid(row=4, column=0, padx=16, pady=(4, 16), sticky="ew")

        self._export_btn = ctk.CTkButton(
            bottom, text="Eksportuj do .txt", state="disabled", command=self._export
        )
        self._export_btn.pack(side="right", padx=8, pady=8)

        self._status_label = ctk.CTkLabel(bottom, text="Gotowy")
        self._status_label.pack(side="left", padx=12, pady=8)

    # ── threshold controls ────────────────────────────────────────────────

    def _on_slider(self, value: float) -> None:
        self._threshold = round(value, 1)
        self._threshold_entry.delete(0, tk.END)
        self._threshold_entry.insert(0, f"{self._threshold:.1f}")

    def _on_entry(self, _event: object = None) -> None:
        try:
            value = float(self._threshold_entry.get())
            value = max(_MIN_THRESHOLD, min(_MAX_THRESHOLD, value))
        except ValueError:
            value = self._threshold
        self._threshold = round(value, 1)
        self._threshold_entry.delete(0, tk.END)
        self._threshold_entry.insert(0, f"{self._threshold:.1f}")
        self._slider.set(self._threshold)

    # ── top_db controls ───────────────────────────────────────────────────

    def _on_top_db_slider(self, value: float) -> None:
        self._top_db = int(round(value))
        self._top_db_entry.delete(0, tk.END)
        self._top_db_entry.insert(0, str(self._top_db))

    def _on_top_db_entry(self, _event: object = None) -> None:
        try:
            value = int(float(self._top_db_entry.get()))
            value = max(_MIN_TOP_DB, min(_MAX_TOP_DB, value))
        except ValueError:
            value = self._top_db
        self._top_db = value
        self._top_db_entry.delete(0, tk.END)
        self._top_db_entry.insert(0, str(self._top_db))
        self._top_db_slider.set(self._top_db)

    # ── file picker ───────────────────────────────────────────────────────

    def _pick_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Wybierz plik AAF",
            filetypes=[("AAF files", "*.aaf"), ("All files", "*.*")],
        )
        if path:
            self._aaf_path = Path(path)
            self._file_label.configure(text=str(self._aaf_path))
            self._status_label.configure(text="Plik wczytany. Kliknij 'Analizuj'.")

    # ── analysis (FR-003) ─────────────────────────────────────────────────

    def _analyze(self) -> None:
        if self._aaf_path is None:
            messagebox.showwarning("Brak pliku", "Najpierw wybierz plik AAF.")
            return
        if self._running:
            return

        self._running = True
        self._analyze_btn.configure(state="disabled")
        self._export_btn.configure(state="disabled")
        self._progress.grid(row=5, column=0, padx=16, pady=(0, 4), sticky="ew")
        self._progress.start()
        self._status_label.configure(text="Analizuję…")

        threading.Thread(
            target=self._run_analysis,
            args=(self._aaf_path, self._threshold, self._top_db),
            daemon=True,
        ).start()

    def _run_analysis(self, aaf_path: Path, threshold_sec: float, top_db: int) -> None:
        """Runs in a background thread — never touches Tk widgets directly."""
        try:
            result: AnalysisResult = analyzer.analyze(
                aaf_path,
                threshold_sec,
                top_db=top_db,
                on_progress=lambda msg: self.after(0, self._set_status, msg),
            )
            self.after(0, self._on_analysis_done, result)
        except AnalysisError as exc:
            self.after(0, self._on_analysis_error, str(exc))
        except Exception as exc:
            self.after(0, self._on_analysis_error, f"Nieoczekiwany błąd: {exc}")

    def _set_status(self, msg: str) -> None:
        self._status_label.configure(text=msg)

    def _on_analysis_done(self, result: AnalysisResult) -> None:
        self._stop_progress()
        self._results_box.configure(state="normal")
        self._results_box.delete("1.0", tk.END)
        if result.lines:
            self._results_box.insert(tk.END, "\n".join(result.lines))
            self._export_btn.configure(state="normal")
            self._status_label.configure(text=f"Znaleziono {len(result.lines)} pauz.")
        else:
            self._results_box.insert(
                tk.END, "Brak pauz spełniających kryteria.\nSpróbuj zmniejszyć próg."
            )
            self._status_label.configure(text="Analiza zakończona — brak pauz.")
        self._results_box.configure(state="disabled")

        # Summary bar
        def fmt(sec: float) -> str:
            m, s = divmod(int(sec), 60)
            h, m = divmod(m, 60)
            return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

        self._summary_label.configure(
            text=(
                f"Materiał: {fmt(result.total_sec)} ({result.total_sec:.1f} s)   │   "
                f"Pauzy: {fmt(result.pause_sec)} ({result.pause_sec:.1f} s)   │   "
                f"Dźwięk: {fmt(result.audio_sec)} ({result.audio_sec:.1f} s)"
            )
        )

    def _on_analysis_error(self, message: str) -> None:
        self._stop_progress()
        self._status_label.configure(text="Błąd analizy.")
        messagebox.showerror("Błąd analizy", message)

    def _stop_progress(self) -> None:
        self._running = False
        self._progress.stop()
        self._progress.grid_forget()
        self._analyze_btn.configure(state="normal")

    # ── export (FR-005) ───────────────────────────────────────────────────

    def _export(self) -> None:
        content = self._results_box.get("1.0", tk.END).strip()
        if not content:
            return

        save_path = filedialog.asksaveasfilename(
            title="Zapisz wyniki",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt")],
            initialfile="pauzy.txt",
        )
        if save_path:
            Path(save_path).write_text(content, encoding="utf-8")
            self._status_label.configure(text=f"Wyeksportowano: {save_path}")


def main() -> None:
    app = EditorAnalyzerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
