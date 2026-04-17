# Option A — Personal Bot Per User: Action Plan

_Created: 2026-04-17_
_Supersedes: PLAN_option_b.md (Option B dropped — requires SF admin rights for Connected App)_

---

## Goal

Any Mend support engineer can clone the repo, run `python setup.py`, and have a fully working personal Slack bot in under 10 minutes. Each person runs their own bot process on their own machine using their own SF CLI session — fully isolated, no shared state, no central server.

---

## Architecture (Target State)

```
Colleague's machine                        William's machine
─────────────────────                      ─────────────────
case_bot.py (their process)                case_bot.py (your process)
    │                                          │
    ├── Their Slack app (xoxb-..., xapp-...)   ├── Your Slack app (xoxb-..., xapp-...)
    ├── Their SF CLI session                   ├── Your SF CLI session
    └── Their local CASES/ folder             └── Your local CASES/ folder
```

One GitHub repo, one `setup.py`, each person gets an independent instance.

**Key constraint:** Each user creates their own Slack app (~5 min at api.slack.com). No admin rights needed. The bot only talks to the person who installed it.

---

## What Was Already Done

- `setup.py` exists and handles most of the setup flow for Windows
- `case_bot.py` is functional but has no user-ID guard (anyone who finds the bot can use it)

---

## Phases

---

### Phase 0 — User-ID Guard ✅ (2026-04-17)
**Goal:** The bot only responds to the owner who configured it. All other Slack users get a friendly "not for you" message.

**Changes in `case_bot.py`:**
- Extract `sender_id = event.get("user", "")` in `handle_event`
- Pass `sender_id` into `dispatch()`
- At the start of `dispatch()`, compare `sender_id` to `config["slack_user_id"]`
- If mismatch: reply with a message explaining this is a personal bot and pointing to the repo

---

### Phase 1 — Cross-Platform setup.py ✅ (2026-04-17)
**Goal:** `setup.py` works on Windows and macOS so colleagues on either platform can run it.

**Changes in `setup.py`:**

| Area | Windows (existing) | macOS (added) |
|------|-------------------|---------------|
| SF CLI install | `winget install Salesforce.SFDXCLI` | `brew install sfdxcli` → `npm install -g @salesforce/cli` |
| rclone install | `winget install Rclone.Rclone` | `brew install rclone` |
| Background tasks | Windows Task Scheduler (PowerShell) | macOS launchd plists in `~/Library/LaunchAgents/` |
| Default cases path | `~/Documents/CASES` | `~/Documents/CASES` |
| Slack instructions | Updated to "create YOUR OWN Slack app" | Same |

---

### Phase 2 — GitHub Repo + README (next)
**Goal:** A public (or internal) GitHub repo that colleagues can clone with one URL, with a README that gets them running in under 10 minutes.

**Repo contents:**
```
case_bot.py
case_watcher.py
utils.py
setup.py
config.example.json
requirements.txt
CLAUDE.md
README.md          ← new, step-by-step quickstart
.gitignore
```

**README sections:**
1. Prerequisites (Python 3.9+, SF CLI, Claude CLI)
2. Clone the repo
3. Run `python setup.py`
4. Create your Slack app (with screenshots or links)
5. Troubleshooting

---

### Phase 3 — Validate on a Colleague's Machine (next)
**Goal:** End-to-end test on a fresh Windows machine and a macOS machine.

**Checklist:**
- [ ] Clone repo → `python setup.py` → bot starts
- [ ] DM the bot → `help` → responds correctly
- [ ] `case 00XXXXXX` → fetches, downloads attachments, creates folder
- [ ] `digest` → shows open cases
- [ ] Bot rejects DMs from a different Slack user (Phase 0 guard)
- [ ] Watcher auto-starts after reboot

---

## Files Modified

| File | Change |
|------|--------|
| `case_bot.py` | Phase 0 user-ID guard |
| `setup.py` | Cross-platform support (macOS brew/npm/launchd) + updated Slack instructions |
| `PLAN_option_a.md` | This file |
| `state_log.md` | Updated to reflect completed phases |

## Files Still Needed

| File | When |
|------|------|
| `README.md` | Phase 2 |
