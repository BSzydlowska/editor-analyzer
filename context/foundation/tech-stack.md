---
starter_id: fastapi
package_manager: uv
project_name: editor-analyzer
hints:
  language_family: python
  team_size: solo
  deployment_target: github-releases
  ci_provider: github-actions
  ci_default_flow: manual-promotion
  bootstrapper_confidence: best-effort
  path_taken: custom
  quality_override: true
  self_check_answers:
    typed: true
    from_official_starter: false
    conventions: true
    docs_current: true
    can_judge_agent: true
  has_auth: false
  has_payments: false
  has_realtime: false
  has_ai: false
  has_background_jobs: false
---

## Why this stack

Solo developer building a local Windows desktop tool for a single non-technical user. Python was chosen over JS/Rust/Dart because the `aaf2` library (AAF file parsing) and audio analysis ecosystem (librosa/pydub) live in Python — language-family compatibility with the problem domain is the primary driver. CustomTkinter provides a native-feeling Windows UI with zero runtime dependencies beyond Python, appropriate for a single-purpose tool that must run offline. No Python desktop starter exists in the registry (`recommended_defaults[desktop][python]` is unset), so `fastapi` is recorded as the nearest Python toolchain anchor and `bootstrapper_confidence` is `best-effort` — manual scaffolding steps are expected. Distribution is via PyInstaller `.exe` published to GitHub Releases; releases are triggered manually. `quality_override` is true because `from_official_starter` failed (no registered Python desktop starter), though the remaining three agent-friendly gates all pass.
