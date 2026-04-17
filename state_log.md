# Mend Support Toolkit v2.0 — Project State Log

_Last updated: 2026-04-17 (rev 5)_

---

## Purpose

Personal productivity tool for a Mend Technical Support Engineer. Automates the most manual parts of Salesforce case management: polling for new/updated cases, downloading attachments, routing cases into a local folder hierarchy, generating AI-assisted summaries, and surfacing everything through a Slack bot. Claude CLI is invoked non-interactively for summary generation and TKA draft creation.

---

## Architecture

```
case_watcher.py     — Background poller (Windows Task Scheduler, every 5 min)
                      Syncs SF cases, downloads attachments, fires Slack alerts
case_bot.py         — Slack bot (Socket Mode, always-on listener)
                      Handles DM commands: case, claim, tka, summarize, digest, archive, move, etc.
utils.py            — Shared library: SF API calls, Slack messaging, token estimation,
                      summary generation, duplicate detection, picklist caching
setup.py            — First-run wizard: installs deps, configures auth, registers Task Scheduler tasks
config.json         — Runtime config (tokens, paths, SLA thresholds, field names)
.watcher_state.json — Persistent polling state: timestamps, downloaded file IDs, watched cases
toolkit.log         — Audit log of all bot and watcher actions
```

**Case folder layout:**
```
CASES_2/
  My Cases/<CASE_NUMBER>/
    summary.md
    RESOURCES/
      large_files/    ← files excluded from AI context due to token size
  Staging/<CASE_NUMBER>/
  Other Cases/<CASE_NUMBER>/
```

---

## Component Status

### `case_watcher.py` — FUNCTIONAL
- Polls My Cases queue and Staging queue on separate intervals (configurable; defaults 15 min / 5 min)
- Downloads new SF attachments; skips already-downloaded files via `.watcher_state.json`
- Sends Slack alert on new/updated cases and SLA breach warnings
- Moves large attachments to `RESOURCES/large_files/` if they exceed the token budget

### `case_bot.py` — FUNCTIONAL
- Commands: `case <num>`, `claim <num>`, `tka <num>`, `summarize <num>`, `digest`, `archive <num>`, `move <num>`, `status`, `help`
- `case` command: fetches case from SF, downloads attachments, routes to correct folder, generates summary
- `tka` command: drafts a Jira TKA ticket using Claude; **does not yet POST to Jira** (draft only, printed to Slack)
- `summarize` command: regenerates `summary.md` for an existing local case
- `digest` command: generates a daily digest of open cases with SLA status
- `move` command: moves a case between My Cases / Staging / Other Cases folders

### `utils.py` — FUNCTIONAL
- `get_sf_case()` — fetches case fields via SF CLI SOQL
- `get_sf_attachments()` — fetches attachment list via SF REST API
- `download_sf_attachment()` — downloads attachment content
- `resolve_sf_user_id()` — multi-method SF user ID resolution (REST, org display, SOQL fallback)
- `estimate_tokens()` — crude token estimator (len / 4); used for large-file gating
- `is_likely_duplicate()` — keyword similarity check against existing summaries
- `send_slack_message()` / `send_slack_block()` — Slack messaging wrappers
- `get_picklist_values()` — cached SF describe call for domain/sub-category picklists

### `setup.py` — FUNCTIONAL
- Checks Python ≥ 3.9, installs pip deps, verifies SF CLI and Claude CLI presence
- Creates `config.json` from `config.example.json` via interactive prompts
- Initiates SF web-browser auth flow
- Registers `case_watcher.py` as a Windows Task Scheduler task
- Regenerates `CLAUDE.md` with current paths

---

## Known Issues / Gaps

| Priority | Issue | Notes |
|----------|-------|-------|
| High | `--dangerously-skip-permissions` used on Claude CLI calls | Should register specific MCP tools instead of bypassing all guards |
| High | Plaintext tokens in `config.json` | Slack bot token, SF session tokens stored unencrypted |
| High | Windows Task Scheduler registration broken | `setup.py` Step 12 runs without error but tasks are not created; bot and watcher must be started manually for now (`python case_bot.py`, `python case_watcher.py`) |
| Medium | No retry/backoff on SF API calls | Expired session silently kills watcher; needs exponential backoff |
| Medium | Race condition on `.watcher_state.json` | No file locking; concurrent watcher + bot writes could corrupt state |
| Medium | Jira TKA creation not implemented | `tka` command drafts ticket text but doesn't POST to Jira API |
| Low | SF staging report ID in config.example.json | Currently set to William's report; confirm it's the shared team report or update per-user |
| Low | `requirements.txt` incomplete | Only lists `slack_sdk`; other dependencies rely on stdlib or aren't pinned |
| Low | Token estimation is crude | `len(text) / 4` — could use `tiktoken` or Claude's tokenizer for accuracy |
| Low | SF API version hardcoded | `v62.0` in REST calls; should be config-driven |
| Low | No unit tests | Zero test coverage across all modules |

---

## Completed Work (Chronological)

- **v1.0** — Initial Slack bot with manual `case` command and basic SF attachment download
- **v2.0** — Major rebuild:
  - Added `case_watcher.py` background poller with Task Scheduler integration
  - Added SLA threshold alerting (Critical/High/Medium/Low)
  - Added `utils.py` shared library to eliminate duplication between bot and watcher
  - Added `setup.py` first-run wizard
  - Added token budget management and `large_files/` routing
  - Added `digest` command for daily SLA overview
  - Added duplicate detection for summaries
  - Added `move` command for case reclassification
  - Added picklist caching to speed up TKA drafts
- **v2.1 (2026-04-17, dev branch)** — Multi-user / cross-platform groundwork:
  - **User-ID guard** (`case_bot.py`): bot now rejects DMs from Slack users who aren't the configured owner; replies with a pointer to the repo to set up their own instance
  - **Cross-platform `setup.py`**: macOS support added throughout
    - SF CLI: tries `brew install sfdxcli` → `npm install -g @salesforce/cli` fallback
    - rclone: tries `brew install rclone` fallback
    - Background tasks: macOS `launchd` plist generation in `~/Library/LaunchAgents/` (watcher every 5 min, bot at login with KeepAlive); Windows path unchanged
    - Default cases directory: now `~/Documents/CASES` instead of toolkit dir
    - Slack setup instructions updated: each user must create their own Slack app
  - **`PLAN_option_a.md`**: deployment strategy documented (Option B dropped — requires SF admin for Connected App)
  - **Git initialised** on `master`; all active development on `dev` branch
  - **Slack app manifest** (`slack_app_manifest.yaml`): pre-configured app definition colleagues can import at api.slack.com in one paste — eliminates manual scope/event/Socket Mode setup
  - **`users:read.email` scope removed** from manifest and setup.py; Slack user ID now entered manually (avoids workspace admin approval requirement)

_As of 2026-04-17, the tool is in active daily use. Recent log activity shows cases being processed across My Cases and Other Cases queues._

---

## Suggested Next Steps

These are ordered by impact. Pick up from wherever makes sense:

1. **Fix Windows Task Scheduler registration** — `setup.py` Step 12 runs without error but tasks are not created. Root cause unknown; needs debugging. Until fixed, users must start bot/watcher manually.

2. **Write `README.md`** — short public-facing intro for the repo (setup_guide.md is the detailed walkthrough; README should be the one-page entry point).

2. **End-to-end test on a colleague's machine** — validate the Phase 0 guard works and that setup.py runs clean on macOS. See `PLAN_option_a.md` Phase 3 checklist.

3. **Update the GitHub repo URL** in the user-ID guard message in `case_bot.py` (currently a placeholder `https://github.com/your-org/mend-support-toolkit`).

4. **Add SF API retry logic** — wrap `subprocess.run(["sf", ...])` calls in `utils.py` with a retry decorator (3 attempts, exponential backoff). Target: silent failures during expired SF sessions.

5. **Implement Jira TKA posting** — `tka` command already generates draft text. Add `post_tka_to_jira()` in `utils.py` using Jira REST API (`POST /rest/api/2/issue`).

6. **Wire up Google Drive archival** — `archive` command stub needs the `rclone copy` subprocess call and a post-archive Slack confirmation.

---

## How to Orient Yourself in a New Session

1. Read this file first, then `CLAUDE.md` for AI assistant context
2. Check `toolkit.log` tail for recent activity and any error patterns
3. Check `.watcher_state.json` for current polling state
4. The three main files are `case_bot.py`, `case_watcher.py`, and `utils.py` — the shared library is the right place for any new SF/Slack integration work
5. `config.example.json` documents all available configuration keys
