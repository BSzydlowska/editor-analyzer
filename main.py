from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path

import customtkinter as ctk

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


class EditorAnalyzerApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.title("EditorAnalyzer")
        self.geometry("900x600")
        self.minsize(700, 450)

        self._aaf_path: Path | None = None

        self._build_ui()

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # — top bar: file picker —
        top = ctk.CTkFrame(self)
        top.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")
        top.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(top, text="Otwórz plik AAF…", command=self._pick_file).grid(
            row=0, column=0, padx=(8, 4), pady=8
        )
        self._file_label = ctk.CTkLabel(top, text="Nie wybrano pliku", anchor="w")
        self._file_label.grid(row=0, column=1, padx=4, pady=8, sticky="ew")

        ctk.CTkButton(
            top, text="Analizuj", fg_color="green", command=self._analyze
        ).grid(row=0, column=2, padx=(4, 8), pady=8)

        # — results list —
        results_frame = ctk.CTkFrame(self)
        results_frame.grid(row=1, column=0, padx=16, pady=8, sticky="nsew")
        results_frame.grid_columnconfigure(0, weight=1)
        results_frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(results_frame, text="Wykryte pauzy", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, padx=12, pady=(10, 4), sticky="w"
        )

        self._results_box = ctk.CTkTextbox(results_frame, state="disabled")
        self._results_box.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="nsew")

        # — bottom bar: export —
        bottom = ctk.CTkFrame(self)
        bottom.grid(row=2, column=0, padx=16, pady=(8, 16), sticky="ew")

        self._export_btn = ctk.CTkButton(
            bottom, text="Eksportuj do .txt", state="disabled", command=self._export
        )
        self._export_btn.pack(side="right", padx=8, pady=8)

        self._status_label = ctk.CTkLabel(bottom, text="Gotowy")
        self._status_label.pack(side="left", padx=12, pady=8)

    def _pick_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Wybierz plik AAF",
            filetypes=[("AAF files", "*.aaf"), ("All files", "*.*")],
        )
        if path:
            self._aaf_path = Path(path)
            self._file_label.configure(text=str(self._aaf_path))
            self._status_label.configure(text="Plik wczytany. Kliknij 'Analizuj'.")

    def _analyze(self) -> None:
        if self._aaf_path is None:
            messagebox.showwarning("Brak pliku", "Najpierw wybierz plik AAF.")
            return

        self._status_label.configure(text="Analizuję…")
        self.update_idletasks()

        # TODO: wywołaj moduł analizy audio (FR-001, FR-002)
        results: list[str] = _stub_analyze(self._aaf_path)

        self._results_box.configure(state="normal")
        self._results_box.delete("1.0", tk.END)
        if results:
            self._results_box.insert(tk.END, "\n".join(results))
            self._export_btn.configure(state="normal")
            self._status_label.configure(text=f"Znaleziono {len(results)} pauz.")
        else:
            self._results_box.insert(tk.END, "Brak pauz spełniających kryteria.")
            self._export_btn.configure(state="disabled")
            self._status_label.configure(text="Analiza zakończona.")
        self._results_box.configure(state="disabled")

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


def _stub_analyze(aaf_path: Path) -> list[str]:
    # Placeholder — zastąp wywołaniem aaf2 + librosa (FR-001, FR-002, FR-003)
    return [
        "00:01:23.450 – 00:01:27.810  (4.36 s)  [STUB]",
        "00:03:05.100 – 00:03:09.220  (4.12 s)  [STUB]",
    ]


def main() -> None:
    app = EditorAnalyzerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
