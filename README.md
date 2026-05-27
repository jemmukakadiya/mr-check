# mr check

Python pipeline for the **Koinon Team-Jemish MR review** workflow.

The pipeline has three phases:

```
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────────┐
│ 1. fetch_mrs.py  │ → │ 2. analyze       │ → │ 3. publish_review.py │
│  fetch + decide  │   │  (Claude / you)  │   │  PDF → upload → post │
│  + stage         │   │  fill analysis   │   │  → record            │
└──────────────────┘   └──────────────────┘   └──────────────────────┘
```

Each phase is independent and idempotent. The two scripts only talk to
each other through the filesystem (`~/.koinon-mr-reviews/pending/<iid>/`)
and the persistent state file (`~/.koinon-mr-reviews/state.json`).

---

## Layout

```
mr check/
├── README.md
├── requirements.txt
├── .env.example         ← committed template
├── .env                 ← local, gitignored — holds the PAT
├── .gitignore
├── common.py            ← shared library (config, state, glab helpers)
├── fetch_mrs.py         ← phase 1: fetch + stage
├── publish_review.py    ← phase 3: PDF + upload + post + record
└── status.py            ← read-only inspection of pending packages
```

Runtime data lives **outside** this folder so the repo stays clean:

```
~/.koinon-mr-reviews/
├── state.json                     ← iid → {sha, note_id, upload_url, ts}
├── logs/<YYYY-MM-DD>.log          ← one line per posted MR
└── pending/<iid>/                 ← per-MR work in progress
    ├── mr.json                    ← MR metadata (from glab mr view)
    ├── mr.diff                    ← raw unified diff
    ├── task.json                  ← linked GitLab issue, if any (full JSON)
    ├── task.md                    ← linked issue title + description
    ├── context.md                 ← short human summary (incl. linked task)
    └── analysis.json              ← phase 2 fills this in

~/mr reviews/MR<iid>_<author>_<new|old>_<dd.mm>.pdf
```

`task.json` and `task.md` are only written when fetch detects a linked issue
(branch name prefix `<iid>-...` or `#<iid>` in the MR description) and
`glab issue view` succeeds. Absent files = no linked task for that MR.

These paths are overridable via `.env` (`MR_REVIEW_ROOT`, `MR_REVIEW_PDF_DIR`).

---

## Phase 1 — `fetch_mrs.py`

```sh
python3 fetch_mrs.py
```

- `glab mr list -R <project> --label <label> --state opened`
- Buckets each MR per `state.json`:

  | Condition                                | Action            | Bucket                  |
  |------------------------------------------|-------------------|-------------------------|
  | iid in state, sha matches                | skip              | skipped_no_new_commits  |
  | iid in state, sha differs                | re-review         | re_reviewed             |
  | iid NOT in state, < 24h old              | review            | new_reviewed            |
  | iid NOT in state, ≥ 24h old              | skip (no backfill)| out_of_window           |

- For every MR in `new_reviewed` or `re_reviewed`: writes
  `mr.json`, `mr.diff`, `context.md`, and an `analysis.json` **template**
  into `pending/<iid>/`. Re-runs never overwrite an in-flight `analysis.json`.
- macOS notification listing the IIDs needing analysis.

---

## Phase 2 — analysis (manual or by Claude in the routine)

Open each `~/.koinon-mr-reviews/pending/<iid>/` and edit `analysis.json`.
The template ships with one example finding and instructions. Fill in:

```json
{
  "complete": true,                    ← gate for publish; flip last
  "verdict": "Approve" | "Request changes" | "Comment",
  "verdict_reason": "one-line reason",
  "tldr": "one paragraph",
  "task_coverage": null | {            ← compare the diff against task.md
    "task_iid":            1628,
    "task_title":          "lehrevaluation: close schoolarization gaps …",
    "task_state":          "opened",
    "addresses":           ["Item that is fully implemented", "..."],
    "partially_addresses": ["Item started but incomplete"],
    "not_addressed":       ["Item the task asks for but the diff skips"],
    "out_of_scope_changes":["Refactor not requested by the task"],
    "notes":               "free-form remarks"
  },
  "findings": [
    {"severity": "BLOCKER|MAJOR|MINOR|NIT|INFO",
     "area": "Security / Models / …",
     "title": "short title",
     "body": "multi-paragraph; \\n\\n for breaks",
     "code_snippet": "optional monospace",
     "action": "imperative sentence"}
  ],
  "what_looks_good": ["bullet 1", "bullet 2"],
  "appendix": null,                    ← or {title, headers, rows, highlight_rows}
  "actions": ["Fix X.", "Smoke-test Y."]
}
```

`task_coverage` is the new (May 2026) field that records how the diff lines
up against the linked issue. Set it to `null` if the MR is not connected to
any GitLab issue (no `<iid>-` branch prefix and no `#<n>` in the
description). When set, the renderer adds a **Task coverage** section to
the PDF between TL;DR and the findings summary — green ✓ for *addresses*,
amber ⚠ for *partially*, red ✗ for *not addressed*, blue • for *out of scope*.

A genuinely missing task item should also be surfaced as a finding (with
`MAJOR` or `BLOCKER` severity as appropriate) so it shows up in the
comment's top-findings list — `task_coverage` itself is informational.

Severity scale (colors used in the PDF):

| Severity | Color    | Meaning                                          |
|----------|----------|--------------------------------------------------|
| BLOCKER  | #b00020  | must fix before merge; security/correctness      |
| MAJOR    | #d93f0b  | should fix before merge; meaningful impact       |
| MINOR    | #bf8700  | nice to fix; cleanup or style                    |
| NIT      | #1f883d  | subjective polish                                |
| INFO     | #0969da  | context only, not an issue                       |

When **everything** is filled in, set `"complete": true`. That's the only
flag that releases the package to phase 3.

The PDF layout spec lives at
`~/.claude/projects/-Users-jemmu-Work-Koinon/memory/feedback_mr_review_pdf.md`.

---

## Phase 3 — `publish_review.py`

```sh
python3 publish_review.py
```

For every `pending/<iid>/` whose `analysis.json` is `"complete": true`:

1. Render PDF → `~/mr reviews/MR<iid>_<author>_<new|old>_<dd.mm>.pdf`
   - `new` = first-ever review of this MR
   - `old` = re-review (prior entry in state.json)
2. Upload via `curl -F file=@…` to
   `POST {GITLAB_URL}/api/v4/projects/{ENCODED}/uploads`
   *(curl, not `glab api -F` — the latter returns HTTP 400 "file is
   invalid" on this GitLab instance)*
3. Compose and post the markdown comment via `glab mr note`. The PDF
   attachment is mandatory; the body always contains the
   `[filename.pdf](/uploads/…)` link.
4. `merge_state(iid, {last_reviewed_sha, last_reviewed_at, last_note_id,
   last_upload_url})`.
5. Append one line to `logs/<YYYY-MM-DD>.log`.
6. `rm -rf pending/<iid>/`.
7. macOS notification with posted / failed / incomplete counts.

**State is updated only on full per-MR success.** A failure between PDF
render and comment post leaves no state change, so the next run retries
from the same `analysis.json` you already wrote.

---

## status helper

```sh
python3 status.py
```

Read-only. Shows every pending dir with `ready` (complete) / `draft`
(template-or-WIP) / `invalid` (broken JSON) status. Useful for confirming
phase 2 progress without touching anything.

---

## Configuration (.env)

All sensitive and per-environment values come from a `.env` file next to
the scripts. The library parses it inline at import time — no
`python-dotenv` dependency.

| Key                  | Required | Default               | Purpose                              |
|----------------------|----------|-----------------------|--------------------------------------|
| `GITLAB_TOKEN`       | yes      | —                     | PAT with `api` scope                 |
| `GITLAB_URL`         | no       | `https://gitlab.lrz.de` | GitLab base URL (no trailing slash) |
| `GITLAB_PROJECT`     | no       | `politeia/koinon`     | `namespace/name` form                |
| `MR_LABEL`           | no       | `Team Jemish`         | MR label gate                        |
| `MR_REVIEW_ROOT`     | no       | `~/.koinon-mr-reviews`| where state/logs/pending live        |
| `MR_REVIEW_PDF_DIR`  | no       | `~/mr reviews`        | where PDFs are written               |

**Resolution order** for every key: existing env var → `.env` → built-in default.

### Setting up `.env`

```sh
cp .env.example .env
chmod 600 .env
# paste your PAT into .env

# To copy from the macOS Keychain:
security find-generic-password -s 'claude gilab token ' -w
```

`.env` is in `.gitignore` and ships at `chmod 600`. Never commit it.

---

## Integration with the Claude scheduled routine

The scheduled task at
`~/.claude/scheduled-tasks/koinon-daily-mr-review-team-jemish/SKILL.md`
fires every 2h (09–21) and runs all three phases:

```
fetch_mrs.py
  ↓
Claude analyzes each pending dir against CLAUDE.md, writes analysis.json
  ↓
publish_review.py
```

Phase 2 (the analysis) happens **inside** the routine — Claude reads
mr.diff, the project's CLAUDE.md, and the format spec, then writes the
analysis JSON for every pending dir. Phase 3 then publishes everything
that ended up `"complete": true`.

For manual runs (you reviewing an MR by hand without the routine):

```sh
python3 fetch_mrs.py
# (open ~/.koinon-mr-reviews/pending/<iid>/analysis.json in your editor, fill it in)
python3 status.py     # confirm "ready"
python3 publish_review.py
```

---

## Studying in PyCharm

```
File → Open → /Users/jemmu/Work/python scripts/mr check
```

Module layout for navigation:

- `common.py` — module-level `_load_dotenv()` runs on import; all
  constants (PROJECT, LABEL, paths, severity scale) live here. Look here
  first to understand defaults and resolution.
- `fetch_mrs.py` — `main()` calls `enumerate_and_decide()` (from common)
  then `write_pending()` for each MR. Easy to step through.
- `publish_review.py` — `main()` iterates `PENDING_DIR.iterdir()` and
  calls `_publish_one()`. That function is the entire publish pipeline
  for one MR — read it top to bottom to see render → upload → post →
  state → log → cleanup.
- `status.py` — 25 lines. The simplest entry point.

Interpreter: system Python 3.14 (where `reportlab 4.5.1` lives) or the
`.venv/` if you set one up via `pip install -r requirements.txt`.

External binaries the scripts shell out to:

- `glab` — `/opt/homebrew/bin/glab`
- `curl` — system
- `osascript` — macOS notifications (best-effort, optional)

---

## Failure modes

| Symptom                                              | Likely cause                                                       |
|------------------------------------------------------|--------------------------------------------------------------------|
| `Required config 'GITLAB_TOKEN' is not set`          | No PAT in `.env` or environment                                   |
| `glab … did not return JSON`                         | Network, glab auth, or label/project misconfigured                 |
| `publish_review.py` reports `skipped_incomplete`     | Analyze and set `"complete": true`                                 |
| `publish_review.py` reports `verdict ... not one of` | Use exactly: `Approve`, `Request changes`, or `Comment`            |
| `upload failed (curl exit …)`                        | Token lacks `api` scope, network, or GitLab rejecting              |
| `glab mr note failed`                                | MR closed / token scope / network                                  |
| Comment posted but state.json not updated            | File a bug; until then, manually edit state.json                   |

---

## CLAUDE.md compliance

The scripts honor the carve-out in `/Users/jemmu/Work/Koinon/CLAUDE.md`
(lines 770–775):

- MR comments are only posted when accompanied by the PDF attachment
- The upload step is coupled to the comment post — no speculative uploads
- No other GitLab writes (no label/state/title/description changes, no
  approvals, no edits or deletions of prior comments)

If `publish_review.py` succeeds at upload but fails at the comment post,
it logs the orphan upload URL and does not retry the upload.
