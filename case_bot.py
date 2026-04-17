#!/usr/bin/env python3
"""
case_bot.py — Mend Support Toolkit Slack Bot (Socket Mode).

Always-running bot that listens for DM commands and orchestrates case management.

Commands:
  case <NUMBER>              — fetch SF case, create folder, download attachments
  staging                    — show + process all cases in the SF staging queue
  claim <NUMBER>             — take ownership (AI-suggested Domain/Sub-category)
  summarize <NUMBER>         — AI summary of the case via Claude CLI
  tka <NUMBER>               — AI-drafted TKA Jira ticket via Claude CLI
  logs <NUMBER>              — show RESOURCES/ files with token counts
  move <NUMBER> <filename>   — move a file to large_files/
  move <NUMBER> all          — move all large files to large_files/
  digest                     — on-demand daily digest
  watch                      — list cases being watched
  unwatch <NUMBER>           — stop watching a case
  config show                — show current config
  config <key> <value>       — update a config value
  setup                      — show setup status
  help                       — show this list

Pending-confirmation flows:
  claim  → approve | override <domain> <subcategory>
  tka    → confirm | cancel
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.response import SocketModeResponse

from utils import (
    TOOLKIT_DIR,
    _slack_post,
    _slack_update,
    build_digest_message,
    estimate_tokens,
    fetch_and_write_summary,
    fetch_staging_report,
    find_similar_cases,
    get_sf_picklist_values,
    get_sf_user_id,
    get_token_report,
    load_config,
    load_state,
    log,
    regenerate_claude_md,
    run_soql,
    save_config,
    save_state,
    sync_case_attachments,
    update_sf_case,
    SF_BASE_URL,
)

SOURCE = "BOT"


# ── Pending confirmations ─────────────────────────────────────────────────────
# Keyed by channel ID.  Each entry: {"type": str, "data": dict, "thread_ts": str|None}

PENDING: dict = {}


# ── Logging / reply helpers ───────────────────────────────────────────────────

def _log(msg: str):
    log(msg, SOURCE)


def reply(token: str, channel: str, text: str, thread_ts: str = None) -> dict:
    return _slack_post(token, channel, text, thread_ts)


def thinking(token: str, channel: str, thread_ts: str = None) -> tuple:
    """Post a :hourglass: placeholder; return (ts, channel) so we can update it."""
    result = reply(token, channel, ":hourglass_flowing_sand: Working...", thread_ts)
    return result.get("ts"), channel


# ── Case folder helpers ───────────────────────────────────────────────────────

def _case_folder(case_number: str, staging: bool = False) -> Path:
    config = load_config()
    if staging:
        base = Path(config.get("staging_dir", TOOLKIT_DIR / "Staging"))
    else:
        base = Path(config.get("my_cases_dir", TOOLKIT_DIR / "My Cases"))
    return base / case_number


def _other_cases_folder(case_number: str) -> Path:
    config = load_config()
    base = Path(config.get("other_cases_dir", TOOLKIT_DIR / "Other Cases"))
    return base / case_number


def _find_case_folder(case_number: str) -> tuple:
    """Return (Path, label). Checks My Cases, Staging, then Other Cases."""
    my = _case_folder(case_number, staging=False)
    if my.exists():
        return my, "my"
    st = _case_folder(case_number, staging=True)
    if st.exists():
        return st, "staging"
    ot = _other_cases_folder(case_number)
    if ot.exists():
        return ot, "other"
    return my, "my"  # default: My Cases (will be created)


def _folder_link(folder: Path) -> str:
    """Return the folder path as a copyable code block (Slack doesn't render file:// links)."""
    return f"`{folder}`"


def _strip_slack_md(text: str) -> str:
    """Strip Slack markdown formatting (*bold*, _italic_, ~strike~) from a string."""
    # Remove surrounding bold/italic/strikethrough markers around words/numbers
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"_([^_]+)_",   r"\1", text)
    text = re.sub(r"~([^~]+)~",   r"\1", text)
    # Remove inline code backticks
    text = re.sub(r"`([^`]+)`",   r"\1", text)
    return text


# ── Claude CLI integration ────────────────────────────────────────────────────

def run_claude(prompt: str, context_files: list = None, allow_tools: bool = False) -> str:
    """
    Call `claude -p` non-interactively, with CLAUDE.md auto-loaded from TOOLKIT_DIR.
    context_files: list of Path objects to prepend as file context.
    allow_tools: if True, passes --dangerously-skip-permissions for MCP tool use.
    Returns stdout as a string.
    """
    config        = load_config()
    token_limit   = config.get("token_limit", 50000)
    full_prompt   = ""
    total_tokens  = 0

    if context_files:
        for fpath in context_files:
            fpath = Path(fpath)
            if not fpath.exists():
                continue
            if "large_files" in fpath.parts:
                continue
            tok = estimate_tokens(str(fpath))
            # Allow up to 3× the case token limit for Claude context
            if tok > 0 and total_tokens + tok > token_limit * 3:
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
                full_prompt += f"\n\n--- File: {fpath.name} ---\n{content}"
                if tok > 0:
                    total_tokens += tok
            except Exception:
                pass

    full_prompt += f"\n\n---\n\n{prompt}"

    cmd = ["claude", "-p"]
    if allow_tools:
        cmd.append("--dangerously-skip-permissions")

    try:
        result = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(TOOLKIT_DIR),   # CLAUDE.md in this dir is auto-loaded
            shell=(os.name == "nt"),
            timeout=180,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "_Claude CLI timed out (180s). Try a simpler prompt or reduce context._"
    except FileNotFoundError:
        return "_Claude CLI not found. Ensure `claude` is installed and in PATH._"
    except Exception as e:
        return f"_Claude CLI error: {e}_"


def _collect_context_files(case_folder: Path) -> list:
    """Return [summary.md] + RESOURCES/ files (excluding large_files/)."""
    files = []
    summary = case_folder / "summary.md"
    if summary.exists():
        files.append(summary)
    res_dir = case_folder / "RESOURCES"
    if res_dir.is_dir():
        for f in sorted(res_dir.iterdir()):
            if f.is_file() and f.parent.name != "large_files":
                files.append(f)
    return files


# ── Token report formatter ────────────────────────────────────────────────────

def _format_token_report(case_number: str, token_report: dict, threshold: int) -> str:
    if not token_report:
        return f"No files in RESOURCES/ for case {case_number}."

    lines = [f"*Files for case {case_number}:*"]
    total = 0
    for fname, tok in token_report.items():
        if tok == -1:
            lines.append(f"  • `{fname}` — _binary/image_")
        elif tok > threshold:
            lines.append(f"  • `{fname}` — ~{tok:,} tokens  :warning: *LARGE*")
            total += tok
        else:
            lines.append(f"  • `{fname}` — ~{tok:,} tokens")
            total += tok
    lines.append(f"\n_Total text tokens: ~{total:,} (threshold: {threshold:,})_")
    return "\n".join(lines)


def _large_files_in_report(token_report: dict, threshold: int) -> list:
    return [f for f, t in token_report.items() if t > threshold]


# ── Command: case <NUMBER> ────────────────────────────────────────────────────

def cmd_case(token: str, case_number: str, channel: str, thread_ts: str = None):
    ts, _ = thinking(token, channel, thread_ts)
    config = load_config()

    # Fetch case data first (without writing) to check owner
    # We write to a temp placeholder then move if needed — simplest: fetch into My Cases
    # then move if owner differs.
    # Use My Cases as initial destination; reassign after owner check.
    my_folder    = _case_folder(case_number, staging=False)
    other_folder = _other_cases_folder(case_number)

    # Check if a folder already exists somewhere
    existing, existing_label = _find_case_folder(case_number)
    if existing.exists() and existing_label in ("my", "other"):
        case_folder = existing
    else:
        case_folder = my_folder  # temporary; may move after owner check

    case, emails, comments = fetch_and_write_summary(case_number, case_folder)

    if not case:
        _slack_update(token, channel, ts, f":x: Case *{case_number}* not found in Salesforce.")
        return

    # Determine correct folder based on owner (compare SF user IDs, not emails)
    my_sf_id  = get_sf_user_id()
    case_owner_id = case.get("OwnerId", "")
    is_mine   = bool(my_sf_id and case_owner_id == my_sf_id)
    _log(f"Owner check for {case_number}: OwnerId={case_owner_id or 'MISSING'}, my_sf_id={my_sf_id or 'EMPTY'}, is_mine={is_mine}")

    if is_mine:
        correct_folder = my_folder
        folder_label   = "My Cases"
    else:
        correct_folder = other_folder
        folder_label   = "Other Cases"

    # Move/re-create folder in the correct location
    correct_folder.parent.mkdir(parents=True, exist_ok=True)
    if case_folder != correct_folder:
        if case_folder.exists() and not correct_folder.exists():
            shutil.move(str(case_folder), str(correct_folder))
        else:
            fetch_and_write_summary(case_number, correct_folder)
    case_folder = correct_folder

    # Always download attachments to the resolved folder
    state     = load_state()
    new_files = sync_case_attachments(case, case_folder, state)
    save_state(state)
    _log(f"Attachment sync for {case_number}: {len(new_files)} new file(s) downloaded to {case_folder}")

    threshold  = config.get("token_limit", 50000)
    tok_report = get_token_report(case_folder)
    large      = _large_files_in_report(tok_report, threshold)
    sf_id      = case.get("Id", "")
    sf_url     = f"{SF_BASE_URL}/{sf_id}/view"
    owner_name = (case.get("Owner") or {}).get("Name", "Unknown")

    status_line = (
        f"*Case {case_number} ready* ✓  _{folder_label}_\n"
        f"*Customer:* {(case.get('Account') or {}).get('Name', 'N/A')} | "
        f"*Priority:* {case.get('Priority', '?')} | "
        f"*Status:* {case.get('Status', '?')}\n"
        f"*Subject:* {case.get('Subject', 'N/A')}\n"
        f"*Owner:* {owner_name}\n"
        f"<{sf_url}|View in Salesforce> · {_folder_link(case_folder)}\n"
    )
    if new_files:
        status_line += f"\n:paperclip: Downloaded {len(new_files)} attachment(s): " + ", ".join(f"`{f}`" for f in new_files)
    else:
        status_line += "\n:paperclip: No new attachments."

    # Duplicate detection
    my_cases_dir = Path(config.get("my_cases_dir", TOOLKIT_DIR / "My Cases"))
    similar = find_similar_cases(
        case_number,
        case.get("Subject", ""),
        case.get("Description", ""),
        my_cases_dir,
    )
    if similar:
        status_line += "\n\n:mag: *Similar cases:*\n"
        for s in similar:
            status_line += f"  • {s['case_number']}: {s['subject'][:80]}\n"

    _slack_update(token, channel, ts, status_line)

    if large:
        large_list = "\n".join(f"  • `{f}` — ~{tok_report[f]:,} tokens" for f in large)
        reply(
            token, channel,
            f":warning: *{len(large)} large file(s) detected (>{threshold:,} tokens):*\n"
            f"{large_list}\n\n"
            f"Reply `move {case_number} all` to move them to `large_files/`, "
            f"or `move {case_number} <filename>` to move one. "
            f"Or ignore and they stay in RESOURCES/.",
            thread_ts,
        )

    _log(f"Processed case {case_number} → {folder_label}")


# ── Command: download <NUMBER> ────────────────────────────────────────────────

def cmd_download(token: str, case_number: str, channel: str, thread_ts: str = None):
    """Force-download all attachments for a case (skips already-downloaded ones)."""
    folder, _ = _find_case_folder(case_number)

    # Need the SF case record (for Id) — fetch it
    records = run_soql(
        f"SELECT Id, CaseNumber FROM Case WHERE CaseNumber = '{case_number}'"
    )
    if not records:
        reply(token, channel, f":x: Case {case_number} not found in Salesforce.", thread_ts)
        return

    ts, _ = thinking(token, channel, thread_ts)
    case  = records[0]
    folder.mkdir(parents=True, exist_ok=True)

    state     = load_state()
    new_files = sync_case_attachments(case, folder, state)
    save_state(state)

    if new_files:
        names = "\n".join(f"  • `{f}`" for f in new_files)
        _slack_update(token, channel, ts,
            f":paperclip: *{len(new_files)} attachment(s) downloaded for case {case_number}:*\n{names}")
    else:
        _slack_update(token, channel, ts,
            f":white_check_mark: No new attachments for case {case_number} "
            f"(all previously downloaded or none exist).")

    _log(f"download command: {case_number} — {len(new_files)} new file(s)")


# ── Command: staging ──────────────────────────────────────────────────────────

def cmd_staging(token: str, channel: str, thread_ts: str = None):
    ts, _ = thinking(token, channel, thread_ts)

    cases = fetch_staging_report()
    if not cases:
        _slack_update(token, channel, ts, ":x: No cases found in the staging queue.")
        return

    config      = load_config()
    staging_dir = Path(config.get("staging_dir", TOOLKIT_DIR / "Staging"))
    staging_dir.mkdir(parents=True, exist_ok=True)
    state = load_state()

    lines = [f":inbox_tray: *Staging queue — {len(cases)} case(s):*\n"]
    for case in cases:
        num         = case["CaseNumber"]
        case_folder = staging_dir / num
        fetch_and_write_summary(num, case_folder)
        sync_case_attachments(case, case_folder, state)

        priority = case.get("Priority", "?")
        status   = case.get("Status", "?")
        customer = (case.get("Account") or {}).get("Name", "Unknown")
        sf_id    = case.get("Id", "")
        sf_url   = f"{SF_BASE_URL}/{sf_id}/view"
        lines.append(f"• <{sf_url}|{num}> — {customer} | *{priority}* | _{status}_")

    save_state(state)
    lines.append("\n_Type `claim <NUMBER>` to take ownership of a case._")
    _slack_update(token, channel, ts, "\n".join(lines))
    _log(f"Staging command: {len(cases)} cases")


# ── Command: cases ────────────────────────────────────────────────────────────

ACTIVE_STATUSES = (
    "New", "Open", "Consulting", "Consultation",
    "Response Received", "Jira Response Received",
    "Escalated to R&D", "Escalated to Product",
)


def cmd_cases(token: str, channel: str, thread_ts: str = None):
    config  = load_config()
    sf_user = config.get("sf_user", "")
    if not sf_user:
        reply(token, channel, ":x: `sf_user` not configured. Run `config sf_user <email>`.", thread_ts)
        return

    ts, _ = thinking(token, channel, thread_ts)

    my_sf_id = get_sf_user_id()
    if not my_sf_id:
        _slack_update(token, channel, ts,
            ":x: Could not resolve your SF user ID — session may have expired.\n"
            "Type `reauth` to re-authenticate with Salesforce.")
        return

    statuses_str = "', '".join(ACTIVE_STATUSES)
    cases = run_soql(
        f"SELECT Id, OwnerId, CaseNumber, Subject, Status, Priority, Description, "
        f"Account.Name, Contact.Name, Contact.Email, CreatedDate, LastModifiedDate, "
        f"Owner.Name, Origin, Type "
        f"FROM Case WHERE OwnerId = '{my_sf_id}' "
        f"AND Status IN ('{statuses_str}') "
        f"ORDER BY LastModifiedDate DESC"
    )

    if not cases:
        _slack_update(token, channel, ts, ":white_check_mark: No active cases found.")
        return

    my_cases_dir = Path(config.get("my_cases_dir", TOOLKIT_DIR / "My Cases"))
    my_cases_dir.mkdir(parents=True, exist_ok=True)
    created = []
    updated = []

    for case in cases:
        num         = case["CaseNumber"]
        case_folder = my_cases_dir / num
        is_new      = not case_folder.exists()
        fetch_and_write_summary(num, case_folder)
        if is_new:
            created.append(num)
        else:
            updated.append(num)

    lines = [f":briefcase: *My Active Cases — {len(cases)} found*\n"]
    for case in cases:
        num      = case["CaseNumber"]
        sf_id    = case.get("Id", "")
        sf_url   = f"{SF_BASE_URL}/{sf_id}/view"
        priority = case.get("Priority", "?")
        status   = case.get("Status", "?")
        customer = (case.get("Account") or {}).get("Name", "Unknown")
        marker   = " :new:" if num in created else ""
        lines.append(f"• <{sf_url}|{num}>{marker} — {customer} | *{priority}* | _{status}_")

    if created:
        lines.append(f"\n_Folders created: {len(created)} · Updated: {len(updated)}_")
    else:
        lines.append(f"\n_{len(updated)} folder(s) refreshed_")
    lines.append("_Run `case <NUMBER>` to download attachments for a specific case._")

    _slack_update(token, channel, ts, "\n".join(lines))
    _log(f"cases command: {len(cases)} active, {len(created)} new")


# ── Command: claim <NUMBER> ───────────────────────────────────────────────────

def cmd_claim(token: str, case_number: str, channel: str, thread_ts: str = None):
    staging_folder = _case_folder(case_number, staging=True)
    if not staging_folder.exists():
        reply(token, channel, f":x: Case {case_number} not found in Staging folder.", thread_ts)
        return

    ts, _ = thinking(token, channel, thread_ts)
    config = load_config()

    domain_field = config.get("sf_domain_field", "Domain__c")
    subcat_field = config.get("sf_subcategory_field", "Sub_Category__c")

    domains    = get_sf_picklist_values(domain_field)
    subcats    = get_sf_picklist_values(subcat_field)
    context    = _collect_context_files(staging_folder)

    prompt = (
        f"You are helping a Mend support engineer claim a case. "
        f"Based on the case content, suggest the most appropriate Domain and Sub-category.\n\n"
        f"Valid Domain values:\n{json.dumps(domains, indent=2)}\n\n"
        f"Valid Sub-category values:\n{json.dumps(subcats, indent=2)}\n\n"
        f"Reply in this exact format (nothing else):\n"
        f"DOMAIN: <value>\nSUBCATEGORY: <value>\nREASON: <one sentence>"
    )

    ai_response = run_claude(prompt, context_files=context)
    domain_match = re.search(r"DOMAIN:\s*(.+)", ai_response)
    subcat_match = re.search(r"SUBCATEGORY:\s*(.+)", ai_response)
    reason_match = re.search(r"REASON:\s*(.+)", ai_response)

    suggested_domain = domain_match.group(1).strip() if domain_match else "Unknown"
    suggested_subcat = subcat_match.group(1).strip() if subcat_match else "Unknown"
    reason           = reason_match.group(1).strip() if reason_match else ""

    # Store pending confirmation
    PENDING[channel] = {
        "type":      "claim",
        "thread_ts": thread_ts,
        "data": {
            "case_number":      case_number,
            "staging_folder":   str(staging_folder),
            "domain":           suggested_domain,
            "subcategory":      suggested_subcat,
            "domain_field":     domain_field,
            "subcat_field":     subcat_field,
            "valid_domains":    domains,
            "valid_subcats":    subcats,
        },
    }

    domain_list = " | ".join(domains[:10]) + ("..." if len(domains) > 10 else "")
    subcat_list = " | ".join(subcats[:10]) + ("..." if len(subcats) > 10 else "")

    _slack_update(token, channel, ts,
        f":label: *Claim case {case_number}?*\n\n"
        f"*AI Suggestion:*\n"
        f"  Domain: `{suggested_domain}`\n"
        f"  Sub-category: `{suggested_subcat}`\n"
        f"  _{reason}_\n\n"
        f"Reply:\n"
        f"  `approve` — use this suggestion\n"
        f"  `override <domain> <subcategory>` — use your own values\n"
        f"  `cancel` — do nothing\n\n"
        f"_Valid domains:_ {domain_list}\n"
        f"_Valid subcats:_ {subcat_list}"
    )


def _execute_claim(token: str, channel: str, data: dict,
                   domain: str, subcategory: str, thread_ts: str = None):
    case_number    = data["case_number"]
    staging_folder = Path(data["staging_folder"])
    domain_field   = data["domain_field"]
    subcat_field   = data["subcat_field"]

    ts, _ = thinking(token, channel, thread_ts)

    # Get SF user ID and case ID
    sf_user_id = get_sf_user_id()
    records = run_soql(f"SELECT Id FROM Case WHERE CaseNumber = '{case_number}'")
    if not records:
        _slack_update(token, channel, ts, f":x: Could not find case {case_number} in SF.")
        return

    case_id = records[0]["Id"]

    fields = {domain_field: domain, subcat_field: subcategory}
    if sf_user_id:
        fields["OwnerId"] = sf_user_id

    success = update_sf_case(case_id, fields)
    if not success:
        _slack_update(token, channel, ts,
            ":warning: SF update failed — folder moved locally but SF not updated. "
            "Check the bot log for details."
        )

    # Move folder from Staging → My Cases
    config       = load_config()
    my_cases_dir = Path(config.get("my_cases_dir", TOOLKIT_DIR / "My Cases"))
    my_cases_dir.mkdir(parents=True, exist_ok=True)
    dest = my_cases_dir / case_number

    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(staging_folder), str(dest))

    state = load_state()
    state.pop(case_number, None)
    save_state(state)

    sf_url = f"https://whitesourcesoftware.lightning.force.com/lightning/r/Case/{case_id}/view"
    _slack_update(token, channel, ts,
        f":white_check_mark: *Case {case_number} claimed!*\n"
        f"  Domain: `{domain}` | Sub-category: `{subcategory}`\n"
        f"  Moved to `My Cases/` and {'SF updated.' if success else 'SF update failed — check logs.'}\n"
        f"<{sf_url}|View in Salesforce>"
    )
    _log(f"Claimed case {case_number}: domain={domain}, subcat={subcategory}")


# ── Command: summarize <NUMBER> ───────────────────────────────────────────────

def cmd_summarize(token: str, case_number: str, channel: str, thread_ts: str = None):
    folder, _ = _find_case_folder(case_number)
    if not (folder / "summary.md").exists():
        reply(token, channel, f":x: No summary.md found for case {case_number}. Run `case {case_number}` first.", thread_ts)
        return

    ts, _ = thinking(token, channel, thread_ts)
    context = _collect_context_files(folder)
    prompt  = (
        f"Provide a concise technical summary of this support case. Include:\n"
        f"1. What the customer is experiencing (root cause if known)\n"
        f"2. Customer impact and urgency\n"
        f"3. Current status and what has been tried\n"
        f"4. Recommended next steps\n\n"
        f"Keep it under 300 words. Be direct and technical."
    )

    result = run_claude(prompt, context_files=context)

    # Save to ai_analysis file with timestamp header
    analysis_path = folder / f"ai_analysis_{case_number}.md"
    timestamp     = datetime.now().strftime("%Y-%m-%d %H:%M")
    analysis_path.write_text(
        f"# AI Analysis — Case {case_number}\n_Generated: {timestamp}_\n\n{result}\n",
        encoding="utf-8",
    )

    header = f":memo: *Summary — Case {case_number}*  ·  {_folder_link(folder)}\n\n"

    if len(result) > 3000:
        _slack_update(token, channel, ts, header)
        reply(token, channel, result[:3000], thread_ts)
        reply(token, channel, result[3000:], thread_ts)
    else:
        _slack_update(token, channel, ts, header + result)

    _log(f"Summarized case {case_number} → {analysis_path.name}")


# ── Command: tka <NUMBER> ─────────────────────────────────────────────────────

def cmd_tka(token: str, case_number: str, channel: str, thread_ts: str = None):
    folder, _ = _find_case_folder(case_number)
    if not (folder / "summary.md").exists():
        reply(token, channel, f":x: No summary.md for case {case_number}. Run `case {case_number}` first.", thread_ts)
        return

    config          = load_config()
    jira_account_id = config.get("jira_account_id", "")
    sf_user         = config.get("sf_user", "")

    ts, _ = thinking(token, channel, thread_ts)
    context = _collect_context_files(folder)

    prompt = (
        f"You are a Mend support engineer creating a TKA Jira ticket for an internal bug or feature request.\n\n"
        f"1. First search TKA for any existing tickets that match this case to avoid duplicates.\n"
        f"2. If a matching ticket exists, report its key and summary — do NOT create a new one.\n"
        f"3. If no match exists, draft the ticket fields as follows and present them clearly:\n\n"
        f"   PROJECT: TKA\n"
        f"   ISSUE TYPE: Bug (or Feature Request if applicable)\n"
        f"   SUMMARY: <concise, technical title>\n"
        f"   DESCRIPTION: <markdown: steps to reproduce, expected vs actual, logs/errors, affected versions>\n"
        f"   COMPONENTS: <relevant Mend component if identifiable>\n"
        f"   REPORTER: {jira_account_id or sf_user}\n"
        f"   ASSIGNEE: (leave unassigned)\n"
        f"   STATUS: New\n\n"
        f"IMPORTANT: Status must be 'New'. Do not set Backlog, To Do, or any other status.\n"
        f"Present the draft clearly so the engineer can review before creation."
    )

    draft = run_claude(prompt, context_files=context, allow_tools=True)

    if not draft:
        _slack_update(token, channel, ts, ":x: Claude did not return a response. Check the log.")
        return

    # Check if Claude found an existing ticket (heuristic: mentions a TKA key)
    tka_found = re.search(r"\bTKA-\d+\b", draft)

    if tka_found:
        _slack_update(token, channel, ts,
            f":mag: *TKA search result for case {case_number}:*\n\n{draft}"
        )
        _log(f"TKA search for {case_number}: existing ticket found")
        return

    # Store pending confirmation for creation
    PENDING[channel] = {
        "type":      "tka",
        "thread_ts": thread_ts,
        "data":      {"case_number": case_number, "draft": draft, "context": [str(f) for f in context]},
    }

    _slack_update(token, channel, ts,
        f":ticket: *TKA Draft — Case {case_number}*\n\n{draft}\n\n"
        f"---\nReply `confirm` to create this ticket in TKA, or `cancel` to discard."
    )
    _log(f"TKA draft prepared for {case_number}")


def _execute_tka(token: str, channel: str, data: dict, thread_ts: str = None):
    case_number = data["case_number"]
    draft       = data["draft"]
    context     = [Path(f) for f in data.get("context", [])]
    config      = load_config()
    jira_account_id = config.get("jira_account_id", "")
    sf_user     = config.get("sf_user", "")

    ts, _ = thinking(token, channel, thread_ts)

    prompt = (
        f"Create the following TKA Jira ticket now using the Atlassian MCP tool.\n\n"
        f"{draft}\n\n"
        f"Rules:\n"
        f"- Status: New (mandatory)\n"
        f"- Assignee: unassigned\n"
        f"- Reporter: {jira_account_id or sf_user}\n"
        f"- Confirm the ticket key once created."
    )

    result = run_claude(prompt, context_files=context, allow_tools=True)
    _slack_update(token, channel, ts, f":white_check_mark: *TKA ticket created for case {case_number}*\n\n{result}")
    _log(f"TKA ticket created for {case_number}")


# ── Command: logs <NUMBER> ────────────────────────────────────────────────────

def cmd_logs(token: str, case_number: str, channel: str, thread_ts: str = None):
    folder, _ = _find_case_folder(case_number)
    config    = load_config()
    threshold = config.get("token_limit", 50000)
    report    = get_token_report(folder)
    msg       = _format_token_report(case_number, report, threshold)
    reply(token, channel, msg, thread_ts)


# ── Command: move <NUMBER> <filename|all> ─────────────────────────────────────

def cmd_move(token: str, case_number: str, target: str, channel: str, thread_ts: str = None):
    folder, _ = _find_case_folder(case_number)
    res_dir   = folder / "RESOURCES"
    large_dir = res_dir / "large_files"
    config    = load_config()
    threshold = config.get("token_limit", 50000)

    if not res_dir.is_dir():
        reply(token, channel, f":x: No RESOURCES/ folder for case {case_number}.", thread_ts)
        return

    if target.lower() == "all":
        report  = get_token_report(folder)
        to_move = [f for f, t in report.items() if t > threshold]
    else:
        to_move = [target]

    if not to_move:
        reply(token, channel, f":white_check_mark: No files to move for case {case_number}.", thread_ts)
        return

    large_dir.mkdir(exist_ok=True)
    moved, missing = [], []
    for fname in to_move:
        src = res_dir / fname
        if src.exists():
            shutil.move(str(src), str(large_dir / fname))
            moved.append(fname)
        else:
            missing.append(fname)

    msg = ""
    if moved:
        msg += f":file_folder: Moved {len(moved)} file(s) to `large_files/`:\n"
        msg += "\n".join(f"  • `{f}`" for f in moved)
    if missing:
        msg += f"\n:x: Not found: {', '.join(f'`{f}`' for f in missing)}"
    reply(token, channel, msg, thread_ts)


# ── Command: digest ───────────────────────────────────────────────────────────

def cmd_digest(token: str, channel: str, thread_ts: str = None):
    ts, _ = thinking(token, channel, thread_ts)

    my_sf_id = get_sf_user_id()
    if not my_sf_id:
        _slack_update(token, channel, ts,
            ":x: Could not resolve your SF user ID — session may have expired.\n"
            "Type `reauth` to re-authenticate with Salesforce.")
        return

    cases = run_soql(
        f"SELECT CaseNumber, Id, Subject, Status, Priority, Account.Name, CreatedDate "
        f"FROM Case WHERE OwnerId = '{my_sf_id}' AND IsClosed = false "
        f"ORDER BY CreatedDate DESC"
    )
    if not cases:
        _slack_update(token, channel, ts, ":white_check_mark: No open cases found.")
        return

    _slack_update(token, channel, ts, build_digest_message(cases))


# ── Command: watch / unwatch ──────────────────────────────────────────────────

def cmd_watch(token: str, channel: str, thread_ts: str = None):
    config       = load_config()
    my_cases_dir = Path(config.get("my_cases_dir", TOOLKIT_DIR / "My Cases"))
    staging_dir  = Path(config.get("staging_dir", TOOLKIT_DIR / "Staging"))
    ignore_file  = TOOLKIT_DIR / ".watch_ignore"

    ignored = set()
    if ignore_file.exists():
        ignored = {l.strip() for l in ignore_file.read_text().splitlines() if l.strip() and not l.startswith("#")}

    my_cases = sorted(
        [d.name for d in my_cases_dir.iterdir() if d.is_dir()]
    ) if my_cases_dir.is_dir() else []
    staging_cases = sorted(
        [d.name for d in staging_dir.iterdir() if d.is_dir()]
    ) if staging_dir.is_dir() else []

    lines = ["*Currently watching:*\n"]
    if my_cases:
        lines.append("*My Cases:*\n" + "\n".join(
            f"  • {n}" + (" _(ignored)_" if n in ignored else "") for n in my_cases
        ))
    if staging_cases:
        lines.append("\n*Staging:*\n" + "\n".join(f"  • {n}" for n in staging_cases))
    if not my_cases and not staging_cases:
        lines.append("_No case folders found._")

    reply(token, channel, "\n".join(lines), thread_ts)


def cmd_unwatch(token: str, case_number: str, channel: str, thread_ts: str = None):
    ignore_file = TOOLKIT_DIR / ".watch_ignore"
    existing    = set()
    if ignore_file.exists():
        existing = {l.strip() for l in ignore_file.read_text().splitlines() if l.strip()}

    if case_number in existing:
        reply(token, channel, f":white_check_mark: Case {case_number} is already ignored.", thread_ts)
        return

    existing.add(case_number)
    ignore_file.write_text("\n".join(sorted(existing)) + "\n", encoding="utf-8")
    reply(token, channel, f":mute: Case {case_number} added to ignore list. Watcher will skip it.", thread_ts)


# ── Command: config ───────────────────────────────────────────────────────────

_SENSITIVE_KEYS = {"slack_bot_token", "slack_app_token"}


def cmd_config(token: str, args: list, channel: str, thread_ts: str = None):
    config = load_config()

    if not args or args[0].lower() == "show":
        lines = ["*Current config:*\n```"]
        for k, v in config.items():
            if k.startswith("_"):
                continue
            display = "***hidden***" if k in _SENSITIVE_KEYS and v else str(v)
            lines.append(f"  {k}: {display}")
        lines.append("```")
        reply(token, channel, "\n".join(lines), thread_ts)
        return

    key = args[0].lower().replace("-", "_")
    val = " ".join(args[1:]) if len(args) > 1 else ""

    # Special handling
    if key == "system_prompt":
        config["system_prompt"] = val
        save_config(config)
        regenerate_claude_md()
        reply(token, channel, f":white_check_mark: `system_prompt` updated and `CLAUDE.md` regenerated.", thread_ts)
        return

    if key == "sla" and len(args) >= 3:
        priority = args[1].capitalize()
        try:
            hours = int(args[2])
            config.setdefault("sla_thresholds", {})[priority] = hours
            save_config(config)
            reply(token, channel, f":white_check_mark: SLA for {priority} set to {hours}h.", thread_ts)
        except ValueError:
            reply(token, channel, ":x: Usage: `config sla <Priority> <hours>`", thread_ts)
        return


    # Generic key=value
    if not val:
        reply(token, channel, f":x: Usage: `config <key> <value>`", thread_ts)
        return

    # Type coercion
    if val.lower() in ("true", "false"):
        val = val.lower() == "true"
    else:
        try:
            val = int(val)
        except ValueError:
            pass

    config[key] = val
    save_config(config)
    reply(token, channel, f":white_check_mark: `{key}` updated.", thread_ts)


# ── Command: setup ────────────────────────────────────────────────────────────

def cmd_setup(token: str, channel: str, thread_ts: str = None):
    import shutil as _shutil
    lines = ["*Setup status:*\n"]

    checks = [
        ("sf CLI",      _shutil.which("sf")),
        ("claude CLI",  _shutil.which("claude")),
        ("python",      _shutil.which("python") or _shutil.which("python3")),
    ]
    for name, path in checks:
        icon = ":white_check_mark:" if path else ":x:"
        lines.append(f"{icon} `{name}`" + (f" — {path}" if path else " — *NOT FOUND*"))

    try:
        config = load_config()
        for field in ["slack_bot_token", "slack_app_token", "sf_user", "my_cases_dir", "staging_dir"]:
            val = config.get(field, "")
            icon = ":white_check_mark:" if val else ":warning:"
            lines.append(f"{icon} `{field}`" + (" — set" if val else " — *not set*"))
    except FileNotFoundError:
        lines.append(":x: `config.json` not found — run `python setup.py`")

    lines.append("\n_Run `python setup.py` to re-run the full onboarding wizard._")
    reply(token, channel, "\n".join(lines), thread_ts)


# ── Command: reauth ───────────────────────────────────────────────────────────

def cmd_reauth(token: str, channel: str, thread_ts: str = None):
    config  = load_config()
    sf_user = config.get("sf_user", "")

    # Clear cached SF user ID so it gets re-resolved after login
    if "_sf_user_id_cache" in config:
        del config["_sf_user_id_cache"]
        save_config(config)

    reply(token, channel,
        ":key: *Salesforce re-authentication*\n"
        "Opening a browser window on the bot machine now...\n"
        "_Complete the login in your browser, then try your command again._",
        thread_ts,
    )

    try:
        cmd = ["sf", "org", "login", "web", "--set-default"]
        if sf_user:
            cmd += ["--alias", sf_user]
        # Run in background — browser opens on the bot machine, user completes it there
        subprocess.Popen(
            cmd,
            shell=(os.name == "nt"),
            cwd=str(TOOLKIT_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _log("SF reauth initiated via browser")
    except Exception as e:
        reply(token, channel,
            f":x: Could not launch SF login: `{e}`\n"
            f"Run `sf org login web --set-default` manually in a terminal.",
            thread_ts,
        )


# ── Pending confirmation handler ──────────────────────────────────────────────

def handle_pending(token: str, text: str, channel: str, thread_ts: str = None) -> bool:
    """
    If a pending confirmation exists for this channel, try to match the reply.
    Returns True if the message was consumed as a confirmation.
    """
    if channel not in PENDING:
        return False

    pending = PENDING[channel]
    ptype   = pending["type"]
    data    = pending["data"]
    orig_ts = pending.get("thread_ts")

    lower = text.strip().lower()

    if ptype == "claim":
        if lower == "cancel":
            del PENDING[channel]
            reply(token, channel, ":no_entry_sign: Claim cancelled.", thread_ts)
            return True
        if lower == "approve":
            del PENDING[channel]
            _execute_claim(token, channel, data, data["domain"], data["subcategory"], orig_ts)
            return True
        # override <domain> <subcategory>
        m = re.match(r"^override\s+(.+?)\s+(.+)$", text.strip(), re.IGNORECASE)
        if m:
            del PENDING[channel]
            _execute_claim(token, channel, data, m.group(1).strip(), m.group(2).strip(), orig_ts)
            return True
        # Not a recognised reply — leave pending, send hint
        reply(token, channel,
            f"Reply `approve`, `override <domain> <subcategory>`, or `cancel`.", thread_ts)
        return True

    if ptype == "tka":
        if lower == "cancel":
            del PENDING[channel]
            reply(token, channel, ":no_entry_sign: TKA creation cancelled.", thread_ts)
            return True
        if lower == "confirm":
            del PENDING[channel]
            _execute_tka(token, channel, data, orig_ts)
            return True
        reply(token, channel, "Reply `confirm` to create the ticket, or `cancel`.", thread_ts)
        return True

    return False


# ── Main command dispatcher ───────────────────────────────────────────────────

HELP_TEXT = """*Mend Support Toolkit — Bot Commands*

*Case management*
  `case <NUMBER>`               — fetch / refresh a case from SF
  `cases`                       — sync all my active SF cases at once
  `staging`                     — list + process the SF staging queue
  `claim <NUMBER>`              — take ownership (AI-suggested Domain/Sub-category)
  `summarize <NUMBER>`          — AI summary via Claude
  `tka <NUMBER>`                — AI-drafted TKA ticket via Claude

*Files & tokens*
  `download <NUMBER>`           — download attachments for a case (on demand)
  `logs <NUMBER>`               — show RESOURCES/ files with token counts
  `move <NUMBER> <file|all>`    — move large file(s) to large_files/

*Watching*
  `watch`                       — list all watched cases
  `unwatch <NUMBER>`            — stop watching a case

*Config*
  `config show`                 — show all settings
  `config <key> <value>`        — update a setting
  `config system-prompt <text>` — update AI system prompt + regenerate CLAUDE.md
  `config sla <Priority> <hrs>` — set SLA threshold (e.g. `config sla Critical 4`)

*Other*
  `digest`                      — on-demand daily digest
  `reauth`                      — re-authenticate with Salesforce (opens browser)
  `setup`                       — check prerequisites and config status
  `help`                        — show this message"""


def dispatch(token: str, text: str, channel: str, thread_ts: str = None, sender_id: str = ""):
    # Reject anyone who isn't the configured owner of this bot instance
    try:
        config = load_config()
        owner_id = config.get("slack_user_id", "")
        if owner_id and sender_id and sender_id != owner_id:
            reply(token, channel,
                  "Sorry, this is a personal bot — it's already claimed by someone else.\n"
                  "Clone the repo and run `python setup.py` to set up your own instance:\n"
                  "https://github.com/your-org/mend-support-toolkit",
                  thread_ts)
            return
    except Exception:
        pass

    # Check pending confirmations first
    if handle_pending(token, text, channel, thread_ts):
        return

    text_stripped = text.strip()
    lower         = text_stripped.lower()

    # case <NUMBER>
    m = re.match(r"^case\s+(\d+)$", lower)
    if m:
        cmd_case(token, m.group(1).zfill(8), channel, thread_ts)
        return

    # cases (bulk sync)
    if lower == "cases":
        cmd_cases(token, channel, thread_ts)
        return

    # staging
    if lower == "staging":
        cmd_staging(token, channel, thread_ts)
        return

    # claim <NUMBER>
    m = re.match(r"^claim\s+(\d+)$", lower)
    if m:
        cmd_claim(token, m.group(1).zfill(8), channel, thread_ts)
        return

    # summarize <NUMBER>
    m = re.match(r"^summarize\s+(\d+)$", lower)
    if m:
        cmd_summarize(token, m.group(1).zfill(8), channel, thread_ts)
        return

    # tka <NUMBER>
    m = re.match(r"^tka\s+(\d+)$", lower)
    if m:
        cmd_tka(token, m.group(1).zfill(8), channel, thread_ts)
        return

    # download <NUMBER>
    m = re.match(r"^download\s+(\d+)$", lower)
    if m:
        cmd_download(token, m.group(1).zfill(8), channel, thread_ts)
        return

    # logs <NUMBER>
    m = re.match(r"^logs\s+(\d+)$", lower)
    if m:
        cmd_logs(token, m.group(1).zfill(8), channel, thread_ts)
        return

    # move <NUMBER> <filename|all>
    m = re.match(r"^move\s+(\d+)\s+(.+)$", text_stripped, re.IGNORECASE)
    if m:
        cmd_move(token, m.group(1).zfill(8), m.group(2).strip(), channel, thread_ts)
        return


    # digest
    if lower == "digest":
        cmd_digest(token, channel, thread_ts)
        return

    # watch
    if lower == "watch":
        cmd_watch(token, channel, thread_ts)
        return

    # unwatch <NUMBER>
    m = re.match(r"^unwatch\s+(\d+)$", lower)
    if m:
        cmd_unwatch(token, m.group(1).zfill(8), channel, thread_ts)
        return

    # config [show | key value]
    if lower.startswith("config"):
        args = text_stripped.split()[1:]
        cmd_config(token, args, channel, thread_ts)
        return

    # reauth
    if lower == "reauth":
        cmd_reauth(token, channel, thread_ts)
        return

    # setup
    if lower == "setup":
        cmd_setup(token, channel, thread_ts)
        return

    # help / ?
    if lower in ("help", "?"):
        reply(token, channel, HELP_TEXT, thread_ts)
        return

    reply(token, channel, f":question: Unknown command: `{text_stripped}`\nType `help` to see available commands.", thread_ts)


# ── Socket Mode event handler ─────────────────────────────────────────────────

def handle_event(sm_client: SocketModeClient, req):
    sm_client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    event = req.payload.get("event", {})
    if event.get("type") != "message":
        return
    if event.get("bot_id") or event.get("subtype"):
        return
    if event.get("channel_type") not in ("im", "mpim"):
        return

    text      = _strip_slack_md((event.get("text") or "").strip())
    channel   = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts")
    sender_id = event.get("user", "")

    if not text or not channel:
        return

    try:
        config = load_config()
        token  = config.get("slack_bot_token", "")
    except Exception:
        return

    _log(f"Command from {channel}: {text}")

    try:
        dispatch(token, text, channel, thread_ts, sender_id)
    except Exception as e:
        _log(f"Unhandled error in dispatch: {e}")
        try:
            reply(token, channel, f":rotating_light: Unexpected error: `{e}`\nCheck the bot log.", thread_ts)
        except Exception:
            pass


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    try:
        config = load_config()
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        print("Run setup.py first.")
        sys.exit(1)

    bot_token = config.get("slack_bot_token", "")
    app_token = config.get("slack_app_token", "")

    if not bot_token or not app_token:
        print("ERROR: slack_bot_token and slack_app_token must be set in config.json.")
        print("Run setup.py or set them with: config slack_bot_token xoxb-...")
        sys.exit(1)

    if not (TOOLKIT_DIR / "CLAUDE.md").exists():
        regenerate_claude_md()

    web_client    = WebClient(token=bot_token)
    socket_client = SocketModeClient(app_token=app_token, web_client=web_client)
    socket_client.socket_mode_request_listeners.append(handle_event)
    socket_client.connect()

    _log("Case Bot connected — listening for commands (Ctrl+C to stop)")

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        _log("Case Bot stopped")


if __name__ == "__main__":
    main()
