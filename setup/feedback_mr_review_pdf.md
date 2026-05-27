---
name: MR review → PDF deliverable
description: When the user asks for a merge request / PR review, also produce a PDF of the findings in a fixed multi-section format.
type: feedback
---
When the user asks me to review a merge request / PR (typically by pasting a GitLab/GitHub URL or saying "review MR <n>"), the deliverable they expect is:

1. A short chat summary with verdict + top issues.
2. A PDF saved to `/Users/jemmu/mr reviews/` with the filename `MR<number>_<author>_<new|old>_<dd.mm>.pdf` (GitLab MR) or `PR<number>_<author>_<new|old>_<dd.mm>.pdf` (GitHub PR). The PDF must follow the standard layout below.
   - `<author>` = MR/PR author's username
   - `<new|old>` = `new` if this is the first-ever review; `old` if re-review (previously reviewed at a different SHA)
   - `<dd.mm>` = review date in DD.MM format (e.g. `19.05`)

**Why:** User asked for a PDF the first time, then asked me to "make a rule" so they never have to ask again, then explicitly asked for this exact format ("give me format of pdf you have generated. also add it in claude file so i will get same format pdf every time") after the MR !2980 review.

**How to apply:**

Trigger on: "review this MR", "review MR <n>", "check this PR", a pasted GitLab/GitHub MR/PR URL, "study changes and tell me if everything is according to the claude.md rules". Do not ask whether to generate the PDF — generate it as part of the deliverable and mention the path at the end.

Build with Python 3 + `reportlab` (already installed system-wide on this machine — no venv needed). Save to `/Users/jemmu/mr reviews/MR<n>_<author>_<new|old>_<dd.mm>.pdf`.

## Required PDF structure (in order)

1. **Header block**
   - H1 title: `Merge Request Review — !<n>` (GitLab) or `Pull Request Review — #<n>` (GitHub)
   - Metadata lines, one per line, label bold:
     - Title (from MR/PR)
     - Source → Target branches (monospace)
     - Author
     - Reviewer
     - Commit SHA (short)
     - Related issue/work-item
     - Files changed | +X | -Y

2. **TL;DR** (H2) — one paragraph: what the MR does, scope, biggest concerns.

3. **Summary table** — columns `# | Severity | Area | Issue`. One row per finding. Use Paragraph cells so long issue text wraps; do not let columns overflow the page. Reserve ~12 cm for the Issue column on A4.

4. **Verdict** (H2) — short paragraph stating Approve / Request changes / Comment with one-line reason.

5. **Page break, then Detailed findings** (H1) — one subsection per issue:
   - Colored severity pill at the top (see scale below).
   - H2 heading: `N. <issue title>`.
   - Body paragraphs explaining the issue.
   - Optional sub-table for matrix data (e.g. before/after labels, line-by-line breakdown).
   - Optional code block (gray background, monospace) for snippets.
   - Always end with a concrete action sentence ("Rename X to Y", "Restore bracket to col 0", etc.).

6. **What looks good** (H2) — bullet list of genuine positives. Always include this even if short — keeps the review balanced.

7. **Appendix A — Full <thing> table** (H2) — line-by-line table of every change/rename/touch point. Highlight rows that change behavior with `#fff2cc` background. Use Paragraph cells with Courier 7-7.5 pt for key columns so long identifiers wrap inside the cell instead of overflowing.

8. **Suggested action list for the author** (H2) — numbered list, each item starts with an imperative verb. Last item is usually "Smoke-test …".

9. **Footer** (muted gray, small) — `Generated YYYY-MM-DD from MR commit <sha>`.

## Severity scale

| Severity | Color | Meaning |
|---|---|---|
| BLOCKER | `#b00020` | must fix before merge; security/correctness |
| MAJOR | `#d93f0b` | should fix before merge; meaningful impact |
| MINOR | `#bf8700` | nice to fix; cleanup or style |
| NIT | `#1f883d` | subjective polish |
| INFO | `#0969da` | context only, not an issue |

Render the pill as white bold text inside a colored Paragraph background, with `&nbsp;SEVERITY&nbsp;` content.

## Page & typography

- A4 portrait; margins 2 cm L/R, 1.6 cm T/B.
- Title color `#0a2540`; subheading color `#103a72`.
- Body Helvetica 10 pt, leading 13.
- Code blocks: Courier 8.5 pt, bg `#f4f5f7`, border `#dcdfe4`, 0.5 pt; small left/right padding.
- Tables: header bg `#0a2540` (or `#103a72` for sub-tables), white bold header text, `GRID` 0.4 pt `#dcdfe4`, zebra rows `[white, '#f7f8fa']` via `ROWBACKGROUNDS`. Highlight rows `#fff2cc`.
- Use `Paragraph` (not raw strings) in any table cell that contains text longer than ~25 chars so it wraps cleanly. Long monospace identifiers in Courier 7 pt cells.

## Data collection (before writing the PDF)

- Always run `glab mr view <n> -R politeia/koinon` and `glab mr diff <n> -R politeia/koinon` (or `gh pr view/diff` for GitHub) to get title, author, branches, SHA, files, and the actual diff.
- For Koinon, fetch the source branch into `origin/<branch>` (`git fetch origin <branch>`) and inspect with `git show <sha>` / `git grep <key> origin/<branch>` — do NOT check out the branch into the working tree if it would dirty unrelated files. If you must check out the changed files, `git checkout HEAD -- <paths>` afterwards.
- Verify every claim in the PDF against the actual code (file:line). Do not rely on memory of similar past MRs.

## What NOT to do

- Do not ask the user whether they want a PDF — just produce it.
- Do not skip the "What looks good" section even on a flawed MR.
- Do not use the old single-severity-table layout from the first version of this rule — the user explicitly asked for the richer multi-section format.
- For interactive (chat) reviews: PDF is a local file; do not post comments or push anything. For the daily automation path (`/review-team-jemish`) the rules differ — see below.

## Daily automation

The same PDF format feeds the `/review-team-jemish` slash command at `~/.claude/commands/review-team-jemish.md`, which is invoked by a scheduled routine at 09:00 daily ("Koinon daily MR review (Team Jemish)").

That workflow:
- Generates the PDF identically to the interactive path.
- Uploads it via `glab api projects/politeia%2Fkoinon/uploads -F "file=@…"` and posts a comment on each matching MR that attaches the PDF (per the CLAUDE.md MR-review-PDF carve-out, lines 770-775).
- Skips per-MR confirmation because the slash-command body itself contains the session-level authorization that CLAUDE.md:775 contemplates.
- Tracks reviewed `iid → sha` in `~/.koinon-mr-reviews/state.json` so a re-run only reviews MRs with new commits.
- The plan that defines this automation is at `~/.claude/plans/i-want-to-automate-dynamic-hartmanis.md`.

Constraints that apply to the automation specifically (in addition to everything above):
- Every MR comment **must** attach the `mr<iid>_review.pdf` — text-only MR comments are denied by CLAUDE.md:761.
- Do not upload PDFs speculatively; the upload must be part of the comment post (CLAUDE.md:775).
- The comment header must be unambiguously automated (`🤖 Automated review by Claude Code (via jemmu)`) — no impersonation.
- Failure on one MR must not abort the others; only update `state.json` for MRs whose full upload+post pipeline succeeded.
