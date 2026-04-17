#!/usr/bin/env python3
"""
uninstall.py — Mend Support Toolkit uninstaller.

Removes everything the toolkit installed on this machine:
  - Running bot / watcher processes
  - Windows: Startup folder shortcut + Task Scheduler entry
  - macOS: launchd plists (unloaded and deleted)
  - Generated launch scripts (start_bot.bat / .vbs, start_watcher.bat)
  - Runtime files: config.json, .watcher_state.json, toolkit.log, logs/
  - Optionally: your case data folders (My Cases, Staging, Other Cases)
  - Optionally: the entire toolkit directory

Note: your Slack app must be deleted manually at https://api.slack.com/apps
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

TOOLKIT_DIR = Path(__file__).parent
CONFIG_PATH = TOOLKIT_DIR / "config.json"

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):   print(f"  {GREEN}[REMOVED]{RESET}  {msg}")
def skip(msg): print(f"  {YELLOW}[SKIPPED]{RESET}  {msg}")
def warn(msg): print(f"  {YELLOW}[WARNING]{RESET}  {msg}")
def err(msg):  print(f"  {RED}[ERROR]{RESET}    {msg}")
def info(msg): print(f"             {msg}")
def section(title):
    print(f"\n{BOLD}{'─'*60}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'─'*60}{RESET}")

def ask_yn(prompt: str, default: bool = False) -> bool:
    hint = "[y/N]" if not default else "[Y/n]"
    try:
        val = input(f"  {prompt} {hint}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if not val:
        return default
    return val in ("y", "yes")


# ── Stop running processes ────────────────────────────────────────────────────

def stop_processes():
    section("Stop running processes")

    scripts = ["case_bot.py", "case_watcher.py"]

    if IS_WIN:
        for script in scripts:
            result = subprocess.run(
                ["taskkill", "/F", "/FI", f"WINDOWTITLE eq {script}", "/IM", "python.exe"],
                capture_output=True, text=True
            )
            # Also try pythonw
            subprocess.run(
                ["taskkill", "/F", "/FI", f"WINDOWTITLE eq {script}", "/IM", "pythonw.exe"],
                capture_output=True, text=True
            )
            # Broader: kill any python process with the script name in its command line
            ps_kill = (
                f'Get-WmiObject Win32_Process | Where-Object {{ $_.CommandLine -like "*{script}*" }} '
                f'| ForEach-Object {{ $_.Terminate() }}'
            )
            result = subprocess.run(
                ["powershell", "-ExecutionPolicy", "Bypass", "-Command", ps_kill],
                capture_output=True, text=True
            )
            ok(f"Sent stop signal for {script}")
    else:
        for script in scripts:
            result = subprocess.run(
                ["pkill", "-f", script],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                ok(f"Stopped process: {script}")
            else:
                skip(f"{script} was not running")


# ── Windows: Startup folder + Task Scheduler ─────────────────────────────────

def remove_windows_autostart():
    section("Windows autostart entries")

    # Startup folder shortcut
    startup_dir = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    shortcut = startup_dir / "MendCaseBot.lnk"
    if shortcut.exists():
        shortcut.unlink()
        ok(f"Startup shortcut removed: {shortcut}")
    else:
        skip(f"Startup shortcut not found: {shortcut}")

    # Task Scheduler
    result = subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-Command",
         'Unregister-ScheduledTask -TaskName "MendCaseWatcher" -Confirm:$false; Write-Output "OK"'],
        capture_output=True, text=True, timeout=15
    )
    if "OK" in result.stdout:
        ok("Task Scheduler: MendCaseWatcher removed")
    else:
        skip("Task Scheduler: MendCaseWatcher not found (or already removed)")


# ── macOS: launchd plists ─────────────────────────────────────────────────────

def remove_macos_launchd():
    section("macOS launchd agents")

    agents_dir = Path.home() / "Library" / "LaunchAgents"
    plists = [
        agents_dir / "io.mend.casewatcher.plist",
        agents_dir / "io.mend.casebot.plist",
    ]

    for plist in plists:
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
            plist.unlink()
            ok(f"launchd agent removed: {plist.name}")
        else:
            skip(f"launchd agent not found: {plist.name}")


# ── Generated launch scripts ──────────────────────────────────────────────────

def remove_launch_scripts():
    section("Generated launch scripts")

    scripts = [
        TOOLKIT_DIR / "start_bot.bat",
        TOOLKIT_DIR / "start_bot.vbs",
        TOOLKIT_DIR / "start_watcher.bat",
    ]

    for path in scripts:
        if path.exists():
            path.unlink()
            ok(str(path.name))
        else:
            skip(f"{path.name} not found")


# ── Runtime files ─────────────────────────────────────────────────────────────

def remove_runtime_files():
    section("Runtime files")

    files = [
        CONFIG_PATH,
        TOOLKIT_DIR / ".watcher_state.json",
        TOOLKIT_DIR / "toolkit.log",
        TOOLKIT_DIR / "CLAUDE.md",
    ]

    for path in files:
        if path.exists():
            path.unlink()
            ok(str(path.name))
        else:
            skip(f"{path.name} not found")

    logs_dir = TOOLKIT_DIR / "logs"
    if logs_dir.exists():
        shutil.rmtree(logs_dir)
        ok("logs/")
    else:
        skip("logs/ not found")

    pycache = TOOLKIT_DIR / "__pycache__"
    if pycache.exists():
        shutil.rmtree(pycache)
        ok("__pycache__/")


# ── Case data ─────────────────────────────────────────────────────────────────

def remove_case_data():
    section("Case data folders")

    # Try to read cases_parent_dir from config (may already be deleted)
    case_dirs = []
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                config = json.load(f)
            for key in ("my_cases_dir", "staging_dir", "other_cases_dir"):
                val = config.get(key, "")
                if val and Path(val).exists():
                    case_dirs.append(Path(val))
        except Exception:
            pass

    if not case_dirs:
        skip("No case data folders found (config already removed or dirs not set)")
        return

    print()
    info("Found the following case data folders:")
    total_size = 0
    for d in case_dirs:
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        total_size += size
        info(f"  {d}  ({size / 1024 / 1024:.1f} MB)")
    info(f"  Total: {total_size / 1024 / 1024:.1f} MB")
    print()
    warn("This will permanently delete all local case folders and summaries.")

    if ask_yn("Delete case data folders?", default=False):
        for d in case_dirs:
            shutil.rmtree(d, ignore_errors=True)
            ok(str(d))

        # Remove parent if now empty
        if case_dirs:
            parent = case_dirs[0].parent
            try:
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
                    ok(f"Removed empty parent: {parent}")
            except Exception:
                pass
    else:
        skip("Case data kept")


# ── Toolkit directory ─────────────────────────────────────────────────────────

def remove_toolkit_dir():
    section("Toolkit directory")

    info(f"Toolkit directory: {TOOLKIT_DIR}")
    info("This contains all the source files (case_bot.py, setup.py, etc.)")
    print()
    warn("If you delete this you will need to re-clone the repo to use the toolkit again.")

    if ask_yn("Delete the entire toolkit directory?", default=False):
        # Can't delete the directory we're running from on Windows while running
        # Schedule deletion via a temp script instead
        if IS_WIN:
            del_bat = Path(os.environ.get("TEMP", "C:\\Temp")) / "mend_cleanup.bat"
            del_bat.write_text(
                f"@echo off\r\n"
                f"timeout /t 2 /nobreak > nul\r\n"
                f"rd /s /q \"{TOOLKIT_DIR}\"\r\n"
                f"del \"%~f0\"\r\n",
                encoding="utf-8"
            )
            subprocess.Popen(["cmd", "/c", str(del_bat)], creationflags=0x00000008)  # DETACHED_PROCESS
            ok(f"Toolkit directory will be deleted in ~2 seconds: {TOOLKIT_DIR}")
        else:
            # On macOS/Linux we can schedule via a subshell
            subprocess.Popen(
                f'sleep 2 && rm -rf "{TOOLKIT_DIR}"',
                shell=True, start_new_session=True
            )
            ok(f"Toolkit directory will be deleted in ~2 seconds: {TOOLKIT_DIR}")
    else:
        skip("Toolkit directory kept")


# ── Slack reminder ────────────────────────────────────────────────────────────

def slack_reminder():
    section("Manual step required: Slack app")
    info("Your Slack app cannot be removed automatically.")
    info("To delete it:")
    info("  1. Go to https://api.slack.com/apps")
    info("  2. Select your 'Mend Case Watcher - <your name>' app")
    info("  3. Settings → Basic Information → scroll to bottom → Delete App")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}   Mend Support Toolkit — Uninstaller{RESET}")
    print(f"{BOLD}{'═'*60}{RESET}")
    print()
    print(f"  {YELLOW}This will remove the Mend Support Toolkit from this machine.{RESET}")
    print(f"  {YELLOW}You will be asked to confirm each destructive step.{RESET}")
    print()

    if not ask_yn("Continue with uninstall?", default=False):
        print("  Uninstall cancelled.")
        sys.exit(0)

    stop_processes()

    if IS_WIN:
        remove_windows_autostart()
    elif IS_MAC:
        remove_macos_launchd()

    remove_launch_scripts()
    remove_runtime_files()
    remove_case_data()
    remove_toolkit_dir()
    slack_reminder()

    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{GREEN}{BOLD}  Uninstall complete.{RESET}")
    print(f"{BOLD}{'═'*60}{RESET}\n")


if __name__ == "__main__":
    main()
