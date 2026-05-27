"""
common.py — shared library for the mr check scripts.

Imports of this module trigger:
  - .env loading (so GITLAB_TOKEN etc. are available)
  - Constant resolution (PROJECT, LABEL, GITLAB_URL, paths, severity scale)

Exposes:
  - die / run / glab_json                — subprocess + error helpers
  - log_line                              — append to ~/.koinon-mr-reviews/logs/<date>.log
  - load_state / save_state / merge_state — JSON state file management
  - MRDecision / enumerate_and_decide     — MR fetch + bucket logic (used by fetch_mrs.py)
  - ANALYSIS_TEMPLATE                     — empty analysis.json shape
  - is_complete / previously_seen         — predicates used by publish_review.py
  - load_token / bootstrap                — explicit token load + env propagation

Not exposed: PDF rendering or upload — those are in publish_review.py since
they are only used during the publish step.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --- .env loading -----------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file. Existing env vars take precedence."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ENV_FILE)


def _env(key: str, default: str | None = None, *, required: bool = False) -> str:
    """Read an env var; abort if required and missing."""
    val = os.environ.get(key, default if default is not None else "").strip()
    if required and not val:
        die(
            f"Required config {key!r} is not set. "
            f"Either export it in your shell or add it to {ENV_FILE}. "
            f"See {ENV_FILE.with_name('.env.example')} for the template."
        )
    return val


# --- Constants --------------------------------------------------------------

PROJECT = _env("GITLAB_PROJECT", "politeia/koinon")
PROJECT_ENCODED = PROJECT.replace("/", "%2F")
LABEL = _env("MR_LABEL", "Team Jemish")
GITLAB_URL = _env("GITLAB_URL", "https://gitlab.lrz.de").rstrip("/")

HOME = Path.home()
ROOT = Path(_env("MR_REVIEW_ROOT", str(HOME / ".koinon-mr-reviews")))
STATE_FILE = ROOT / "state.json"
LOG_DIR = ROOT / "logs"
PENDING_DIR = ROOT / "pending"
PDF_DIR = Path(_env("MR_REVIEW_PDF_DIR", str(HOME / "mr reviews")))

GLAB = shutil.which("glab") or "/opt/homebrew/bin/glab"

SEVERITY_COLORS = {
    "BLOCKER": "#b00020",
    "MAJOR":   "#d93f0b",
    "MINOR":   "#bf8700",
    "NIT":     "#1f883d",
    "INFO":    "#0969da",
}
SEVERITY_ORDER = ["BLOCKER", "MAJOR", "MINOR", "NIT", "INFO"]

VALID_VERDICTS = {"Approve", "Request changes", "Comment"}


# --- Token & shell helpers --------------------------------------------------

def die(msg: str, code: int = 1) -> None:
    """Print to stderr and exit non-zero."""
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd: list[str], *, input_bytes: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess inheriting the current env (with GITLAB_TOKEN exported)."""
    return subprocess.run(cmd, input=input_bytes, capture_output=True, check=check)


def glab_json(args: list[str]) -> Any:
    """Invoke glab with --output json (or similar) and parse the JSON response."""
    cp = run([GLAB, *args])
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        die(f"glab {' '.join(args)} did not return JSON: {e}\nstdout: {cp.stdout[:400]!r}")


def load_token() -> str:
    """Read GITLAB_TOKEN from the environment (populated by .env at module load)."""
    return _env("GITLAB_TOKEN", required=True)


def bootstrap() -> None:
    """Call at the top of each entry-point script.

    Ensures GITLAB_TOKEN and GITLAB_HOST are present in os.environ so child
    processes (glab, curl) inherit them. Without GITLAB_HOST, glab falls back
    to gitlab.com instead of the configured instance. Idempotent — safe to
    call repeatedly.
    """
    os.environ["GITLAB_TOKEN"] = load_token()
    # glab reads GITLAB_HOST (hostname only, no scheme) to select the
    # configured GitLab instance. Derive it from GITLAB_URL so subprocess
    # calls don't silently fall back to gitlab.com.
    if "GITLAB_HOST" not in os.environ:
        parsed = urlparse(GITLAB_URL)
        if parsed.hostname:
            os.environ["GITLAB_HOST"] = parsed.hostname


# --- Logging ----------------------------------------------------------------

def log_line(line: str) -> None:
    """Append a line to today's log file and echo to stdout."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    with log_file.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")
    print(line.rstrip())


# --- State management -------------------------------------------------------

def load_state() -> dict[str, dict[str, Any]]:
    """Load state.json or return an empty dict if it does not exist."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError as e:
        die(f"state.json is corrupt: {e}")


def save_state(state: dict[str, dict[str, Any]]) -> None:
    """Atomically write state.json (tmp file + rename)."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=False))
    tmp.replace(STATE_FILE)


def merge_state(iid: str, entry: dict[str, Any]) -> None:
    """Merge one MR's record into state.json without clobbering other entries."""
    state = load_state()
    state[iid] = entry
    save_state(state)


# --- MR enumeration & decision ---------------------------------------------

@dataclass
class MRDecision:
    """One MR + the bucket the decision logic put it in."""
    mr: dict[str, Any]
    action: str  # "new_reviewed" | "re_reviewed" | "skipped_no_new_commits" | "out_of_window"
    reason: str
    review_type: str  # "new" | "old"


def enumerate_and_decide() -> list[MRDecision]:
    """List every open MR labeled LABEL and bucket each one per state.json.

    Decision rules:
      - iid in state, sha unchanged    -> skipped_no_new_commits
      - iid in state, sha changed      -> re_reviewed  (review_type = "old")
      - iid NOT in state, <24h old     -> new_reviewed (review_type = "new")
      - iid NOT in state, >=24h old    -> out_of_window
    """
    mrs = glab_json([
        "mr", "list", "-R", PROJECT,
        "--label", LABEL,
        "--output", "json",
    ])
    state = load_state()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    decisions: list[MRDecision] = []
    for m in mrs:
        iid = str(m["iid"])
        sha = m["sha"]
        created = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00"))
        prior = state.get(iid)
        if prior:
            if prior["last_reviewed_sha"] == sha:
                decisions.append(MRDecision(m, "skipped_no_new_commits",
                                            "already reviewed at current sha", "old"))
            else:
                decisions.append(MRDecision(m, "re_reviewed",
                                            "new commits since last review", "old"))
        else:
            if created >= cutoff:
                decisions.append(MRDecision(m, "new_reviewed",
                                            "new MR in last 24h", "new"))
            else:
                decisions.append(MRDecision(m, "out_of_window",
                                            "never seen, older than 24h", "new"))
    return decisions


# --- Linked task (issue) detection + fetch ---------------------------------

# Branches that follow the `<iid>-<slug>` convention are the most reliable
# signal. The MR description's `#<n>` / `Closes #<n>` mentions are a fallback.
_BRANCH_ISSUE_RE = re.compile(r"^(\d+)\b")
_DESC_ISSUE_RE = re.compile(r"[#!](\d{1,6})\b")


def extract_issue_iid(mr: dict[str, Any]) -> int | None:
    """Pick a linked-issue iid from an MR JSON, or None.

    Order of preference:
      1. Branch name prefix matching `^\\d+-` (Koinon's convention is
         `<issue_iid>-<slug>`).
      2. The first `#<n>` or `!<n>` reference in the MR description.
    """
    branch = str(mr.get("source_branch", "") or "")
    m = _BRANCH_ISSUE_RE.match(branch)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    desc = str(mr.get("description", "") or "")
    m = _DESC_ISSUE_RE.search(desc)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def fetch_issue(iid: int) -> dict[str, Any] | None:
    """Fetch a single issue via `glab issue view`. Returns None on failure.

    Failure is *not fatal* — an MR may legitimately reference an issue that
    has been moved, closed-and-deleted, or that lives in another project.
    The caller should fall back to no task data.
    """
    cp = run(
        [GLAB, "issue", "view", str(iid), "-R", PROJECT, "--output", "json"],
        check=False,
    )
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# --- analysis.json template + predicates -----------------------------------

ANALYSIS_TEMPLATE: dict[str, Any] = {
    "complete": False,
    "_instructions": (
        "Fill in this analysis, then set 'complete' to true. Run "
        "publish_review.py to render+upload+post. "
        "See ~/.claude/projects/-Users-jemmu-Work-Koinon/memory/feedback_mr_review_pdf.md "
        "for the format the PDF will follow."
    ),
    "verdict": "Comment",
    "verdict_reason": "One-line reason for the verdict.",
    "tldr": "One paragraph: what the MR does, scope, biggest concerns.",
    # Task coverage — populated only if a linked task was found at fetch time.
    # If task.json does not exist in the pending dir, leave this object null.
    # The structure mirrors the four buckets Phase 2 should populate:
    #   addresses             — task items fully implemented by the MR
    #   partially_addresses   — task items started but incomplete
    #   not_addressed         — task items not touched at all
    #   out_of_scope_changes  — MR changes that aren't in the task (scope creep)
    "task_coverage": {
        "task_iid": 0,
        "task_title": "",
        "task_state": "",
        "addresses": [
            "Item from the task that the diff fully implements."
        ],
        "partially_addresses": [],
        "not_addressed": [],
        "out_of_scope_changes": [],
        "notes": ""
    },
    "findings": [
        {
            "severity": "INFO",
            "area": "Example",
            "title": "Replace this example finding",
            "body": "Body of the finding — multiple paragraphs are fine. Reference file:line where relevant.",
            "code_snippet": "",
            "action": "Imperative action sentence ending the finding.",
        }
    ],
    "what_looks_good": [
        "First positive observation.",
    ],
    "appendix": None,  # or {"title": ..., "headers": [...], "rows": [[...]], "highlight_rows": []}
    "actions": [
        "Imperative action 1.",
        "Smoke-test the affected flow.",
    ],
}


def is_complete(analysis: dict[str, Any]) -> bool:
    """An analysis.json is ready for publish when it is flagged complete
    AND has a list of findings (catches the 'I set complete=true on the
    template by accident' case)."""
    return bool(analysis.get("complete")) and isinstance(analysis.get("findings"), list)


def previously_seen(state: dict[str, dict[str, Any]], iid: str) -> bool:
    """Whether we have ever posted a review on this MR (controls new vs old in filenames)."""
    return iid in state


# --- macOS notification ----------------------------------------------------

def notify(title: str, message: str) -> None:
    """Best-effort macOS notification. Silent if osascript is unavailable."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            check=False,
        )
    except FileNotFoundError:
        pass
