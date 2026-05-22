---
project: "EditorAnalyzer"
version: 1
status: draft
created: 2026-05-19
context_type: greenfield
product_type: desktop
target_scale:
  users: small
  qps: low
  data_volume: large
timeline_budget:
  mvp_weeks: 3
  hard_deadline: null
  after_hours_only: false
---

## Vision & Problem Statement

A documentary film editor spends a significant portion of their editing time manually scrubbing through multi-hour raw footage to locate specific audio moments — long pauses, silences, reflective beats — that directors describe only vaguely ("there was a moment when she got silent and started thinking"). Without a tool to surface these moments automatically, the editor must watch or fast-scrub through all the footage, which is the single biggest time sink in documentary post-production.

The insight: directors think in emotional terms ("she got thoughtful"), but footage is a linear time-based file. There is no existing local tool that bridges directorial language and timecode lookup for confidential AAF files — cloud tools are off-limits because the material is confidential.

## User & Persona

**Primary persona:** Morti — professional documentary film editor.

Role: Cuts documentary films; works with AAF files from multi-camera shoots, often 6–20 hours of raw footage per project.

The moment: A director gives a note post-screening — "find that pause before she answered the question about her father" — and the editor must locate the exact timecode manually in hours of timeline. This is the recurring friction point that EditorAnalyzer targets.

Persona scope: Single named user — the tool is built for a specific person's workflow.

## Success Criteria

### Primary
- The app correctly identifies ≥ 75% of pauses longer than the configured threshold in a loaded AAF file and outputs timecode stamps.

### Secondary
- Analysis completes fast enough to not block the editor's workflow (does not require them to wait idle).
- Output is a plain .txt file (Timecode | Seconds, tab or pipe separated).

### Guardrails
- Analysis time must not exceed a fixed multiple of footage duration (e.g. 2× realtime — a 1-hour file takes at most 2 hours to analyze). Exact ratio is an open question; must be agreed with primary user before shipping.

## User Stories

### US-01: Editor finds long pauses in raw footage

- **Given** the editor has opened the app and loaded an AAF file
- **When** they set a minimum pause threshold (e.g. 3 seconds) and trigger analysis
- **Then** they see a list of timecode stamps for every pause exceeding that threshold

#### Acceptance Criteria
- Every detected pause is timestamped in standard timecode format
- The result is exportable as a plain .txt file (Timecode | Seconds)
- Analysis runs to completion without requiring further input

## Functional Requirements

### Core flow

- FR-001: Editor can load an AAF file into the app. Priority: must-have
  > Socrates: No counter-argument — loading AAF is the core input, it stands.

- FR-002: Editor can set a minimum pause duration threshold (in seconds). Priority: must-have
  > Socrates: No counter-argument — a numeric input is the right interface, it stands.

- FR-003: Editor can trigger pause analysis on the loaded AAF file. Priority: must-have
  > Socrates: No counter-argument — explicit trigger is necessary, it stands.

- FR-004: Editor can view a list of timecode stamps where detected pauses exceed the threshold. Priority: must-have
  > Socrates: No counter-argument — a list of stamps is exactly what the editor needs, it stands.

### Export

- FR-005: Editor can export results as a plain .txt file (Timecode | Seconds, tab or pipe separated). Priority: must-have
  > Socrates: Counter-argument considered: "plain .txt or .csv would be more portable than markdown for an editor's workflow." Resolution: updated from markdown to plain .txt.

- FR-006: Editor can export results as an Avid-compatible markers file. Priority: nice-to-have
  > Socrates: No counter-argument — already scoped as nice-to-have, it stands.

## Non-Functional Requirements

- No footage data or file content leaves the local machine at any point during or after analysis.
- Analysis time must not exceed a fixed multiple of footage duration (exact ratio TBD — see Open Questions #1).
- The app must run on Windows (current OS of the primary user).

## Business Logic

Given an AAF file, the app identifies every continuous audio segment where energy falls below a perceptible threshold for longer than the configured minimum duration — treating quiet breathing and low hum as "pause" — and returns its start timecode. This is an energy-level detection rule, not binary silence detection; the distinction ensures naturally quiet moments (a soft exhale, background hum) are captured alongside full silences.

Inputs the rule consumes: an AAF file (user-selected), a minimum pause duration in seconds (user-configured). Output: a list of timecode stamps, one per detected pause event. The editor encounters the output as a plain .txt list immediately after analysis completes.

## Access Control

Single user; local profile only. Data lives on-device — no server, no login, no account creation, no network access. The footage is confidential; no material ever leaves the local machine. Flat model: no roles, no separation.

## Non-Goals

- **No other input formats for v1**: AAF only. ProRes, MXF, EDL, and other formats are explicitly out of scope.
- **No other event types for v1**: pause/silence detection only. Music, speech patterns, specific words, or emotional cues are not in scope.
- **No web or mobile version**: desktop Windows app only.
- **No multi-user support**: single-user, no shared projects, no collaboration.
- **No cloud sync or backup**: fully local, no network features, no remote storage.
- **No real-time preview**: the app detects and lists; it does not play back or preview detected moments inside the app itself.
- **Avid markers export deferred**: FR-006 is nice-to-have and explicitly out of v1 scope.

## Open Questions

1. **What is the acceptable analysis time ratio?** — The guardrail says analysis ≤ N× realtime, but N is undefined. Owner: user + primary user (editor). Must be decided before shipping. Block: yes (guardrail is currently unspecified).
2. **Which AAF library will be used?** — Assumed to exist; specific library not yet identified. Owner: builder. By: start of implementation. Block: yes (FR-001 depends on it).
3. **What is the Avid markers file format?** — Required for FR-006 (nice-to-have). Owner: builder (check Avid documentation). By: v1.1 planning. Block: no (nice-to-have only).
4. **Secondary success criterion format** — Shape-notes originally read "clean markdown table" for the output format; updated to "plain .txt" in this PRD to match the FR-005 Socratic resolution. Owner: builder — confirm this is correct before locking PRD. Block: no.
