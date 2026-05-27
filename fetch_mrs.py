#!/usr/bin/env python3
"""
fetch_mrs.py — Step 1 of the mr check pipeline.

Fetches open MRs labeled "Team Jemish" in `politeia/koinon`, decides which
ones need a (re-)review against ~/.koinon-mr-reviews/state.json, and stages
a per-MR package under ~/.koinon-mr-reviews/pending/<iid>/ that contains:

  mr.json        — full MR JSON (from `glab mr view`)
  mr.diff        — raw unified diff
  context.md     — short human-readable summary
  analysis.json  — empty template the analysis step fills in

After this script returns, the analyst (you, or Claude inside the routine)
opens each pending dir, reads mr.diff + context.md, fills in analysis.json,
and flips "complete": true. The next step of the pipeline is publish_review.py.

This script does NOT generate PDFs, upload anything, or post comments.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from common import (
    ANALYSIS_TEMPLATE,
    GLAB,
    LABEL,
    PENDING_DIR,
    PROJECT,
    MRDecision,
    bootstrap,
    enumerate_and_decide,
    extract_issue_iid,
    fetch_issue,
    glab_json,
    log_line,
    notify,
    run,
)


def write_pending(decision: MRDecision) -> Path:
    """Materialize one MR's package under pending/<iid>/.

    Re-running on an already-staged MR refreshes mr.json + mr.diff (in case
    new commits have landed) but never overwrites analysis.json — so any
    in-flight analysis is preserved.
    """
    m = decision.mr
    iid = str(m["iid"])
    pkg = PENDING_DIR / iid
    pkg.mkdir(parents=True, exist_ok=True)

    # mr.json — the richer view (description, full file list, etc.)
    view = glab_json(["mr", "view", iid, "-R", PROJECT, "--output", "json"])
    (pkg / "mr.json").write_text(json.dumps(view, indent=2))

    # mr.diff — raw unified diff text
    diff = run([GLAB, "mr", "diff", iid, "-R", PROJECT]).stdout
    (pkg / "mr.diff").write_bytes(diff)

    # Linked task (if any) — fetch and stage alongside the MR. Phase 2 reads
    # task.md to verify whether the diff implements every requirement.
    task_iid = extract_issue_iid({**m, **view})  # merge for description access
    task: dict[str, Any] | None = None
    if task_iid is not None:
        task = fetch_issue(task_iid)
        if task is not None:
            (pkg / "task.json").write_text(json.dumps(task, indent=2))
            task_md = (
                f"# Task #{task_iid}: {task.get('title', '')}\n\n"
                f"- State:  {task.get('state', '?')}\n"
                f"- Labels: {', '.join(task.get('labels', []) or [])}\n"
                f"- URL:    {task.get('web_url', '')}\n"
                f"\n## Description\n\n"
                f"{task.get('description', '_(no description)_')}\n"
            )
            (pkg / "task.md").write_text(task_md)

    # context.md — at-a-glance index for the analyst
    files = view.get("changes_count") or view.get("files_count") or ""
    task_line = (
        f"- Linked task:  #{task_iid} — {task.get('title', '?')}\n"
        if task is not None
        else (f"- Linked task:  #{task_iid} (NOT FOUND via glab)\n"
              if task_iid is not None
              else "- Linked task:  (none detected in branch or description)\n")
    )
    context = (
        f"# MR !{iid}: {view.get('title', '')}\n\n"
        f"- Author:       {m['author']['username']}\n"
        f"- Branches:     {view.get('source_branch')} → {view.get('target_branch')}\n"
        f"- Commit SHA:   {m['sha']}\n"
        f"- Created:      {m['created_at']}\n"
        f"- Files changed: {files}\n"
        f"- Web URL:      {m.get('web_url', '')}\n"
        f"- Review type:  {decision.review_type}\n"
        f"- Reason:       {decision.reason}\n"
        f"{task_line}"
    )
    (pkg / "context.md").write_text(context)

    # analysis.json — only write the template when starting fresh. Never
    # overwrite a draft (the analyst may be partway through editing it).
    analysis_path = pkg / "analysis.json"
    if not analysis_path.exists():
        # Seed task_coverage from the fetched issue so the analyst sees the
        # right iid/title without having to look it up. Leave it null if no
        # task was found — that signals "no task linkage for this MR".
        template = json.loads(json.dumps(ANALYSIS_TEMPLATE))  # deep copy
        if task is not None:
            tc = template["task_coverage"]
            tc["task_iid"] = task_iid
            tc["task_title"] = task.get("title", "")
            tc["task_state"] = task.get("state", "")
        else:
            template["task_coverage"] = None
        analysis_path.write_text(json.dumps(template, indent=2))

    return pkg


def main() -> int:
    bootstrap()  # populate GITLAB_TOKEN in os.environ for child processes

    decisions = enumerate_and_decide()
    if not decisions:
        log_line(f"{datetime.now().isoformat()}  fetch_mrs: no MRs labeled {LABEL!r}")
        return 0

    to_stage = [d for d in decisions if d.action in ("new_reviewed", "re_reviewed")]
    counts = {a: 0 for a in (
        "new_reviewed", "re_reviewed", "skipped_no_new_commits", "out_of_window"
    )}
    for d in decisions:
        counts[d.action] += 1

    print(f"Found {len(decisions)} open MRs labeled {LABEL!r}:")
    print(f"  new_reviewed:           {counts['new_reviewed']}")
    print(f"  re_reviewed:            {counts['re_reviewed']}")
    print(f"  skipped_no_new_commits: {counts['skipped_no_new_commits']}")
    print(f"  out_of_window:          {counts['out_of_window']}")

    if not to_stage:
        log_line(f"{datetime.now().isoformat()}  fetch_mrs: nothing to stage")
        return 0

    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nStaging {len(to_stage)} MR(s) under {PENDING_DIR}:")
    for d in to_stage:
        pkg = write_pending(d)
        iid = str(d.mr["iid"])
        author = d.mr["author"]["username"]
        sha_short = d.mr["sha"][:8]
        print(f"  - MR !{iid}  by @{author}  @{sha_short}  ({d.action})  → {pkg}")

    print(
        "\nNext: fill in analysis.json in each pending dir, set "
        "'complete': true, then run:\n  python3 publish_review.py"
    )
    log_line(
        f"{datetime.now().isoformat()}  fetch_mrs: staged={len(to_stage)} "
        f"new={counts['new_reviewed']} re={counts['re_reviewed']} "
        f"waiting={counts['skipped_no_new_commits']} oow={counts['out_of_window']}"
    )

    iids = ", ".join(f"!{d.mr['iid']}" for d in to_stage)
    notify(
        "Koinon MR review — analysis needed",
        f"{len(to_stage)} MR(s) need analysis: {iids}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
