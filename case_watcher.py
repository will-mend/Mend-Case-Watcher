#!/usr/bin/env python3
"""
case_watcher.py — Mend Support Toolkit background poller.

Runs every 5 minutes via Windows Task Scheduler.
Internally rate-limits My Cases vs Staging polls using timestamps in state.

What it does each run:
  - Polls Staging queue (SF Report) if poll_staging_minutes have elapsed
  - Polls My Cases (open SF cases owned by sf_user) if poll_my_cases_minutes elapsed
  - Creates folders / refreshes summary.md for new or changed cases
  - Downloads new attachments; sends Slack alert for large files
  - Detects status changes and new comments; sends Slack DM
  - Sends SLA nudges for overdue cases
  - Sends daily digest at configured digest_time
  - Archives closed cases to Google Drive (if configured)
"""

import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Allow running from any working directory
sys.path.insert(0, str(Path(__file__).parent))

from utils import (
    TOOLKIT_DIR,
    build_digest_message,
    fetch_and_write_summary,
    fetch_staging_report,
    find_similar_cases,
    get_token_report,
    load_config,
    load_state,
    log,
    run_soql,
    save_state,
    send_slack_dm,
    sync_case_attachments,
    archive_to_gdrive,
    estimate_tokens,
)

SOURCE = "WATCHER"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log(msg: str):
    log(msg, SOURCE)


def _minutes_since(iso_ts: str) -> float:
    """Return minutes elapsed since an ISO timestamp string, or infinity if missing."""
    if not iso_ts:
        return float("inf")
    try:
        dt = datetime.fromisoformat(iso_ts)
        return (datetime.now() - dt).total_seconds() / 60
    except Exception:
        return float("inf")


def _case_dir(case_number: str, staging: bool = False) -> Path:
    config = load_config()
    if staging:
        base = Path(config.get("staging_dir", TOOLKIT_DIR / "Staging"))
    else:
        base = Path(config.get("my_cases_dir", TOOLKIT_DIR / "My Cases"))
    return base / case_number


# ── Token alert (post-download) ───────────────────────────────────────────────

def _alert_large_files(case_number: str, new_files: list, case_folder: Path):
    """Send a Slack alert if any newly downloaded file exceeds the token limit."""
    config    = load_config()
    threshold = config.get("token_limit", 50000)
    res_dir   = case_folder / "RESOURCES"

    large = []
    for fname in new_files:
        fpath  = res_dir / fname
        tokens = estimate_tokens(str(fpath))
        if tokens > threshold:
            large.append((fname, tokens))

    if not large:
        return

    lines = [f":warning: *Large file(s) on case {case_number}* (>{threshold:,} tokens)"]
    for fname, tokens in large:
        lines.append(f"  • `{fname}` — ~{tokens:,} tokens")
    lines.append(f"DM the bot `logs {case_number}` to review, or `move {case_number} <filename>` to move them.")
    send_slack_dm("\n".join(lines))


# ── SLA nudges ────────────────────────────────────────────────────────────────

NUDGE_STATUSES = {"Response Received", "New"}


def check_sla(cases: list, state: dict):
    config      = load_config()
    sla_hours   = config.get("sla_thresholds", {"Critical": 4, "High": 8, "Medium": 24, "Low": 72})
    now         = datetime.now()

    for case in cases:
        num    = case.get("CaseNumber", "")
        status = case.get("Status", "")
        if status not in NUDGE_STATUSES:
            continue

        priority  = case.get("Priority", "Medium")
        threshold = sla_hours.get(priority, 24)
        last_mod  = case.get("LastModifiedDate", "")

        try:
            last_mod_dt  = datetime.fromisoformat(last_mod.replace("+0000", "").replace("Z", ""))
            hours_since  = (now - last_mod_dt).total_seconds() / 3600
        except Exception:
            continue

        if hours_since < threshold:
            continue

        # Avoid re-nudging within the same threshold window
        last_nudge = state.get(num, {}).get("sla_nudge_sent", "")
        if last_nudge:
            try:
                nudge_dt = datetime.fromisoformat(last_nudge)
                if (now - nudge_dt).total_seconds() / 3600 < threshold:
                    continue
            except Exception:
                pass

        account = (case.get("Account") or {}).get("Name", "Unknown")
        sf_id   = case.get("Id", "")
        sf_url  = f"https://whitesourcesoftware.lightning.force.com/lightning/r/Case/{sf_id}/view"

        send_slack_dm(
            f":alarm_clock: *SLA Nudge — Case {num}* ({priority})\n"
            f"*Customer:* {account} | *Status:* {status}\n"
            f"No update for {hours_since:.0f}h (threshold: {threshold}h)\n"
            f"<{sf_url}|Open in Salesforce>"
        )
        state.setdefault(num, {})["sla_nudge_sent"] = now.isoformat()
        _log(f"SLA nudge sent for {num} ({priority}, {hours_since:.0f}h overdue)")


# ── Update detection ──────────────────────────────────────────────────────────

def check_for_updates(cases: list, state: dict, staging: bool = False):
    """Detect status changes and new comments; send Slack DMs."""
    from utils import run_soql, SF_BASE_URL

    for case in cases:
        num            = case.get("CaseNumber", "")
        case_id        = case.get("Id", "")
        current_status = case.get("Status", "")
        last_mod       = case.get("LastModifiedDate", "")
        account        = (case.get("Account") or {}).get("Name", "Unknown")
        sf_url         = f"{SF_BASE_URL}/{case_id}/view"

        saved      = state.get(num, {})
        prev_mod   = saved.get("last_modified", "")
        prev_status = saved.get("status", "")

        if not prev_mod:
            # First time seen — save baseline, no notification
            state.setdefault(num, {}).update({"last_modified": last_mod, "status": current_status})
            continue

        if last_mod == prev_mod:
            continue

        label  = "Staging" if staging else "My Cases"
        lines  = [f":bell: *Case {num} updated* [{label}] — {account}", f"<{sf_url}|View in Salesforce>"]

        if current_status != prev_status:
            lines.insert(1, f"*Status:* {prev_status} → *{current_status}*")

        # Fetch new comments since last known modification
        new_comments = run_soql(
            f"SELECT CommentBody, CreatedBy.Name, CreatedDate "
            f"FROM CaseComment WHERE ParentId = '{case_id}' "
            f"AND CreatedDate > {prev_mod} ORDER BY CreatedDate ASC"
        )
        for cm in new_comments:
            author  = (cm.get("CreatedBy") or {}).get("Name", "Unknown")
            preview = (cm.get("CommentBody") or "")[:120]
            if len(cm.get("CommentBody") or "") > 120:
                preview += "..."
            lines.insert(-1, f":speech_balloon: *{author}:* {preview}")

        send_slack_dm("\n".join(lines))
        _log(f"Update on {num}: {prev_status} → {current_status}")

        state.setdefault(num, {}).update({"last_modified": last_mod, "status": current_status})


# ── Closed case cleanup ───────────────────────────────────────────────────────

def cleanup_closed_cases(open_case_numbers: set, state: dict):
    """Archive case folders that are no longer in the open SF list."""
    config      = load_config()
    my_cases    = Path(config.get("my_cases_dir", TOOLKIT_DIR / "My Cases"))
    do_archive  = config.get("archive_closed_cases", True)

    if not my_cases.is_dir():
        return

    closed = []
    for entry in my_cases.iterdir():
        if not entry.is_dir() or entry.name in open_case_numbers:
            continue

        _log(f"Case {entry.name} no longer in open SF list — archiving")

        if do_archive:
            success = archive_to_gdrive(entry, entry.name)
            if success:
                shutil.rmtree(entry, ignore_errors=True)
                state.pop(entry.name, None)
                closed.append(entry.name)
                _log(f"Archived and removed local folder: {entry.name}")
            else:
                _log(f"Archive failed for {entry.name} — local folder kept")
        else:
            shutil.rmtree(entry, ignore_errors=True)
            state.pop(entry.name, None)
            closed.append(entry.name)

    if closed:
        items = "\n".join(f"  • {n}" for n in closed)
        action = "archived to Google Drive and removed" if do_archive else "removed"
        send_slack_dm(
            f":file_folder: *{len(closed)} closed case folder(s) {action}:*\n{items}"
        )


# ── Digest ────────────────────────────────────────────────────────────────────

def should_send_digest(state: dict) -> bool:
    config      = load_config()
    digest_time = config.get("digest_time", "09:00")
    now         = datetime.now()

    try:
        h, m = map(int, digest_time.split(":"))
    except Exception:
        return False

    # Window: digest_time to digest_time + 14 min (watcher runs every 5 min)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if not (target <= now < target + timedelta(minutes=14)):
        return False

    return state.get("last_digest_date", "") != now.strftime("%Y-%m-%d")


def send_digest(cases: list, state: dict):
    msg = build_digest_message(cases)
    send_slack_dm(msg)
    state["last_digest_date"] = datetime.now().strftime("%Y-%m-%d")
    _log("Daily digest sent")


# ── Poll: My Cases ────────────────────────────────────────────────────────────

def poll_my_cases(state: dict):
    config  = load_config()
    sf_user = config.get("sf_user", "")
    if not sf_user:
        _log("sf_user not configured — skipping My Cases poll")
        return

    _log("Polling My Cases...")
    cases = run_soql(
        f"SELECT CaseNumber, Id, Subject, Status, Priority, Description, "
        f"Account.Name, Contact.Name, Contact.Email, CreatedDate, LastModifiedDate "
        f"FROM Case WHERE Owner.Email = '{sf_user}' AND IsClosed = false "
        f"ORDER BY CreatedDate DESC"
    )

    if not cases:
        _log("No open cases returned from Salesforce")
        return

    _log(f"{len(cases)} open case(s) found")
    my_cases_dir = Path(config.get("my_cases_dir", TOOLKIT_DIR / "My Cases"))
    my_cases_dir.mkdir(parents=True, exist_ok=True)

    for case in cases:
        num        = case["CaseNumber"]
        case_folder = my_cases_dir / num

        is_new = not case_folder.exists()
        fetch_and_write_summary(num, case_folder)

        if is_new:
            _log(f"New case folder created: {num}")
            # Check for similar existing cases
            subject     = case.get("Subject", "")
            description = case.get("Description", "")
            similar = find_similar_cases(num, subject, description, my_cases_dir)
            sf_id   = case.get("Id", "")
            sf_url  = f"https://whitesourcesoftware.lightning.force.com/lightning/r/Case/{sf_id}/view"
            msg = (
                f":file_folder: *New case: {num}*\n"
                f"*Customer:* {(case.get('Account') or {}).get('Name', 'Unknown')}\n"
                f"*Priority:* {case.get('Priority', '?')} | *Status:* {case.get('Status', '?')}\n"
                f"*Subject:* {subject}\n"
                f"<{sf_url}|View in Salesforce>"
            )
            if similar:
                msg += "\n\n:mag: *Similar cases found:*\n"
                for s in similar:
                    msg += f"  • {s['case_number']}: {s['subject'][:80]}\n"
            send_slack_dm(msg)
            state.setdefault(num, {})["first_seen"] = datetime.now().strftime("%Y-%m-%d")

        # Sync attachments
        new_files = sync_case_attachments(case, case_folder, state)
        if new_files:
            _alert_large_files(num, new_files, case_folder)

    # Update detection, SLA nudges, digest, cleanup
    check_for_updates(cases, state, staging=False)
    check_sla(cases, state)

    if should_send_digest(state):
        send_digest(cases, state)

    open_nums = {c["CaseNumber"] for c in cases}
    cleanup_closed_cases(open_nums, state)

    state["last_my_cases_poll"] = datetime.now().isoformat()
    _log("My Cases poll complete")


# ── Poll: Staging ─────────────────────────────────────────────────────────────

def poll_staging(state: dict):
    config      = load_config()
    staging_dir = Path(config.get("staging_dir", TOOLKIT_DIR / "Staging"))
    staging_dir.mkdir(parents=True, exist_ok=True)

    _log("Polling Staging queue...")
    cases = fetch_staging_report()

    if not cases:
        _log("No cases in staging queue")
        state["last_staging_poll"] = datetime.now().isoformat()
        return

    _log(f"{len(cases)} case(s) in staging queue")

    for case in cases:
        num         = case["CaseNumber"]
        case_folder = staging_dir / num

        is_new = not case_folder.exists()
        fetch_and_write_summary(num, case_folder)

        if is_new:
            _log(f"New staging case folder: {num}")
            sf_id  = case.get("Id", "")
            sf_url = f"https://whitesourcesoftware.lightning.force.com/lightning/r/Case/{sf_id}/view"
            send_slack_dm(
                f":inbox_tray: *New case in Staging queue: {num}*\n"
                f"*Customer:* {(case.get('Account') or {}).get('Name', 'Unknown')}\n"
                f"*Priority:* {case.get('Priority', '?')} | "
                f"*Subject:* {case.get('Subject', 'N/A')}\n"
                f"<{sf_url}|View in Salesforce>\n"
                f"_Type `claim {num}` to take ownership._"
            )
            state.setdefault(num, {})["in_staging"] = True

        new_files = sync_case_attachments(case, case_folder, state)
        if new_files:
            _alert_large_files(num, new_files, case_folder)

    check_for_updates(cases, state, staging=True)

    state["last_staging_poll"] = datetime.now().isoformat()
    _log("Staging poll complete")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    _log("Watcher run started")

    try:
        config = load_config()
    except FileNotFoundError as e:
        _log(f"Config error: {e}")
        sys.exit(1)

    state = load_state()
    now   = datetime.now()

    my_cases_interval = config.get("poll_my_cases_minutes", 15)
    staging_interval  = config.get("poll_staging_minutes", 5)

    # Staging poll
    if _minutes_since(state.get("last_staging_poll", "")) >= staging_interval:
        try:
            poll_staging(state)
        except Exception as e:
            _log(f"Staging poll error: {e}")

    # My Cases poll
    if _minutes_since(state.get("last_my_cases_poll", "")) >= my_cases_interval:
        try:
            poll_my_cases(state)
        except Exception as e:
            _log(f"My Cases poll error: {e}")

    save_state(state)
    _log("Watcher run complete\n")


if __name__ == "__main__":
    main()
