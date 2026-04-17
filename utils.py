#!/usr/bin/env python3
"""utils.py — Shared utilities for the Mend Support Toolkit."""

import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
TOOLKIT_DIR = Path(__file__).parent
CONFIG_PATH = TOOLKIT_DIR / "config.json"
STATE_PATH  = TOOLKIT_DIR / ".watcher_state.json"
LOG_PATH    = TOOLKIT_DIR / "toolkit.log"

SF_BASE_URL = "https://whitesourcesoftware.lightning.force.com/lightning/r/Case"

TEXT_EXTENSIONS = {
    ".txt", ".log", ".json", ".xml", ".yaml", ".yml", ".csv", ".md",
    ".html", ".htm", ".js", ".py", ".java", ".cs", ".cpp", ".c", ".h",
    ".ts", ".sh", ".bat", ".properties", ".conf", ".cfg", ".ini", ".toml",
    ".gradle", ".pom", ".lock", ".tf",
}


# ── Config / State ────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.json not found. Run setup.py first."
        )
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str, source: str = "TOOLKIT"):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{source}] {msg}"
    print(line.encode("ascii", errors="replace").decode("ascii"), flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Salesforce ────────────────────────────────────────────────────────────────

def _sf_cmd_base() -> list:
    config = load_config()
    sf_user = config.get("sf_user", "")
    base = ["sf"]
    if sf_user:
        base += ["--target-org", sf_user]
    return base


def run_soql(query: str) -> list:
    config = load_config()
    sf_user = config.get("sf_user", "")
    cmd = ["sf", "data", "query", "--query", query, "--json"]
    if sf_user:
        cmd += ["--target-org", sf_user]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            cwd=str(TOOLKIT_DIR),
            shell=(os.name == "nt"),
            timeout=60,
        )
        data = json.loads(result.stdout)
        return data.get("result", {}).get("records", [])
    except Exception as e:
        log(f"SOQL error: {e}")
        return []


def get_sf_session() -> tuple:
    """Returns (access_token, instance_url) from the active SF CLI session."""
    config = load_config()
    sf_user = config.get("sf_user", "")
    cmd = ["sf", "org", "display", "--json"]
    if sf_user:
        cmd += ["--target-org", sf_user]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            cwd=str(TOOLKIT_DIR),
            shell=(os.name == "nt"),
            timeout=30,
        )
        data = json.loads(result.stdout)
        info = data.get("result", {})
        return info.get("accessToken", ""), info.get("instanceUrl", "")
    except Exception as e:
        log(f"SF session error: {e}")
        return "", ""


def get_sf_user_id() -> str:
    """
    Returns the SF user ID (OwnerId format) of the currently authenticated user.
    Tries three methods in order; caches the result in config on success.
    """
    config = load_config()

    # Return cached value if available
    cached = config.get("_sf_user_id_cache", "")
    if cached:
        return cached

    sf_user = config.get("sf_user", "")
    user_id = ""

    # Attempt 1: REST API /chatter/users/me — uses same session as attachments
    access_token, instance_url = get_sf_session()
    if access_token and instance_url:
        try:
            req = urllib.request.Request(
                f"{instance_url}/services/data/v62.0/chatter/users/me",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            user_id = data.get("id", "")
            if user_id:
                log(f"SF user ID resolved via REST /chatter/users/me: {user_id}")
        except Exception as e:
            log(f"SF user ID REST lookup failed: {e}")

    # Attempt 2: sf org display --json
    if not user_id:
        try:
            cmd = ["sf", "org", "display", "--json"]
            if sf_user:
                cmd += ["--target-org", sf_user]
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                cwd=str(TOOLKIT_DIR),
                shell=(os.name == "nt"),
                timeout=30,
            )
            data = json.loads(result.stdout)
            user_id = data.get("result", {}).get("userId", "")
            if user_id:
                log(f"SF user ID resolved via sf org display: {user_id}")
        except Exception as e:
            log(f"SF org display failed: {e}")

    # Attempt 3: SOQL SELECT Id FROM User
    if not user_id and sf_user:
        try:
            escaped = sf_user.replace("'", "\\'")
            records = run_soql(
                f"SELECT Id FROM User WHERE IsActive = true "
                f"AND Username = '{escaped}' LIMIT 1"
            )
            if records:
                user_id = records[0].get("Id", "")
                if user_id:
                    log(f"SF user ID resolved via SOQL Username: {user_id}")
        except Exception as e:
            log(f"SF user SOQL failed: {e}")

    if user_id:
        config["_sf_user_id_cache"] = user_id
        save_config(config)
    else:
        log(f"SF user ID resolution FAILED — all 3 methods returned empty")

    return user_id


def fetch_staging_report() -> list:
    """Fetch cases from the SF staging queue report via the Analytics REST API."""
    config = load_config()
    report_id = config.get("sf_report_id", "")
    if not report_id:
        log("sf_report_id not configured")
        return []

    access_token, instance_url = get_sf_session()
    if not access_token:
        log("No SF session — cannot fetch staging report")
        return []

    url = f"{instance_url}/services/data/v62.0/analytics/reports/{report_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        metadata       = data.get("reportMetadata", {})
        detail_columns = metadata.get("detailColumns", [])
        fact_map       = data.get("factMap", {})

        # Locate the CaseNumber column index
        case_num_idx = 0
        for i, col in enumerate(detail_columns):
            if "CASE_NUMBER" in col.upper() or col in ("CaseNumber", "CASENUMBER"):
                case_num_idx = i
                break

        # Collect all rows (tabular reports use "T!T", summary use "0!T" etc.)
        rows = []
        for section in fact_map.values():
            if isinstance(section, dict) and "rows" in section:
                rows.extend(section["rows"])

        case_numbers = []
        for row in rows:
            cells = row.get("dataCells", [])
            if case_num_idx < len(cells):
                label = str(cells[case_num_idx].get("label", ""))
                if re.match(r"^\d{8}$", label):
                    case_numbers.append(label)

        if not case_numbers:
            log("No case numbers found in staging report")
            return []

        nums_str = "', '".join(case_numbers)
        query = (
            f"SELECT CaseNumber, Id, Subject, Status, Priority, Description, "
            f"Account.Name, Contact.Name, Contact.Email, CreatedDate, LastModifiedDate "
            f"FROM Case WHERE CaseNumber IN ('{nums_str}') ORDER BY CreatedDate DESC"
        )
        return run_soql(query)

    except Exception as e:
        log(f"Staging report fetch error: {e}")
        return []


def get_sf_picklist_values(field_name: str) -> list:
    """Return active picklist values for a Case field, using cache where possible."""
    config = load_config()
    cached = config.get("sf_picklist_cache", {}).get(field_name)
    if cached:
        return cached

    access_token, instance_url = get_sf_session()
    if not access_token:
        return []

    url = f"{instance_url}/services/data/v62.0/sobjects/Case/describe"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            describe = json.loads(resp.read())

        for field in describe.get("fields", []):
            if field.get("name") == field_name:
                values = [
                    pv["value"]
                    for pv in field.get("picklistValues", [])
                    if pv.get("active")
                ]
                config.setdefault("sf_picklist_cache", {})[field_name] = values
                save_config(config)
                return values
    except Exception as e:
        log(f"Could not fetch picklist for {field_name}: {e}")

    return []


def update_sf_case(case_id: str, fields: dict) -> bool:
    """PATCH a Salesforce Case record with the provided fields dict."""
    access_token, instance_url = get_sf_session()
    if not access_token:
        return False

    url     = f"{instance_url}/services/data/v62.0/sobjects/Case/{case_id}"
    payload = json.dumps(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="PATCH",
        headers={
            "Authorization":  f"Bearer {access_token}",
            "Content-Type":   "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass  # 204 No Content on success
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log(f"SF update failed ({e.code}): {body}")
        return False
    except Exception as e:
        log(f"SF update error: {e}")
        return False


# ── Attachments ───────────────────────────────────────────────────────────────

def get_case_attachments(case_id: str) -> list:
    links = run_soql(
        f"SELECT ContentDocumentId FROM ContentDocumentLink "
        f"WHERE LinkedEntityId = '{case_id}'"
    )
    if not links:
        return []
    ids_str = "', '".join(r["ContentDocumentId"] for r in links)
    return run_soql(
        f"SELECT Id, Title, FileExtension, VersionData, ContentDocumentId "
        f"FROM ContentVersion "
        f"WHERE ContentDocumentId IN ('{ids_str}') AND IsLatest = true"
    )


def download_attachment(
    version_data_path: str, access_token: str, instance_url: str, dest_path: str
) -> bool:
    url = f"{instance_url}{version_data_path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            Path(dest_path).write_bytes(resp.read())
        return True
    except Exception as e:
        log(f"Attachment download failed ({dest_path}): {e}")
        return False


def safe_filename(title: str, ext: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", title).strip()
    if ext and not name.lower().endswith(f".{ext.lower()}"):
        name = f"{name}.{ext}"
    return name


def sync_case_attachments(case: dict, case_folder: Path, state: dict) -> list:
    """Download any new attachments for a case. Returns list of new filenames."""
    case_id  = case["Id"]
    case_num = case["CaseNumber"]
    res_dir  = case_folder / "RESOURCES"
    res_dir.mkdir(parents=True, exist_ok=True)

    access_token, instance_url = get_sf_session()
    if not access_token:
        log(f"No SF session — skipping attachments for {case_num}")
        return []

    attachments    = get_case_attachments(case_id)
    downloaded_ids = state.get(case_num, {}).get("downloaded_files", [])
    new_files      = []

    for att in attachments:
        doc_id  = att.get("ContentDocumentId", "")
        title   = att.get("Title", "attachment")
        ext     = att.get("FileExtension", "")
        vd_path = att.get("VersionData", "")

        if not vd_path or doc_id in downloaded_ids:
            continue

        filename  = safe_filename(title, ext)
        dest_path = res_dir / filename

        if dest_path.exists():
            base, dot_ext = os.path.splitext(filename)
            dest_path = res_dir / f"{base}_{doc_id[:6]}{dot_ext}"
            filename  = dest_path.name

        if download_attachment(vd_path, access_token, instance_url, str(dest_path)):
            downloaded_ids.append(doc_id)
            new_files.append(filename)
            log(f"Downloaded: {case_num}/RESOURCES/{filename}")

    if new_files:
        state.setdefault(case_num, {})["downloaded_files"] = downloaded_ids

    return new_files


# ── Token estimation ──────────────────────────────────────────────────────────

def estimate_tokens(file_path: str) -> int:
    """Returns approximate token count (chars/4), or -1 for binary/image files."""
    if Path(file_path).suffix.lower() not in TEXT_EXTENSIONS:
        return -1
    try:
        return max(1, len(Path(file_path).read_text(encoding="utf-8", errors="replace")) // 4)
    except Exception:
        return -1


def get_token_report(case_folder: Path) -> dict:
    """Returns {filename: token_count} for files in RESOURCES/ (excludes large_files/)."""
    res_dir = case_folder / "RESOURCES"
    if not res_dir.is_dir():
        return {}
    return {
        f.name: estimate_tokens(str(f))
        for f in sorted(res_dir.iterdir())
        if f.is_file() and f.parent.name != "large_files"
    }


# ── Summary generation ────────────────────────────────────────────────────────

def _fmt_date(iso: str) -> str:
    if not iso:
        return "N/A"
    return iso.replace("T", " ").replace(".000+0000", "").replace("+0000", "") + " UTC"


def generate_summary_md(case: dict, emails: list, comments: list) -> str:
    """Build the full summary.md markdown string from SF case data."""
    sf_id  = case.get("Id", "")
    sf_url = f"{SF_BASE_URL}/{sf_id}/view"

    account       = (case.get("Account") or {}).get("Name", "N/A")
    contact       = (case.get("Contact") or {}).get("Name", "N/A")
    contact_email = (case.get("Contact") or {}).get("Email", "")
    owner         = (case.get("Owner") or {}).get("Name", "N/A")
    contact_str   = contact + (f" ({contact_email})" if contact_email else "")

    md  = f"# Case {case.get('CaseNumber', '?')} - {case.get('Subject', 'N/A')}\n\n"
    md += "| Field | Value |\n|---|---|\n"
    md += f"| **Case #** | {case.get('CaseNumber', 'N/A')} |\n"
    md += f"| **Subject** | {case.get('Subject', 'N/A')} |\n"
    md += f"| **Customer** | {account} |\n"
    md += f"| **Contact** | {contact_str} |\n"
    md += f"| **Owner** | {owner} |\n"
    md += f"| **Status** | {case.get('Status', 'N/A')} |\n"
    md += f"| **Priority** | {case.get('Priority', 'N/A')} |\n"
    md += f"| **Origin** | {case.get('Origin', 'N/A')} |\n"
    md += f"| **Type** | {case.get('Type', 'N/A')} |\n"
    md += f"| **Created** | {_fmt_date(case.get('CreatedDate', ''))} |\n"
    md += f"| **Last Modified** | {_fmt_date(case.get('LastModifiedDate', ''))} |\n"
    md += f"| **SF Link** | {sf_url} |\n"

    description = (case.get("Description") or "_No description_").replace("\r\n", "\n").strip()
    md += f"\n---\n\n## Description\n\n{description}\n"

    # Build merged, sorted timeline
    timeline = []
    for cm in comments:
        is_internal = not cm.get("IsPublished", True)
        body = (cm.get("CommentBody") or "").replace("\r\n", "\n")
        body = body.split("\nFrom: Mend Support")[0].strip()
        author = (cm.get("CreatedBy") or {}).get("Name", "Unknown")
        timeline.append({
            "date":   cm.get("CreatedDate", ""),
            "author": author,
            "type":   "Internal" if is_internal else "Public Comment",
            "body":   body,
        })

    for em in emails:
        body = (em.get("TextBody") or "").replace("\r\n", "\n")
        body = body.split("\nFrom: Mend Support")[0]
        body = body.split("\n-----Original Message-----")[0].strip()
        is_incoming = em.get("Incoming", True)
        author = (em.get("FromName") or em.get("FromAddress") or "Unknown") if is_incoming else "Mend Support"
        timeline.append({
            "date":   em.get("MessageDate", ""),
            "author": author,
            "type":   "Customer Email" if is_incoming else "Support Email",
            "body":   body,
        })

    timeline.sort(key=lambda x: x["date"])

    if timeline:
        md += "\n---\n\n## Timeline\n\n"
        for entry in timeline:
            md += f"### {_fmt_date(entry['date'])} — {entry['author']} *({entry['type']})*\n\n"
            md += entry["body"] + "\n\n"

    md += "---\n\n## Next Steps\n\n_TODO: Update with next steps._\n"
    return md


def fetch_and_write_summary(case_number: str, case_folder: Path) -> tuple:
    """
    Query SF for case + emails + comments, write summary.md.
    Returns (case_dict, emails, comments) or (None, [], []) on failure.
    """
    records = run_soql(
        f"SELECT Id, OwnerId, CaseNumber, Subject, Description, Status, Priority, "
        f"Account.Name, Contact.Name, Contact.Email, CreatedDate, LastModifiedDate, "
        f"Owner.Name, Origin, Type FROM Case WHERE CaseNumber = '{case_number}'"
    )
    if not records:
        return None, [], []

    case    = records[0]
    case_id = case["Id"]

    emails = run_soql(
        f"SELECT Id, TextBody, FromName, FromAddress, Subject, MessageDate, Incoming "
        f"FROM EmailMessage WHERE ParentId = '{case_id}' ORDER BY MessageDate ASC"
    )
    comments = run_soql(
        f"SELECT Id, CommentBody, CreatedDate, CreatedBy.Name, IsPublished "
        f"FROM CaseComment WHERE ParentId = '{case_id}' ORDER BY CreatedDate ASC"
    )

    case_folder.mkdir(parents=True, exist_ok=True)
    (case_folder / "RESOURCES").mkdir(exist_ok=True)

    summary = generate_summary_md(case, emails, comments)
    (case_folder / "summary.md").write_text(summary, encoding="utf-8")

    return case, emails, comments


# ── Duplicate detection ───────────────────────────────────────────────────────

STOPWORDS = {
    "this", "that", "with", "have", "from", "they", "your", "will", "also",
    "been", "when", "what", "into", "some", "just", "case", "mend", "error",
    "issue", "please", "thank", "hello", "using", "would", "could", "should",
}


def find_similar_cases(case_number: str, subject: str, description: str, search_dir: Path) -> list:
    """Keyword overlap search against existing summary.md files. Returns top 3 matches."""
    if not search_dir.is_dir():
        return []

    text     = (subject + " " + (description or "")).lower()
    keywords = set(re.findall(r"\b[a-z]{4,}\b", text)) - STOPWORDS
    if not keywords:
        return []

    matches = []
    for case_dir in search_dir.iterdir():
        if not case_dir.is_dir() or case_dir.name == case_number:
            continue
        summary_file = case_dir / "summary.md"
        if not summary_file.exists():
            continue
        try:
            content = summary_file.read_text(encoding="utf-8", errors="replace").lower()
            score   = sum(1 for kw in keywords if kw in content)
            if score >= 3:
                heading = next(
                    (ln.lstrip("#").strip() for ln in content.splitlines()[:3] if ln.startswith("#")),
                    case_dir.name,
                )
                matches.append({"case_number": case_dir.name, "score": score, "subject": heading})
        except Exception:
            pass

    return sorted(matches, key=lambda x: x["score"], reverse=True)[:3]


# ── Digest ────────────────────────────────────────────────────────────────────

PRIORITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}


def build_digest_message(cases: list) -> str:
    """Format the daily digest Slack message from a list of SF case dicts."""
    today = datetime.now().strftime("%A %d %b %Y")
    lines = [f"*📋 Daily Digest — {today}*", f"_{len(cases)} open case(s)_\n"]

    grouped: dict = {}
    for c in cases:
        p = c.get("Priority", "Unknown")
        grouped.setdefault(p, []).append(c)

    for priority in sorted(grouped, key=lambda p: PRIORITY_ORDER.get(p, 99)):
        lines.append(f"*{priority}*")
        for c in grouped[priority]:
            num     = c.get("CaseNumber", "?")
            customer = ((c.get("Account") or {}).get("Name", "Unknown"))[:25]
            status  = c.get("Status", "?")
            created = (c.get("CreatedDate") or "")[:10]
            try:
                days = (datetime.now() - datetime.fromisoformat(created)).days
            except Exception:
                days = "?"
            sf_id  = c.get("Id", "")
            sf_url = f"{SF_BASE_URL}/{sf_id}/view"
            lines.append(f"• <{sf_url}|{num}> — {customer} | _{status}_ | {days}d")
        lines.append("")

    lines.append("_Type `case <NUMBER>` to process any case._")
    return "\n".join(lines)


# ── Slack ─────────────────────────────────────────────────────────────────────

def send_slack_dm(message: str):
    """Post a DM using config slack_bot_token → slack_user_id."""
    try:
        config  = load_config()
        token   = config.get("slack_bot_token", "")
        user_id = config.get("slack_user_id", "")
    except Exception:
        return

    if not token or not user_id:
        log("Slack not configured — cannot send DM")
        return

    _slack_post(token, user_id, message)


def _slack_post(token: str, channel: str, text: str, thread_ts: str = None) -> dict:
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                log(f"Slack post error: {result.get('error')}")
            return result
    except Exception as e:
        log(f"Slack post failed: {e}")
        return {}


def _slack_update(token: str, channel: str, ts: str, text: str):
    data = json.dumps({"channel": channel, "ts": ts, "text": text}).encode("utf-8")
    req  = urllib.request.Request(
        "https://slack.com/api/chat.update",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        log(f"Slack update failed: {e}")


# ── CLAUDE.md generation ──────────────────────────────────────────────────────

def regenerate_claude_md():
    """Write CLAUDE.md from the system_prompt in config.json."""
    try:
        config        = load_config()
        system_prompt = config.get("system_prompt", "You are a technical assistant.")
    except Exception:
        system_prompt = "You are a technical assistant."

    content = f"""# Mend Support Toolkit — AI Assistant Context

{system_prompt}

---

## Toolkit Structure
- Case summaries are in `summary.md` inside each case folder
- SF attachments are in `RESOURCES/` within each case folder
- Files moved due to token size are in `RESOURCES/large_files/` — excluded from AI context
- My Cases: `My Cases/<CASE_NUMBER>/`
- Staging cases: `Staging/<CASE_NUMBER>/`
- Watcher state: `.watcher_state.json`

## Mend Products Reference
- **SCA** — Software Composition Analysis; Unified Agent (Java), Mend CLI (`mend dep`)
- **SAST** — Static analysis; findings, suppressions, rules
- **Renovate / Remediate** — Automated dependency updates; self-hosted EE, cloud, workers, server
- **Container scanning** — Image vulnerability scanning via Mend CLI or platform integration
- **Mend Platform** — Unified UI; policies, workflows, alerts, SBOM

## Response Guidelines
- Be concise and technically precise
- **Case summaries**: identify root cause, customer impact, current status, recommended next steps
- **TKA ticket drafts**: clear title, reproduction steps, expected vs actual, affected versions
- **TKA creation**: Status must be `New`; leave Assignee unassigned
- **Domain / Sub-category suggestions**: only suggest values from the provided valid picklist options
- Salesforce URL: `https://whitesourcesoftware.lightning.force.com`
- Jira project key for internal tickets: `TKA`
"""

    (TOOLKIT_DIR / "CLAUDE.md").write_text(content, encoding="utf-8")
    log("CLAUDE.md regenerated")


# ── Google Drive (rclone) ─────────────────────────────────────────────────────

def archive_to_gdrive(local_path: Path, case_number: str) -> bool:
    """Upload a case folder to Google Drive via rclone. Returns True on success."""
    try:
        config    = load_config()
        remote    = config.get("gdrive_remote", "")
        folder_id = config.get("gdrive_folder_id", "")
    except Exception:
        return False

    if not remote:
        log("gdrive_remote not configured — skipping archive")
        return False

    # Build rclone destination: remote:path or remote:{folder_id}
    dest = f"{remote}:{folder_id}/{case_number}" if folder_id else f"{remote}:{case_number}"

    try:
        result = subprocess.run(
            ["rclone", "copy", str(local_path), dest, "--progress"],
            capture_output=True, text=True,
            shell=(os.name == "nt"),
            timeout=300,
        )
        if result.returncode == 0:
            log(f"Archived {case_number} to Google Drive ({dest})")
            return True
        else:
            log(f"rclone failed for {case_number}: {result.stderr.strip()}")
            return False
    except Exception as e:
        log(f"rclone error for {case_number}: {e}")
        return False
