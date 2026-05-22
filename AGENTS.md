# Repository Guidelines

EditorAnalyzer is a Windows desktop tool that locates low-energy audio pauses in AAF footage files and exports the results as plain text. Stack: Python 3.13, CustomTkinter GUI, `pyaaf2` for AAF parsing, `librosa` for audio analysis.

## Hard rules

- Never add network calls, cloud APIs, or external authentication — the tool runs fully offline by design.
- Never overwrite files in `context/` — that directory holds the project's PRD, shape notes, and bootstrap audit log.
- `_stub_analyze` in `main.py` is a placeholder; replace it with real `pyaaf2` + `librosa` logic (FR-001, FR-002, FR-003) before shipping.
- Do not add a `requirements.txt` — dependency management is handled exclusively via `uv` and `pyproject.toml`.

## Project structure

`main.py` — entry point; `EditorAnalyzerApp` (ctk.CTk subclass) + `_stub_analyze` placeholder. `pyproject.toml` — deps and metadata (see `@pyproject.toml`). `context/foundation/` — PRD, shape notes, tech-stack hand-off; read-only for agents. `.venv/` — managed by uv; do not edit manually. No tests directory or CI workflows exist yet.

## Commands

Run the app: `uv run python main.py`

On Windows, if tkinter fails with "Can't find a usable init.tcl", set `TCL_LIBRARY` and `TK_LIBRARY` to the `tcl8.6` and `tk8.6` subdirectories under the Python313 install before running.

Add a dependency: `uv add <package>`. Security audit: `uv run pip-audit`.

## Coding style & naming

- Python 3.13 with `from __future__ import annotations` at the top of every file.
- Type annotations on all function signatures; `Path` from `pathlib` instead of raw strings for file paths.
- Private methods and attributes prefixed with `_`; widget references stored as `self._<name>`.
- No external formatter configured yet — follow the style visible in `@main.py`.

## Architecture

- `EditorAnalyzerApp` (ctk.CTk subclass) owns the full UI and event loop in `main.py`.
- Audio analysis is isolated in `_stub_analyze(aaf_path: Path) -> list[str]` — extract this into a separate module (`analyzer.py`) when implementing FR-001/FR-002.
- Output format: one timecode range per line (`HH:MM:SS.mmm – HH:MM:SS.mmm  (N.NN s)`), written to a user-chosen `.txt` file via `Path.write_text(..., encoding="utf-8")`.
- Full functional requirements for FR-001/FR-002/FR-003 are defined in `@context/foundation/prd.md`.

## Commit guidelines

No commits exist yet. Use Conventional Commits prefixes: `feat:`, `fix:`, `refactor:`, `docs:`, `chore:`. Keep the subject line under 72 characters.
