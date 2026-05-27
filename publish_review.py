#!/usr/bin/env python3
"""
publish_review.py — Step 3 of the mr check pipeline (step 2 is the manual analysis).

For every pending package whose analysis.json has "complete": true:

  1. Render the PDF (reportlab) into ~/mr reviews/MR<iid>_<author>_<new|old>_<dd.mm>.pdf
  2. Upload it to GitLab via curl (POST /api/v4/projects/<encoded>/uploads)
  3. Compose a markdown comment that attaches the PDF link
  4. Post the comment via `glab mr note`
  5. Merge a new entry into state.json (sha, note_id, upload_url, timestamp)
  6. Append a line to today's log
  7. Remove the pending dir

Packages whose analysis.json is still flagged "complete": false are left alone
for next time. Failures on any one MR do not abort the others — and importantly
the state.json entry is only written when the full pipeline for that MR
succeeds, so a half-failed run can be retried cleanly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import (
    GITLAB_URL,
    GLAB,
    PDF_DIR,
    PENDING_DIR,
    PROJECT,
    PROJECT_ENCODED,
    SEVERITY_COLORS,
    SEVERITY_ORDER,
    VALID_VERDICTS,
    bootstrap,
    die,
    is_complete,
    load_state,
    log_line,
    merge_state,
    notify,
    previously_seen,
    run,
)


# --- PDF rendering ----------------------------------------------------------

def _import_reportlab() -> dict[str, Any]:
    """Lazy import so non-publish callers do not pay the reportlab startup cost."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, PageBreak,
    )
    return {
        "colors": colors, "A4": A4, "cm": cm, "Paragraph": Paragraph,
        "SimpleDocTemplate": SimpleDocTemplate, "Spacer": Spacer,
        "Table": Table, "TableStyle": TableStyle, "PageBreak": PageBreak,
        "ParagraphStyle": ParagraphStyle, "getSampleStyleSheet": getSampleStyleSheet,
    }


def _styles(rl: dict[str, Any]) -> dict[str, Any]:
    base = rl["getSampleStyleSheet"]()
    ParagraphStyle = rl["ParagraphStyle"]
    body = ParagraphStyle("body", parent=base["BodyText"],
                          fontName="Helvetica", fontSize=10, leading=13)
    h1 = ParagraphStyle("h1", parent=base["Heading1"],
                        fontName="Helvetica-Bold", fontSize=18, leading=22,
                        textColor=rl["colors"].HexColor("#0a2540"), spaceAfter=8)
    h2 = ParagraphStyle("h2", parent=base["Heading2"],
                        fontName="Helvetica-Bold", fontSize=13, leading=16,
                        textColor=rl["colors"].HexColor("#103a72"),
                        spaceBefore=10, spaceAfter=4)
    meta = ParagraphStyle("meta", parent=body, fontSize=9.5, leading=12)
    code = ParagraphStyle("code", parent=body, fontName="Courier", fontSize=8.5,
                          leading=10.5, backColor=rl["colors"].HexColor("#f4f5f7"),
                          borderColor=rl["colors"].HexColor("#dcdfe4"),
                          borderWidth=0.5, borderPadding=4, leftIndent=2, rightIndent=2)
    footer = ParagraphStyle("footer", parent=body, fontSize=8,
                            textColor=rl["colors"].HexColor("#666666"))
    return {"body": body, "h1": h1, "h2": h2, "meta": meta, "code": code, "footer": footer}


def _severity_pill(rl: dict[str, Any], styles: dict[str, Any], severity: str):
    color = rl["colors"].HexColor(SEVERITY_COLORS.get(severity, "#666666"))
    p = rl["Paragraph"](
        f'<b><font color="white">&nbsp;{severity}&nbsp;</font></b>',
        styles["body"],
    )
    t = rl["Table"]([[p]], colWidths=[2.6 * rl["cm"]])
    t.setStyle(rl["TableStyle"]([
        ("BACKGROUND", (0, 0), (-1, -1), color),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


def _para_text(s: str) -> str:
    """Plain text with \\n\\n breaks -> reportlab Paragraph markup."""
    s = _xml_escape(s)
    return s.replace("\n\n", "<br/><br/>").replace("\n", "<br/>")


def _summary_table(rl: dict[str, Any], styles: dict[str, Any], findings: list[dict[str, Any]]):
    P = rl["Paragraph"]
    rows: list[list[Any]] = [["#", "Severity", "Area", "Issue"]]
    for i, f in enumerate(findings, 1):
        rows.append([
            str(i),
            f.get("severity", "INFO"),
            P(_xml_escape(f.get("area", "")), styles["body"]),
            P(_xml_escape(f.get("title", "")), styles["body"]),
        ])
    col_widths = [0.9 * rl["cm"], 2.2 * rl["cm"], 3.3 * rl["cm"], 11.0 * rl["cm"]]
    t = rl["Table"](rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(rl["TableStyle"]([
        ("BACKGROUND", (0, 0), (-1, 0), rl["colors"].HexColor("#0a2540")),
        ("TEXTCOLOR", (0, 0), (-1, 0), rl["colors"].white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("GRID", (0, 0), (-1, -1), 0.4, rl["colors"].HexColor("#dcdfe4")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [rl["colors"].white, rl["colors"].HexColor("#f7f8fa")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _appendix_table(rl: dict[str, Any], styles: dict[str, Any], appendix: dict[str, Any]):
    headers = appendix.get("headers", [])
    rows_raw = appendix.get("rows", [])
    highlight = set(appendix.get("highlight_rows", []) or [])
    P = rl["Paragraph"]
    code_style = rl["ParagraphStyle"](
        "appendix_code", parent=styles["body"],
        fontName="Courier", fontSize=7.5, leading=9.5,
    )
    body_small = rl["ParagraphStyle"](
        "appendix_body", parent=styles["body"], fontSize=8.5, leading=10.5,
    )
    rows: list[list[Any]] = [[P(f"<b>{_xml_escape(h)}</b>", body_small) for h in headers]]
    for r in rows_raw:
        rows.append([
            P(_xml_escape(str(c)), code_style if i in (0,) else body_small)
            for i, c in enumerate(r)
        ])

    t = rl["Table"](rows, repeatRows=1)
    style_cmds: list[tuple] = [
        ("BACKGROUND", (0, 0), (-1, 0), rl["colors"].HexColor("#103a72")),
        ("TEXTCOLOR", (0, 0), (-1, 0), rl["colors"].white),
        ("GRID", (0, 0), (-1, -1), 0.4, rl["colors"].HexColor("#dcdfe4")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [rl["colors"].white, rl["colors"].HexColor("#f7f8fa")]),
    ]
    for i in highlight:
        # +1 because row 0 is the header
        style_cmds.append(("BACKGROUND", (0, i + 1), (-1, i + 1),
                           rl["colors"].HexColor("#fff2cc")))
    t.setStyle(rl["TableStyle"](style_cmds))
    return t


def render_pdf(pdf_path: Path, mr: dict[str, Any], analysis: dict[str, Any]) -> None:
    """Build the full multi-section PDF per the project's review format spec.

    The spec lives at:
      ~/.claude/projects/-Users-jemmu-Work-Koinon/memory/feedback_mr_review_pdf.md
    """
    rl = _import_reportlab()
    styles = _styles(rl)
    P = rl["Paragraph"]
    Spacer = rl["Spacer"]
    cm = rl["cm"]

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = rl["SimpleDocTemplate"](
        str(pdf_path),
        pagesize=rl["A4"],
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=1.6 * cm, bottomMargin=1.6 * cm,
        title=f"MR Review !{mr['iid']}",
    )

    story: list[Any] = []
    iid = mr["iid"]
    title = mr.get("title", "")
    author = mr.get("author", {}).get("username", "?")
    src = mr.get("source_branch", "?")
    tgt = mr.get("target_branch", "?")
    sha = mr.get("sha", "")
    sha_short = sha[:8]

    # Header
    story.append(P(f"Merge Request Review — !{iid}", styles["h1"]))
    meta_lines = [
        f"<b>Title:</b> {_xml_escape(title)}",
        f"<b>Source → Target:</b> <font face='Courier'>{_xml_escape(src)} → {_xml_escape(tgt)}</font>",
        f"<b>Author:</b> @{_xml_escape(author)}",
        f"<b>Reviewer:</b> Team Jemish Bot (via jemmu)",
        f"<b>Commit SHA:</b> <font face='Courier'>{_xml_escape(sha_short)}</font>",
    ]
    web_url = mr.get("web_url")
    if web_url:
        meta_lines.append(f"<b>URL:</b> {_xml_escape(web_url)}")
    for ln in meta_lines:
        story.append(P(ln, styles["meta"]))
    story.append(Spacer(1, 8))

    # TL;DR
    story.append(P("TL;DR", styles["h2"]))
    story.append(P(_para_text(analysis.get("tldr", "")), styles["body"]))
    story.append(Spacer(1, 6))

    # Task coverage (only if linked task was found and the analyst filled it in)
    task_coverage = analysis.get("task_coverage")
    if isinstance(task_coverage, dict) and task_coverage.get("task_iid"):
        story.append(P("Task coverage", styles["h2"]))
        t_iid = task_coverage.get("task_iid")
        t_title = task_coverage.get("task_title", "")
        t_state = task_coverage.get("task_state", "")
        header_line = f"<b>Linked task:</b> #{t_iid}"
        if t_title:
            header_line += f" — {_xml_escape(t_title)}"
        if t_state:
            header_line += f"  <font color='#666666'>(state: {_xml_escape(t_state)})</font>"
        story.append(P(header_line, styles["body"]))

        def _coverage_bullets(label: str, items: list, marker: str, color: str | None = None) -> None:
            if not items:
                return
            story.append(Spacer(1, 4))
            story.append(P(f"<b>{label}:</b>", styles["body"]))
            for it in items:
                line = f"{marker} {_xml_escape(str(it))}"
                if color:
                    line = f"<font color='{color}'>{line}</font>"
                story.append(P(line, styles["body"]))

        _coverage_bullets("Addresses",
                          task_coverage.get("addresses") or [],
                          "✓", "#1f883d")
        _coverage_bullets("Partially addresses",
                          task_coverage.get("partially_addresses") or [],
                          "⚠", "#bf8700")
        _coverage_bullets("Not addressed",
                          task_coverage.get("not_addressed") or [],
                          "✗", "#b00020")
        _coverage_bullets("Out of scope (changes not in the task)",
                          task_coverage.get("out_of_scope_changes") or [],
                          "•", "#0969da")

        notes = (task_coverage.get("notes") or "").strip()
        if notes:
            story.append(Spacer(1, 4))
            story.append(P(f"<b>Notes:</b> {_xml_escape(notes)}", styles["body"]))

        story.append(Spacer(1, 8))

    # Findings summary table
    findings = analysis.get("findings", []) or []
    if findings:
        story.append(P("Findings summary", styles["h2"]))
        story.append(_summary_table(rl, styles, findings))
        story.append(Spacer(1, 8))

    # Verdict
    story.append(P("Verdict", styles["h2"]))
    verdict = analysis.get("verdict", "Comment")
    reason = analysis.get("verdict_reason", "")
    story.append(P(f"<b>{_xml_escape(verdict)}.</b> {_xml_escape(reason)}", styles["body"]))

    # Detailed findings (page break first)
    if findings:
        story.append(rl["PageBreak"]())
        story.append(P("Detailed findings", styles["h1"]))
        for i, f in enumerate(findings, 1):
            story.append(_severity_pill(rl, styles, f.get("severity", "INFO")))
            story.append(Spacer(1, 4))
            story.append(P(f"{i}. {_xml_escape(f.get('title', ''))}", styles["h2"]))
            story.append(P(_para_text(f.get("body", "")), styles["body"]))
            snippet = f.get("code_snippet") or ""
            if snippet.strip():
                snippet_html = _xml_escape(snippet).replace("\n", "<br/>")
                story.append(Spacer(1, 4))
                story.append(P(snippet_html, styles["code"]))
            action = f.get("action") or ""
            if action.strip():
                story.append(Spacer(1, 4))
                story.append(P(f"<b>Action:</b> {_xml_escape(action)}", styles["body"]))
            story.append(Spacer(1, 10))

    # What looks good
    positives = analysis.get("what_looks_good") or []
    story.append(P("What looks good", styles["h2"]))
    if positives:
        for item in positives:
            story.append(P(f"• {_xml_escape(item)}", styles["body"]))
    else:
        story.append(P("(none recorded)", styles["body"]))

    # Appendix A
    appendix = analysis.get("appendix")
    if appendix and appendix.get("rows"):
        story.append(Spacer(1, 10))
        story.append(P(
            f"Appendix A — {_xml_escape(appendix.get('title', 'Details'))}",
            styles["h2"],
        ))
        story.append(_appendix_table(rl, styles, appendix))

    # Suggested action list
    actions = analysis.get("actions") or []
    if actions:
        story.append(Spacer(1, 10))
        story.append(P("Suggested action list", styles["h2"]))
        for i, a in enumerate(actions, 1):
            story.append(P(f"{i}. {_xml_escape(a)}", styles["body"]))

    # Footer
    story.append(Spacer(1, 14))
    today = datetime.now().strftime("%Y-%m-%d")
    story.append(P(f"Generated {today} from MR commit {sha_short}", styles["footer"]))

    doc.build(story)


# --- Upload + comment --------------------------------------------------------

def upload_pdf(pdf_path: Path) -> dict[str, Any]:
    """Upload a PDF to <project>/uploads via curl.

    NOTE: `glab api -F file=@…` currently returns HTTP 400
    {"error":"file is invalid"} on gitlab.lrz.de (confirmed 2026-05-27).
    Direct curl multipart works fine and bypasses whatever glab does to
    the request body.
    """
    token = os.environ.get("GITLAB_TOKEN", "")
    if not token:
        die("GITLAB_TOKEN not set — upload would fail")
    url = f"{GITLAB_URL}/api/v4/projects/{PROJECT_ENCODED}/uploads"
    cp = run([
        "curl", "-sS", "--fail-with-body",
        "-X", "POST",
        "-H", f"PRIVATE-TOKEN: {token}",
        "-F", f"file=@{pdf_path}",
        url,
    ], check=False)
    if cp.returncode != 0:
        die(
            f"upload failed (curl exit {cp.returncode}). "
            f"stdout: {cp.stdout[:400]!r} stderr: {cp.stderr[:400]!r}"
        )
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        die(f"upload failed: not JSON. stdout: {cp.stdout[:400]!r} stderr: {cp.stderr[:400]!r} ({e})")


def post_comment(iid: int, body: str) -> int | None:
    """Post a comment via glab. Returns note id if parsable from glab's URL output."""
    cp = run([GLAB, "mr", "note", str(iid), "-R", PROJECT, "-m", body])
    stdout = cp.stdout.decode("utf-8", errors="replace")
    # glab prints a URL like .../merge_requests/<iid>#note_<id>
    m = re.search(r"#note_(\d+)", stdout)
    return int(m.group(1)) if m else None


def compose_comment(analysis: dict[str, Any], upload_markdown: str, sha_short: str) -> str:
    """Build the markdown comment body. The PDF attachment is mandatory
    (CLAUDE.md line 761 forbids text-only MR comments)."""
    verdict = analysis.get("verdict", "Comment")
    tldr = analysis.get("tldr", "").strip().splitlines()[0] if analysis.get("tldr") else ""
    findings = analysis.get("findings", []) or []
    findings_sorted = sorted(
        findings,
        key=lambda f: SEVERITY_ORDER.index(f.get("severity", "INFO"))
        if f.get("severity", "INFO") in SEVERITY_ORDER else 99,
    )
    top = findings_sorted[:5]
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"🤖 **Team Jemish Bot** — Verdict: {verdict}",
        "",
        tldr,
        "",
    ]
    if top:
        lines.append("**Top findings:**")
        for f in top:
            sev = f.get("severity", "INFO")
            short = (f.get("title") or "").strip()
            lines.append(f"- {sev}: {short}")
        lines.append("")
    lines.append(f"📎 Full review (attached): {upload_markdown}")
    lines.append("")
    lines.append(
        f"_Generated {today} from commit `{sha_short}`. "
        f"The PDF is the source of truth — this comment is a summary._"
    )
    return "\n".join(lines)


# --- Per-MR pipeline --------------------------------------------------------

@dataclass
class PublishResult:
    iid: str
    status: str   # posted | skipped_incomplete | failed
    detail: str = ""


def _publish_one(pkg: Path, state: dict[str, dict[str, Any]]) -> PublishResult:
    """Render+upload+post for one pending package. Updates state.json on success."""
    iid = pkg.name
    analysis_path = pkg / "analysis.json"
    mr_path = pkg / "mr.json"
    if not analysis_path.exists() or not mr_path.exists():
        return PublishResult(iid, "failed", "missing analysis.json or mr.json")
    try:
        analysis = json.loads(analysis_path.read_text())
        mr = json.loads(mr_path.read_text())
    except json.JSONDecodeError as e:
        return PublishResult(iid, "failed", f"invalid JSON: {e}")

    if not is_complete(analysis):
        return PublishResult(iid, "skipped_incomplete",
                             "analysis.json not marked complete")

    verdict = analysis.get("verdict")
    if verdict not in VALID_VERDICTS:
        return PublishResult(iid, "failed",
                             f"verdict {verdict!r} not one of {sorted(VALID_VERDICTS)}")

    author = mr.get("author", {}).get("username", "unknown")
    review_type = "old" if previously_seen(state, iid) else "new"
    dd_mm = datetime.now().strftime("%d.%m")
    pdf_filename = f"MR{iid}_{author}_{review_type}_{dd_mm}.pdf"
    pdf_path = PDF_DIR / pdf_filename

    # 1. Render
    try:
        render_pdf(pdf_path, mr, analysis)
        if not pdf_path.exists() or pdf_path.stat().st_size == 0:
            raise RuntimeError("PDF rendered but file missing or empty")
    except Exception as e:
        return PublishResult(iid, "failed", f"PDF render: {e}")

    # 2. Upload
    try:
        upload = upload_pdf(pdf_path)
        upload_markdown = upload.get("markdown")
        upload_url = upload.get("url")
        if not upload_markdown:
            raise RuntimeError(f"upload response missing 'markdown': {upload}")
    except Exception as e:
        return PublishResult(iid, "failed", f"upload: {e}")

    # 3. Compose + post
    sha_short = mr.get("sha", "")[:8]
    body = compose_comment(analysis, upload_markdown, sha_short)
    if upload_markdown not in body:
        return PublishResult(iid, "failed", "comment body missing PDF attachment")
    try:
        note_id = post_comment(int(iid), body)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="replace")
        return PublishResult(iid, "failed", f"glab mr note failed: {stderr[:200]}")

    # 4. Persist state — only on full success
    merge_state(iid, {
        "last_reviewed_sha": mr["sha"],
        "last_reviewed_at": datetime.now(timezone.utc).isoformat(),
        "last_note_id": note_id,
        "last_upload_url": upload_url,
    })

    # 5. Log
    log_line(
        f"{datetime.now().isoformat()}  iid={iid}  sha={sha_short}  "
        f"verdict={verdict}  note={note_id}  pdf={pdf_filename}"
    )

    # 6. Remove pending dir
    shutil.rmtree(pkg)

    return PublishResult(iid, "posted", f"note={note_id} pdf={pdf_filename}")


def main() -> int:
    bootstrap()

    if not PENDING_DIR.exists():
        print("No pending packages — nothing to do.")
        return 0

    state = load_state()
    results: list[PublishResult] = []
    for pkg in sorted(PENDING_DIR.iterdir()):
        if not pkg.is_dir():
            continue
        results.append(_publish_one(pkg, state))
        state = load_state()  # refresh after each merge_state

    posted = [r for r in results if r.status == "posted"]
    incomplete = [r for r in results if r.status == "skipped_incomplete"]
    failed = [r for r in results if r.status == "failed"]

    print(f"\nPublish summary: posted={len(posted)}  incomplete={len(incomplete)}  failed={len(failed)}")
    for r in results:
        print(f"  - MR !{r.iid}: {r.status}  {r.detail}")

    if posted or failed:
        notify(
            "Koinon daily MR review",
            f"Posted: {len(posted)} · Failed: {len(failed)} · Incomplete: {len(incomplete)}",
        )

    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
