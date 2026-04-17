# Mend Case Watcher — Setup Guide

This guide walks you through setting up your own personal Mend Case Watcher bot from scratch. Once set up, you'll have a Slack bot you can DM to fetch Salesforce cases, download attachments, generate AI summaries, and more — all running locally on your own machine.

**Time required:** ~15–20 minutes (most of which is waiting for installs).

---

## What You'll End Up With

- A Slack bot that responds only to you
- A local folder structure for your SF cases (`My Cases/`, `Staging/`, `Other Cases/`)
- A background watcher that polls your SF queue every 5 minutes and alerts you in Slack
- AI-powered case summaries and TKA drafts via Claude

---

## Part 1 — Prerequisites

Before running setup, you need three things installed. The setup wizard handles everything else.

---

### 1a. Python 3.9 or later

**Check if you already have it:**
```
python --version
```
If it prints `Python 3.9.x` or higher, you're good. Skip to 1b.

**Install Python:**
- **Windows:** Download from https://www.python.org/downloads/ — tick **"Add Python to PATH"** during install
- **macOS:** Run `python3 --version` first. If missing, install via Homebrew: `brew install python` or from python.org

---

### 1b. Git

**Check:**
```
git --version
```

**Install:**
- **Windows:** https://git-scm.com/download/win (use all defaults)
- **macOS:** Run `git --version` — if not installed, macOS will prompt you to install developer tools automatically

---

### 1c. Claude CLI

The bot uses Claude CLI to generate case summaries and TKA drafts. Without it, the AI features won't work.

1. Go to https://claude.ai/download
2. Download and install **Claude Code**
3. Verify: `claude --version`

---

## Part 2 — Clone the Repo

```
git clone https://github.com/your-org/mend-support-toolkit
cd mend-support-toolkit
```

> **Windows users:** You can also run this in PowerShell or Git Bash. Either works.

---

## Part 3 — Run the Setup Wizard

```
python setup.py
```

The wizard walks you through 14 steps. Most are automatic — you just press Enter or answer yes/no. There are **two points where you'll need to pause and do something in your browser.** These are covered in detail below.

---

### Step 3 — Salesforce CLI

The wizard checks whether `sf` (Salesforce CLI) is installed.

- **If found:** it moves on automatically.
- **If not found:**
  - **Windows:** it offers to install via `winget` — say yes.
  - **macOS:** it offers to install via Homebrew (`brew install sfdxcli`) — say yes. If you don't have Homebrew, it tries `npm install -g @salesforce/cli` next. If neither works, it opens the download page at https://developer.salesforce.com/tools/salesforcecli.

After installing, re-run `python setup.py` — it will pick up from the top (already-done steps are fast).

---

### Step 5 — Claude CLI

The wizard checks for Claude CLI. If it's missing it will warn you and offer to open the download page. The rest of setup continues — you can add Claude CLI later, but AI features won't work until it's installed.

---

### Step 7 — Salesforce Authentication ⏸️ BROWSER ACTION

The wizard will ask: **"Open SF login in browser now?"** — say **yes**.

A browser window opens to the Salesforce login page. Log in with your Mend email and SSO (the same credentials you use at https://whitesourcesoftware.lightning.force.com).

Once logged in, come back to the terminal and press Enter.

> **If the browser doesn't open automatically:** run `sf org login web --set-default` in a new terminal window, then re-run `python setup.py`.

---

### Step 8 — Slack App Setup ⏸️ BROWSER ACTION

This is the biggest step. You need to create **your own personal Slack app** — it takes about 3 minutes.

**Why your own app?** Each person on the team runs a completely independent bot. Your bot only talks to you and uses your SF session. This also means no-one else can accidentally use your bot.

#### 8a. Create the app

1. Go to https://api.slack.com/apps
2. Click **"Create New App"**
3. Choose **"From a manifest"**
4. Select the **Mend** workspace
5. On the manifest screen, open the file `slack_app_manifest.yaml` from the repo folder you cloned. Copy its entire contents and paste it into the text box.
6. Click **Next** — you'll see a review screen. **Change the app name** from `Mend Case Watcher - YOUR NAME HERE` to something like `Mend Case Watcher - William`. This is important — if everyone leaves the default name, there will be multiple identically-named bots in the workspace and nobody will know which is theirs.
7. Click **Create**.

Your app is created. You should now be on the app's settings page.

#### 8b. Generate your App-Level Token (`xapp-...`)

1. In the left sidebar, click **"Basic Information"**
2. Scroll down to the **"App-Level Tokens"** section
3. Click **"Generate Token and Scopes"**
4. Give it any name (e.g. `watcher-token`)
5. Click **"Add Scope"** → select `connections:write`
6. Click **"Generate"**
7. **Copy the token** — it starts with `xapp-`. You'll need it in a moment.

#### 8c. Request permission and install the app (`xoxb-...`)

1. In the left sidebar, click **"Install App"**
2. You'll see one of two things:
   - **"Install to Workspace"** — click it, allow the permissions, and you're done
   - **"Request to Install"** — this means your Slack workspace requires admin approval first

> **If you see "Request to Install":** Click it to send an approval request to a Mend workspace admin. You'll get a Slack notification once it's approved — this is usually fast. Once approved, come back to https://api.slack.com/apps → your app → **Install App** → **Install to Workspace** → Allow.

3. After installing, you'll be shown your **Bot User OAuth Token** on the same page — it starts with `xoxb-`. **Copy it.**

#### 8d. Find your Slack Member ID

1. In Slack, click your **profile picture** (top right)
2. Click **"Profile"**
3. Click the **⋯ (More)** button
4. Click **"Copy member ID"**

It looks like `U09UNTT3FGT`.

#### 8e. Back in the terminal

The wizard is waiting. Paste in:
- The `xoxb-...` bot token
- The `xapp-...` app-level token
- Your Slack member ID

---

### Steps 9–11 — Automatic

- **Step 8** validates your SF field names (Domain / Sub-category) — fully automatic.
- **Step 9** creates your case folders. The default is `~/Documents/CASES/` — press Enter to accept, or type a different path.

---

### Step 12 — Background Tasks

The wizard attempts to register the watcher and bot as background tasks so they start automatically.

> **⚠️ Known issue (Windows):** The Task Scheduler registration currently does not work reliably. Until this is fixed, you'll need to start the bot and watcher manually — see Part 4 below.
>
> **macOS:** launchd registration should work. The bot and watcher will start immediately and restart automatically at login.

---

## Part 4 — Starting the Bot

### macOS
If Step 12 succeeded, everything is already running. Skip to Part 5.

To check: `launchctl list | grep mend`

To start manually if needed:
```
python case_bot.py &
python case_watcher.py &
```

### Windows (manual start required for now)

Open **two separate terminal windows** in the repo folder:

**Terminal 1 — the bot (keep this open):**
```
python case_bot.py
```
You should see: `Case Bot connected — listening for commands`

**Terminal 2 — run the watcher once manually:**
```
python case_watcher.py
```
The watcher exits after one poll cycle. Run it whenever you want to sync, or set up a Task Scheduler entry manually (Task Scheduler → Create Basic Task → Daily, repeat every 5 minutes).

> **Tip for Windows:** To keep the bot running after you close the terminal, you can use `pythonw case_bot.py` which runs it without a visible window, or set it up via Task Scheduler manually.

---

## Part 5 — Verify It Works

In Slack, find your new bot:
1. Click the **+** next to "Direct messages"
2. Search for the name you gave your app (e.g. "Mend Case Watcher - Your Name")
3. Send it a message: `help`

You should get back a list of all available commands within a few seconds.

**Try a real command:**
```
case 00166255
```
The bot will fetch the case from Salesforce, download any attachments, create a local folder, and reply with a summary.

---

## Part 6 — Command Reference

| Command | What it does |
|---------|-------------|
| `help` | Show all commands |
| `case 00XXXXXX` | Fetch a case, download attachments, generate summary |
| `staging` | Show all cases in the SF staging queue |
| `claim 00XXXXXX` | Take ownership; AI suggests Domain/Sub-category |
| `summarize 00XXXXXX` | Regenerate the AI summary for an existing case |
| `tka 00XXXXXX` | Draft a TKA Jira ticket using AI |
| `logs 00XXXXXX` | List RESOURCES/ files with token sizes |
| `move 00XXXXXX <filename>` | Move a file to large_files/ (exclude from AI context) |
| `move 00XXXXXX all` | Move all oversized files to large_files/ |
| `digest` | Show all your open cases with SLA status |
| `watch` | List cases currently being watched |
| `unwatch 00XXXXXX` | Stop watching a case |
| `reauth` | Re-authenticate with Salesforce |
| `config show` | Show your current config |
| `config <key> <value>` | Update a config value |
| `setup` | Check prerequisites and config status |

---

## Troubleshooting

**Bot doesn't respond to my messages**
- Make sure `case_bot.py` is running (you should see `Case Bot connected` in the terminal)
- Double-check your `slack_user_id` in `config.json` matches your actual Slack member ID
- Check the bot's App Home in Slack — make sure "Allow users to send messages" is enabled (App Home tab in your app settings)

**"SF session expired" errors**
- Run `python setup.py` again — it will re-run the SF login step
- Or run `sf org login web --set-default` directly in your terminal

**"Could not find case" errors**
- Your SF session may have expired — see above
- Check that you're using the case number format `00XXXXXX` (8 digits with leading zeros)

**Claude CLI errors / AI features not working**
- Run `claude --version` to check it's installed
- Run `claude -p "hello"` to check it's authenticated
- If not authenticated, run `claude` and follow the login steps

**App installation needs admin approval (Slack)**
- Contact your Mend Slack workspace admin and ask them to approve your app
- The app only needs `chat:write`, `im:write`, and `users:read` — low-risk scopes

**setup.py crashes on Step X**
- Re-run `python setup.py` — it loads your existing `config.json` so you won't lose progress
- Check `toolkit.log` in the repo folder for error details

---

## Files Created by Setup

| File/Folder | What it is |
|-------------|-----------|
| `config.json` | Your personal config (tokens, paths, settings) — **never commit this** |
| `~/Documents/CASES/My Cases/` | Cases assigned to you |
| `~/Documents/CASES/Staging/` | Unassigned staging queue cases |
| `~/Documents/CASES/Other Cases/` | Cases you looked up that belong to someone else |
| `toolkit.log` | Audit log of all bot and watcher activity |
| `.watcher_state.json` | Watcher state (last poll time, downloaded files) |
