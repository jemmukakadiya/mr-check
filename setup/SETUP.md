# Setup on a new Mac

End state: the Claude Code scheduled task **koinon-daily-mr-review-team-jemish**
fires every 2h, runs `fetch_mrs.py` → analyzes pending MRs → runs
`publish_review.py` to render a PDF, upload it to GitLab, and post the
comment.

The Python scripts in this repo are only one piece. The full reproduction
also needs:

- `glab` CLI installed and authenticated
- A `.env` file with the GitLab PAT
- The Koinon project cloned locally so the routine can read `CLAUDE.md`
- Two Claude files installed in `~/.claude/...` (shipped in this `setup/` folder)
- The scheduled task registered with Claude Code

---

## Prerequisites

1. **Python 3** (`python3 --version` → 3.10+). macOS system Python is fine.
2. **Homebrew** (`brew --version`).
3. **glab** — `brew install glab`, then `glab auth login` and point it at
   `gitlab.lrz.de` (or whatever `GITLAB_URL` is). Confirm with
   `glab mr list -R politeia/koinon --label "Team Jemish" --state opened`.
4. **Claude Code** — installed and signed in.
5. **`/Users/jemmu/Work/Koinon`** — clone the Koinon project here. The
   routine reads `Koinon/CLAUDE.md` and verifies findings against the
   real source files. The exact path matters because it's hard-coded in
   the SKILL.md.

---

## 1. Place this repo

Clone this repo to **exactly** `/Users/jemmu/Work/python scripts/mr check`:

```sh
mkdir -p "/Users/jemmu/Work/python scripts"
cd "/Users/jemmu/Work/python scripts"
git clone https://github.com/jemmukakadiya/mr-check.git "mr check"
cd "mr check"
```

The path with the space matters — it's hard-coded in `SKILL.md` and in
the user's existing memory. If you absolutely must put the repo somewhere
else, `sed`-replace the path inside `setup/SKILL.md` before running step 5.

---

## 2. Install Python deps

```sh
cd "/Users/jemmu/Work/python scripts/mr check"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Or skip the venv and use system Python — `reportlab` is the only
non-stdlib import.

---

## 3. Create the `.env`

```sh
cd "/Users/jemmu/Work/python scripts/mr check"
cp .env.example .env
chmod 600 .env
```

Open `.env` in an editor and paste in your GitLab PAT (needs the `api`
scope). If you stored it in the macOS Keychain on the old machine:

```sh
security find-generic-password -s 'claude gilab token ' -w
```

Smoke-test that the scripts can read the config:

```sh
python3 -c "from common import GITLAB_URL, GITLAB_PROJECT, MR_LABEL; \
            print(GITLAB_URL, GITLAB_PROJECT, repr(MR_LABEL))"
```

Should print `https://gitlab.lrz.de politeia/koinon 'Team Jemish'`.

---

## 4. Create the runtime directories

```sh
mkdir -p ~/.koinon-mr-reviews/pending ~/.koinon-mr-reviews/logs
[ -f ~/.koinon-mr-reviews/state.json ] || echo '{}' > ~/.koinon-mr-reviews/state.json
mkdir -p ~/"mr reviews"
```

If you want continuity with the old machine (no re-reviewing MRs that
were already reviewed there), copy the old machine's
`~/.koinon-mr-reviews/state.json` over the empty one you just created.

---

## 5. Install the Claude routine files

Run the helper script in this folder. It copies `SKILL.md` and
`feedback_mr_review_pdf.md` into the right `~/.claude/...` paths:

```sh
cd "/Users/jemmu/Work/python scripts/mr check/setup"
bash install.sh
```

Or do it manually:

```sh
mkdir -p ~/.claude/scheduled-tasks/koinon-daily-mr-review-team-jemish
cp SKILL.md ~/.claude/scheduled-tasks/koinon-daily-mr-review-team-jemish/SKILL.md

mkdir -p ~/.claude/projects/-Users-jemmu-Work-Koinon/memory
cp feedback_mr_review_pdf.md ~/.claude/projects/-Users-jemmu-Work-Koinon/memory/feedback_mr_review_pdf.md
```

---

## 6. Register the cron schedule

Open Claude Code in any project and ask:

> Use the schedule skill to register the scheduled task at
> `~/.claude/scheduled-tasks/koinon-daily-mr-review-team-jemish/SKILL.md`
> with the cron expression `0 9-21/2 * * *`.

That cron fires at 09:00, 11:00, 13:00, 15:00, 17:00, 19:00, 21:00 daily
(7 ticks/day). Confirm with the schedule skill's list command.

---

## 7. Smoke-test

Manual one-shot to confirm the plumbing works:

```sh
cd "/Users/jemmu/Work/python scripts/mr check"
python3 fetch_mrs.py
python3 status.py
```

If `status.py` shows any `draft` entries, the fetch worked. **Do NOT run
`publish_review.py` yet** — its job is to post real comments to GitLab.

To exercise the full pipeline end-to-end, let the scheduler fire it on
schedule, or trigger the routine manually through Claude Code (which will
do the phase-2 analysis before invoking `publish_review.py`).

To clean up the smoke test without publishing:

```sh
rm -rf ~/.koinon-mr-reviews/pending/*
```

(That leaves `state.json` intact, so on the next real run the affected
MRs will be re-fetched as "never seen".)

---

## File inventory — what's where

| What | Path |
|---|---|
| Scripts repo (this) | `/Users/jemmu/Work/python scripts/mr check/` |
| Config (gitignored) | `/Users/jemmu/Work/python scripts/mr check/.env` |
| Koinon project | `/Users/jemmu/Work/Koinon/` |
| Routine SKILL.md | `~/.claude/scheduled-tasks/koinon-daily-mr-review-team-jemish/SKILL.md` |
| PDF format spec | `~/.claude/projects/-Users-jemmu-Work-Koinon/memory/feedback_mr_review_pdf.md` |
| State / logs / pending | `~/.koinon-mr-reviews/` |
| PDF output dir | `~/mr reviews/` |

---

## Troubleshooting

- **`fetch_mrs.py` errors `Required config 'GITLAB_TOKEN' is not set`** — your
  `.env` isn't being read. Confirm it's at `/Users/jemmu/Work/python scripts/mr check/.env`
  and `chmod 600`.
- **`glab` says not authenticated** — `glab auth login` and pick GitLab Self-Managed,
  `gitlab.lrz.de`, paste the same PAT.
- **No notification on macOS** — System Settings → Notifications → Script Editor /
  Terminal: enable banners. The scripts use `osascript`; failures are non-fatal.
- **Routine doesn't fire at 09:00** — confirm with the schedule skill that the
  cron is registered; Claude Code must be running (or the launchd agent for it).
- **`publish_review.py` reports `skipped_incomplete`** — `analysis.json`
  is still `"complete": false`. Run the full routine through Claude Code,
  not just `publish_review.py`.
