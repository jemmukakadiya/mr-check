---
name: koinon-daily-mr-review-team-jemish
description: Runs every 2h (09,11,13,15,17,19,21). Three-phase pipeline — fetch_mrs.py stages MRs, Claude analyzes each one against CLAUDE.md inside the routine, publish_review.py renders the PDF, uploads it to GitLab, posts the comment, and updates the record.
---

This is the scheduled trigger for the Team-Jemish MR review pipeline. The
heavy mechanical work (fetching MRs, deciding what needs review, rendering
the PDF, uploading, posting the comment, maintaining state) lives in two
Python scripts under `/Users/jemmu/Work/python scripts/mr check/`. The
analysis step itself — verdict + findings + what-looks-good — happens
**inside this routine** between the two script invocations.

## Three-phase pipeline

```
1. fetch_mrs.py        → stages each MR that needs review into
                         ~/.koinon-mr-reviews/pending/<iid>/

2. Claude (you, here)  → for each pending dir, read mr.diff + context.md,
                         analyze against /Users/jemmu/Work/Koinon/CLAUDE.md,
                         write analysis.json with verdict/findings,
                         set "complete": true

3. publish_review.py   → for every pending dir with "complete": true,
                         render PDF → upload via curl → post comment →
                         update state.json → log → remove pending dir
```

## Phase 1 — fetch

```sh
python3 "/Users/jemmu/Work/python scripts/mr check/fetch_mrs.py"
```

What it does:
- Loads config from `/Users/jemmu/Work/python scripts/mr check/.env`
  (GITLAB_TOKEN, GITLAB_URL, GITLAB_PROJECT, MR_LABEL).
- `glab mr list --label "Team Jemish" --state opened`.
- Buckets each MR per `~/.koinon-mr-reviews/state.json`:
    - already at current SHA → skipped
    - SHA changed since last review → re-review
    - never seen, < 24h old → new review
    - never seen, ≥ 24h old → out of window (skipped)
- For each MR to (re-)review, writes `mr.json`, `mr.diff`, `context.md`,
  and an `analysis.json` **template** to `~/.koinon-mr-reviews/pending/<iid>/`.
- Fires a macOS notification listing the IIDs needing analysis.
- Capture the script's stdout in the routine log.

If `fetch_mrs.py` says "nothing to stage", **exit silently** — no
notification, no further steps.

## Phase 2 — analysis (you do this here, in the routine)

After `fetch_mrs.py` has staged at least one MR, iterate over every
sub-directory under `~/.koinon-mr-reviews/pending/`:

For each pending MR:

1. **Read inputs**
   - `pending/<iid>/mr.json`   (metadata: title, author, branches, SHA)
   - `pending/<iid>/mr.diff`   (the actual unified diff to review)
   - `pending/<iid>/context.md` (one-screen summary; also shows the linked task iid, if any)
   - `pending/<iid>/task.md`    (the linked GitLab issue's title + description, **if** the
     branch or MR description matched an issue iid — file may be absent)
   - `pending/<iid>/task.json`  (full issue JSON, same condition)
   - `/Users/jemmu/Work/Koinon/CLAUDE.md` (the rule set to review against)

2. **Analyze the code quality**
   - Check for: SQL injection / prepared statements, CSRF tokens, XSS escaping
     on every dynamic field, `school_id` filtering (schoolarization), session
     discipline, globals (`global $...`), the mysqli string-return gotcha,
     target architecture compliance for new code, comment quality.
   - Pull the format spec from
     `~/.claude/projects/-Users-jemmu-Work-Koinon/memory/feedback_mr_review_pdf.md`
     for what each section of the PDF must contain.
   - For non-trivial findings, verify against the actual code on disk
     (`/Users/jemmu/Work/Koinon/src/...`) — do not rely on memory or assume
     things from the diff alone. `git fetch origin <branch>` if you need the
     branch under `origin/`.

3. **Verify task coverage** (if `task.md` exists)
   - Read `task.md` to understand what the linked GitLab issue actually asks for.
     Treat each bullet, checkbox, or numbered item in the description as one
     deliverable.
   - For each deliverable, check the diff: is it fully implemented, partially
     implemented, or completely missing?
   - Also note any **out-of-scope changes** — code touched by the diff that
     does not correspond to any item in the task. These are not automatically
     bad (refactors are fine), but they should be visible to the reviewer.
   - If `task.md` is absent (no linked issue detected), set `"task_coverage": null`
     in the analysis. Don't fabricate a fake task.
   - If a not-addressed item is critical, also surface it as a `MAJOR` or
     `BLOCKER` finding in `findings[]` — task_coverage is informational, the
     finding is what carries the severity into the comment summary.

4. **Write the analysis** — overwrite `pending/<iid>/analysis.json` with:
   ```json
   {
     "complete": true,
     "verdict": "Approve" | "Request changes" | "Comment",
     "verdict_reason": "one-line",
     "tldr": "one paragraph",
     "task_coverage": null | {
       "task_iid": 1628,
       "task_title": "lehrevaluation: close schoolarization gaps …",
       "task_state": "opened",
       "addresses":            ["Schoolarization closed across model methods", ...],
       "partially_addresses":  ["… with one method still missing JOIN"],
       "not_addressed":        ["Update changelog"],
       "out_of_scope_changes": ["Refactored deletSuccess() — not in task"],
       "notes": "free-form notes if needed"
     },
     "findings": [
       {"severity": "BLOCKER|MAJOR|MINOR|NIT|INFO",
        "area": "...", "title": "...",
        "body": "multi-paragraph explanation",
        "code_snippet": "optional",
        "action": "imperative sentence"}
     ],
     "what_looks_good": ["bullet 1", "bullet 2", ...],
     "appendix": null | {"title": "...", "headers": [...], "rows": [[...]], "highlight_rows": [...]},
     "actions": ["imperative bullet 1", "imperative bullet 2", ...]
   }
   ```
   `"complete": true` is the gate `publish_review.py` checks. Don't set it
   until the analysis is genuinely done.

5. **Do not post anything yourself.** Do not edit existing MR comments,
   labels, or state. The only file you write in this phase is the per-MR
   `analysis.json`.

## Phase 3 — publish

```sh
python3 "/Users/jemmu/Work/python scripts/mr check/publish_review.py"
```

What it does (per pending package with `"complete": true`):
- Renders the PDF (reportlab) to
  `~/mr reviews/MR<iid>_<author>_<new|old>_<dd.mm>.pdf`.
- Uploads it via `curl` to `POST /api/v4/projects/politeia%2Fkoinon/uploads`
  — direct multipart because `glab api -F file=@…` returns HTTP 400
  `file is invalid` on this GitLab instance.
- Posts the markdown comment via `glab mr note <iid> -m "$body"`. The
  comment always attaches the PDF (CLAUDE.md:761 forbids text-only MR
  comments).
- Merges a new entry into `~/.koinon-mr-reviews/state.json`
  (`{last_reviewed_sha, last_reviewed_at, last_note_id, last_upload_url}`).
- Writes one line to `~/.koinon-mr-reviews/logs/<YYYY-MM-DD>.log`.
- Removes the pending dir.
- Fires a macOS notification with the posted/failed/incomplete counts.

Capture the script's stdout in the routine log. If it exits non-zero,
surface the error.

## Authorization

The posting in Phase 3 is covered by the `mr<iid>_review.pdf` carve-out
in `/Users/jemmu/Work/Koinon/CLAUDE.md` (lines 770-775): an MR comment may
be posted on `gitlab.lrz.de` only when it attaches a PDF. `publish_review.py`
always attaches the PDF — that is its entire purpose.

The session-level "post without per-MR confirmation" authorization that
the old `/review-team-jemish` slash command carried still applies for this
scheduled context. Every comment the script posts attaches the PDF; no
other GitLab writes happen.

## Rate-limit recovery

The schedule is `0 9-21/2 * * *` (7 ticks per day from 09 to 21). State is
only persisted for MRs whose full phase 3 pipeline succeeded, and the
`analysis.json` you wrote in phase 2 survives across runs. So if a tick is
cut short by a plan rate-limit between phase 2 and phase 3 — or partway
through phase 2 — the next tick picks up where this one left off:

- `fetch_mrs.py` re-runs cheaply (one `glab mr list`, refreshes
  mr.json/mr.diff for any still-pending MR, never overwrites your
  analysis.json draft).
- Phase 2 only re-analyzes MRs whose `analysis.json` is still `"complete": false`.
- `publish_review.py` only publishes MRs whose `analysis.json` is
  `"complete": true`.

If you hit a 429 / "plan quota exceeded" during phase 2, log it with
`RATE_LIMIT` and exit non-zero. Do not partially update any analysis.json.

## What this routine no longer is

- A single monolithic Claude session that does everything. The fetch and
  publish phases are external scripts, so Claude only thinks during phase 2.
- The old `/review-team-jemish` slash command at
  `~/.claude/commands/review-team-jemish.md` is kept for reference and for
  on-demand manual reviews. It is not invoked by this scheduled task.

## Path constants

- fetch_mrs.py:    `/Users/jemmu/Work/python scripts/mr check/fetch_mrs.py`
- publish_review.py: `/Users/jemmu/Work/python scripts/mr check/publish_review.py`
- status.py:       `/Users/jemmu/Work/python scripts/mr check/status.py`
- common.py:       `/Users/jemmu/Work/python scripts/mr check/common.py`
- Project repo:    `/Users/jemmu/Work/python scripts/mr check/` (open in PyCharm)
- Config (.env):   `/Users/jemmu/Work/python scripts/mr check/.env` (gitignored, chmod 600)
- State:           `/Users/jemmu/.koinon-mr-reviews/state.json`
- Pending dir:     `/Users/jemmu/.koinon-mr-reviews/pending/<iid>/`
- Daily log:       `/Users/jemmu/.koinon-mr-reviews/logs/<YYYY-MM-DD>.log`
- PDF output:      `/Users/jemmu/mr reviews/MR<iid>_<author>_<new|old>_<dd.mm>.pdf`
- Format spec:     `/Users/jemmu/.claude/projects/-Users-jemmu-Work-Koinon/memory/feedback_mr_review_pdf.md`
- Koinon CLAUDE.md: `/Users/jemmu/Work/Koinon/CLAUDE.md`
