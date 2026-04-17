#!/usr/bin/env python3
"""
setup.py — Mend Support Toolkit  First-Run Setup & Onboarding Wizard

Run once (or re-run any time to update config / re-register tasks):
    python setup.py

Steps:
  1. Check Python version
  2. Install pip dependencies
  3. Check / install sf CLI (Salesforce)
  4. Check Claude CLI
  5. Create config.json from example
  6. Set up Salesforce authentication
  7. Configure Slack tokens
  8. Discover SF Domain / Sub-category field names
  9. Set up My Cases + Staging directories
 10. Register background tasks (Windows Task Scheduler / macOS launchd)
 11. Generate CLAUDE.md
 12. Final summary
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

TOOLKIT_DIR   = Path(__file__).parent
CONFIG_PATH   = TOOLKIT_DIR / "config.json"
EXAMPLE_PATH  = TOOLKIT_DIR / "config.example.json"
MIN_PYTHON    = (3, 9)

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

# ── Colour helpers (Windows-safe) ─────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):    print(f"  {GREEN}[OK]{RESET}  {msg}")
def warn(msg):  print(f"  {YELLOW}[!!]{RESET}  {msg}")
def err(msg):   print(f"  {RED}[ERR]{RESET} {msg}")
def info(msg):  print(f"       {msg}")
def section(title):
    print(f"\n{BOLD}{'─'*60}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'─'*60}{RESET}")


def ask(prompt: str, default: str = "") -> str:
    display = f" [{default}]" if default else ""
    try:
        val = input(f"  {prompt}{display}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val or default


def ask_yn(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        val = input(f"  {prompt} {hint}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if not val:
        return default
    return val in ("y", "yes")


def open_url(url: str):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        info(f"Open manually: {url}")


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        shell=(os.name == "nt"), **kwargs
    )


def winget_install(package_id: str, display_name: str) -> bool:
    """Try to install a package via winget. Returns True on success."""
    if not shutil.which("winget"):
        return False
    print(f"       Trying: winget install {package_id} ...")
    result = run(["winget", "install", "--id", package_id, "-e", "--accept-source-agreements",
                  "--accept-package-agreements"])
    if result.returncode == 0:
        ok(f"{display_name} installed via winget.")
        return True
    return False


# ── Step 1: Python version ────────────────────────────────────────────────────

def check_python():
    section("Step 1 — Python version")
    if sys.version_info < MIN_PYTHON:
        err(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required. You have {sys.version}.")
        sys.exit(1)
    ok(f"Python {sys.version.split()[0]}")


# ── Step 2: pip dependencies ──────────────────────────────────────────────────

def install_deps():
    section("Step 2 — Installing pip dependencies")
    req_file = TOOLKIT_DIR / "requirements.txt"
    if not req_file.exists():
        warn("requirements.txt not found — skipping.")
        return
    result = run([sys.executable, "-m", "pip", "install", "-r", str(req_file), "--quiet"])
    if result.returncode == 0:
        ok("pip dependencies installed.")
    else:
        err(f"pip install failed:\n{result.stderr}")
        sys.exit(1)


# ── Step 3: Salesforce CLI ────────────────────────────────────────────────────

def check_sf():
    section("Step 3 — Salesforce CLI (sf)")
    if shutil.which("sf"):
        result = run(["sf", "--version"])
        ok(f"sf CLI found: {result.stdout.strip()[:60]}")
        return
    warn("sf CLI not found.")
    installed = False
    if IS_WIN:
        if ask_yn("Try to install via winget?"):
            installed = winget_install("Salesforce.SFDXCLI", "sf CLI")
    elif IS_MAC:
        if shutil.which("brew") and ask_yn("Try to install via Homebrew?"):
            info("Running: brew install sfdxcli ...")
            result = run(["brew", "install", "sfdxcli"])
            if result.returncode == 0:
                ok("sf CLI installed via Homebrew.")
                installed = True
        if not installed and shutil.which("npm") and ask_yn("Try to install via npm?"):
            info("Running: npm install -g @salesforce/cli ...")
            result = run(["npm", "install", "-g", "@salesforce/cli"])
            if result.returncode == 0:
                ok("sf CLI installed via npm.")
                installed = True
    if not installed:
        if ask_yn("Open download page in browser?"):
            open_url("https://developer.salesforce.com/tools/salesforcecli")
        err("sf CLI is required. Install it and re-run setup.py.")
        sys.exit(1)


# ── Step 4: Claude CLI ────────────────────────────────────────────────────────

def check_claude():
    section("Step 4 — Claude CLI")
    if shutil.which("claude"):
        ok("Claude CLI found.")
        return
    warn("Claude CLI not found.")
    info("Claude CLI is required for AI features (summarize, claim, tka).")
    info("Install it from: https://claude.ai/download  (Claude Code)")
    if ask_yn("Open download page?"):
        open_url("https://claude.ai/download")
    if not ask_yn("Continue without Claude CLI (AI features will be unavailable)?", default=True):
        sys.exit(0)


# ── Step 5: config.json ───────────────────────────────────────────────────────

def setup_config() -> dict:
    section("Step 5 — config.json")

    if not EXAMPLE_PATH.exists():
        err("config.example.json not found. Make sure it is in the same folder.")
        sys.exit(1)

    with open(EXAMPLE_PATH, encoding="utf-8") as f:
        example = json.load(f)

    if CONFIG_PATH.exists():
        ok("config.json already exists — loading existing values.")
        with open(CONFIG_PATH, encoding="utf-8") as f:
            config = json.load(f)
        # Merge any new keys from example
        for k, v in example.items():
            if not k.startswith("_") and k not in config:
                config[k] = v
    else:
        config = {k: v for k, v in example.items() if not k.startswith("_")}
        ok("Created config.json from template.")

    return config


# ── Step 6: Salesforce auth ───────────────────────────────────────────────────

def setup_sf(config: dict) -> dict:
    section("Step 6 — Salesforce authentication")

    sf_user = config.get("sf_user", "")
    if not sf_user:
        sf_user = ask("Your Salesforce / Mend email (e.g. you@mend.io)")
        config["sf_user"] = sf_user

    # Test auth
    cmd = ["sf", "org", "display", "--json"]
    if sf_user:
        cmd += ["--target-org", sf_user]
    result = run(cmd)

    try:
        data = json.loads(result.stdout)
        if data.get("status") == 0 and data.get("result", {}).get("accessToken"):
            ok(f"SF authenticated as {sf_user}")
            return config
    except Exception:
        pass

    warn("SF session not found or expired.")
    if ask_yn("Open SF login in browser now?"):
        login_cmd = ["sf", "org", "login", "web", "--set-default"]
        if sf_user:
            login_cmd += ["--alias", sf_user]
        subprocess.run(login_cmd, shell=(os.name == "nt"))
        ok("SF login complete.")
    else:
        info("Run `sf org login web --set-default` in your terminal before using the toolkit.")

    return config


# ── Step 7: Slack tokens ──────────────────────────────────────────────────────

def setup_slack(config: dict) -> dict:
    section("Step 7 — Slack configuration")

    info("Each person runs their own Slack bot — you need YOUR OWN Slack app.")
    info("The repo includes a manifest that pre-configures everything in one paste.")
    info("This takes about 2 minutes and does not require workspace admin rights.")
    info("")
    info("  1. https://api.slack.com/apps → Create New App → From a manifest")
    info("  2. Select your Mend workspace, then paste the contents of:")
    info(f"     {TOOLKIT_DIR / 'slack_app_manifest.yaml'}")
    info("  3. On the review screen, RENAME the app from 'Mend Case Watcher - YOUR NAME HERE'")
    info("     to something like 'Mend Case Watcher - William'")
    info("  4. Create — all scopes and settings are pre-filled")
    info("  5. Basic Information → App-Level Tokens → Generate Token and Scopes")
    info("       Scope: connections:write  |  Copy the xapp-... token")
    info("  6. Install App → 'Request to Install' (needs admin approval, usually fast)")
    info("     OR 'Install to Workspace' if approval not required → copy the xoxb-... token")
    print()

    if ask_yn("Open Slack API page now?", default=False):
        open_url("https://api.slack.com/apps")

    bot_token = config.get("slack_bot_token", "")
    if not bot_token or not bot_token.startswith("xoxb-"):
        bot_token = ask("Paste Bot User OAuth Token (xoxb-...)")
        config["slack_bot_token"] = bot_token

    app_token = config.get("slack_app_token", "")
    if not app_token or not app_token.startswith("xapp-"):
        app_token = ask("Paste App-Level Token (xapp-...)")
        config["slack_app_token"] = app_token

    # Ask for Slack user ID directly — avoids the users:read.email scope
    # which often requires workspace admin approval
    if not config.get("slack_user_id"):
        info("")
        info("Your Slack user ID is needed so the bot knows who to notify.")
        info("Find it in Slack: click your avatar → Profile → ⋯ (More) → Copy member ID")
        uid = ask("Paste your Slack member ID (e.g. U09UNTT3FGT)")
        if uid:
            config["slack_user_id"] = uid

    if config.get("slack_user_id"):
        ok(f"Slack configured. Bot will respond to {config['slack_user_id']}")
    else:
        warn("slack_user_id not set — the bot will not respond to anyone. Set it with `config slack_user_id <ID>`")

    return config


# ── Step 8: SF field discovery ────────────────────────────────────────────────

_DEFAULT_DOMAIN_FIELD  = "Domain_Category__c"
_DEFAULT_SUBCAT_FIELD  = "Domain_Sub_Category__c"


def _get_sf_describe(config: dict):
    """Return (access_token, instance_url, describe_json) or (None, None, None)."""
    import urllib.request
    try:
        cmd = ["sf", "org", "display", "--json"]
        if config.get("sf_user"):
            cmd += ["--target-org", config["sf_user"]]
        result = run(cmd)
        data   = json.loads(result.stdout)
        info_d = data.get("result", {})
        access_token = info_d.get("accessToken", "")
        instance_url = info_d.get("instanceUrl", "")
        if not access_token or not instance_url:
            return None, None, None
        req = urllib.request.Request(
            f"{instance_url}/services/data/v62.0/sobjects/Case/describe",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            describe = json.loads(resp.read())
        return access_token, instance_url, describe
    except Exception as e:
        warn(f"SF describe failed: {e}")
        return None, None, None


def setup_sf_fields(config: dict) -> dict:
    section("Step 8 — SF Domain / Sub-category fields")

    info("The 'claim' command updates Domain and Sub-category on the SF case.")
    info(f"Default field names: {_DEFAULT_DOMAIN_FIELD}  /  {_DEFAULT_SUBCAT_FIELD}")
    info("Validating against your SF org...")
    print()

    domain_field = config.get("sf_domain_field", _DEFAULT_DOMAIN_FIELD)
    subcat_field = config.get("sf_subcategory_field", _DEFAULT_SUBCAT_FIELD)

    _, _, describe = _get_sf_describe(config)

    if describe:
        all_field_names = {f["name"] for f in describe.get("fields", [])}
        picklist_fields = [
            f["name"] for f in describe.get("fields", [])
            if f.get("type") == "picklist" and f.get("name", "").endswith("__c")
        ]

        domain_ok = domain_field in all_field_names
        subcat_ok = subcat_field in all_field_names

        if domain_ok and subcat_ok:
            ok(f"Default fields confirmed in SF: {domain_field} / {subcat_field}")
            config["sf_domain_field"]      = domain_field
            config["sf_subcategory_field"] = subcat_field
            info("(Picklist values are cached automatically on first use.)")
            return config

        # One or both defaults not found — show picklist fields and ask
        warn(f"Field(s) not found in SF org:")
        if not domain_ok:
            warn(f"  '{domain_field}' — not found")
        if not subcat_ok:
            warn(f"  '{subcat_field}' — not found")
        print()
        if picklist_fields:
            info("Custom picklist fields available on Case:")
            for i, fname in enumerate(picklist_fields[:30], 1):
                info(f"  {i:2}. {fname}")
            print()
    else:
        warn("Could not reach SF — skipping validation. Using defaults (can be changed later).")

    new_domain = ask("Domain field API name", default=domain_field)
    new_subcat = ask("Sub-category field API name", default=subcat_field)
    config["sf_domain_field"]      = new_domain
    config["sf_subcategory_field"] = new_subcat
    ok(f"Fields set: {new_domain}  /  {new_subcat}")
    info("(Picklist values are cached automatically on first use.)")

    return config


# ── Step 9: Directories ──────────────────────────────────────────────────────

def setup_directories(config: dict) -> dict:
    section("Step 9 — Case directories")

    info("All case folders live under a single parent directory.")
    info("Three sub-folders will be created automatically:")
    info("  My Cases/    — cases assigned to you")
    info("  Staging/     — unassigned queue cases")
    info("  Other Cases/ — cases you look up that belong to someone else")
    print()

    # Determine default parent: use existing my_cases_dir parent if already set,
    # otherwise default to TOOLKIT_DIR.
    existing_parent = ""
    if config.get("cases_parent_dir"):
        existing_parent = config["cases_parent_dir"]
    elif config.get("my_cases_dir"):
        existing_parent = str(Path(config["my_cases_dir"]).parent)

    default_parent = existing_parent or str(Path.home() / "Documents" / "CASES")
    parent = ask("Parent directory for all case folders", default=default_parent)
    parent_path = Path(parent)

    sub_dirs = {
        "my_cases_dir":    parent_path / "My Cases",
        "staging_dir":     parent_path / "Staging",
        "other_cases_dir": parent_path / "Other Cases",
    }

    for key, path in sub_dirs.items():
        path.mkdir(parents=True, exist_ok=True)
        config[key] = str(path)
        label = key.replace("_dir", "").replace("_", " ").title()
        ok(f"{label:<14}: {path}")

    config["cases_parent_dir"] = str(parent_path)
    return config


# ── Step 10: Background tasks (cross-platform) ───────────────────────────────

def _setup_windows_tasks():
    python_exe = sys.executable
    watcher_py = str(TOOLKIT_DIR / "case_watcher.py")
    bot_py     = str(TOOLKIT_DIR / "case_bot.py")

    watcher_ps = f"""
$action   = New-ScheduledTaskAction -Execute '{python_exe}' -Argument '{watcher_py}'
$trigger  = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 5) -Once -At (Get-Date)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 4) `
              -StartWhenAvailable -MultipleInstances IgnoreNew -Hidden $true
Register-ScheduledTask -TaskName 'MendCaseWatcher' `
  -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
Write-Output 'OK'
"""
    result = subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-Command", watcher_ps],
        capture_output=True, text=True, timeout=30
    )
    if "OK" in result.stdout:
        ok("Task Scheduler: MendCaseWatcher registered (every 5 min)")
    else:
        warn(f"Watcher task registration failed: {result.stderr.strip()[:200]}")
        info("Run setup.py as Administrator, or register the task manually.")

    bot_ps = f"""
$action   = New-ScheduledTaskAction -Execute '{python_exe}' -Argument '{bot_py}'
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
              -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 5) -StartWhenAvailable
Register-ScheduledTask -TaskName 'MendCaseBot' `
  -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
Write-Output 'OK'
"""
    result = subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-Command", bot_ps],
        capture_output=True, text=True, timeout=30
    )
    if "OK" in result.stdout:
        ok("Task Scheduler: MendCaseBot registered (starts at login, auto-restarts)")
    else:
        warn(f"Bot task registration failed: {result.stderr.strip()[:200]}")
        info("Run setup.py as Administrator to register the bot task.")


def _setup_macos_launchd():
    python_exe  = sys.executable
    watcher_py  = str(TOOLKIT_DIR / "case_watcher.py")
    bot_py      = str(TOOLKIT_DIR / "case_bot.py")
    log_path    = str(TOOLKIT_DIR / "toolkit.log")
    agents_dir  = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    watcher_plist = agents_dir / "io.mend.casewatcher.plist"
    watcher_plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>io.mend.casewatcher</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_exe}</string>
        <string>{watcher_py}</string>
    </array>
    <key>StartInterval</key><integer>300</integer>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>{log_path}</string>
    <key>StandardErrorPath</key><string>{log_path}</string>
</dict>
</plist>
""", encoding="utf-8")

    bot_plist = agents_dir / "io.mend.casebot.plist"
    bot_plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>io.mend.casebot</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_exe}</string>
        <string>{bot_py}</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{log_path}</string>
    <key>StandardErrorPath</key><string>{log_path}</string>
</dict>
</plist>
""", encoding="utf-8")

    for label, plist in [("MendCaseWatcher", watcher_plist), ("MendCaseBot", bot_plist)]:
        subprocess.run(["launchctl", "unload", str(plist)],
                       capture_output=True)  # ignore errors if not loaded yet
        result = subprocess.run(["launchctl", "load", str(plist)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            ok(f"launchd: {label} registered ({plist.name})")
        else:
            warn(f"launchctl load failed for {label}: {result.stderr.strip()[:200]}")


def setup_background_tasks():
    section("Step 10 — Background tasks")
    if IS_WIN:
        _setup_windows_tasks()
    elif IS_MAC:
        _setup_macos_launchd()
    else:
        info("Linux detected — register tasks manually:")
        info("  Watcher (cron): */5 * * * * " + sys.executable + " " + str(TOOLKIT_DIR / "case_watcher.py"))
        info("  Bot (startup):  add `python case_bot.py &` to ~/.bashrc or a systemd user service")


# ── Step 11: CLAUDE.md ────────────────────────────────────────────────────────

def setup_claude_md():
    section("Step 11 — CLAUDE.md (AI system prompt)")

    sys.path.insert(0, str(TOOLKIT_DIR))
    try:
        from utils import regenerate_claude_md
        regenerate_claude_md()
        ok(f"CLAUDE.md written to {TOOLKIT_DIR / 'CLAUDE.md'}")
        info("Edit system_prompt in config.json (or use `config system-prompt ...`)")
        info("to customise the AI assistant's context and behaviour.")
    except Exception as e:
        warn(f"Could not generate CLAUDE.md: {e}")


# ── Step 12: Final summary ────────────────────────────────────────────────────

def final_summary(config: dict):
    section("Setup complete!")

    print(f"""
  {GREEN}{BOLD}✓ Mend Support Toolkit is ready.{RESET}

  {BOLD}To start the bot manually:{RESET}
    python case_bot.py

  {BOLD}To run the watcher manually:{RESET}
    python case_watcher.py

  {BOLD}Then DM your Slack bot:{RESET}
    help          — see all commands
    case 00165609 — try fetching a case
    staging       — check the staging queue
    digest        — see your open cases

  {BOLD}Config:{RESET}         {CONFIG_PATH}
  {BOLD}Logs:{RESET}           {TOOLKIT_DIR / 'toolkit.log'}
  {BOLD}My Cases:{RESET}       {config.get('my_cases_dir', '(not set)')}
  {BOLD}Staging:{RESET}        {config.get('staging_dir', '(not set)')}
  {BOLD}Other Cases:{RESET}    {config.get('other_cases_dir', '(not set)')}
  {BOLD}CLAUDE.md:{RESET}      {TOOLKIT_DIR / 'CLAUDE.md'}
""")

    if not config.get("jira_account_id"):
        info("TIP: For TKA ticket creation, set your Jira account ID:")
        info("     config jira-account-id <your-jira-account-id>")
        info("     (Find it in your Jira profile URL)")
        print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}   Mend Support Toolkit — Setup Wizard{RESET}")
    print(f"{BOLD}{'═'*60}{RESET}")

    check_python()
    install_deps()
    check_sf()
    check_claude()

    config = setup_config()
    config = setup_sf(config)
    config = setup_slack(config)
    config = setup_sf_fields(config)
    config = setup_directories(config)

    # Save config before registering tasks (tasks need the saved config)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    ok("config.json saved.")

    setup_background_tasks()
    setup_claude_md()
    final_summary(config)


if __name__ == "__main__":
    main()
