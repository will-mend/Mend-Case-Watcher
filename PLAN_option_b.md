# Option B — Multi-Tenant Shared Bot: Action Plan

_Created: 2026-04-17_

## Goal

One Slack bot instance runs centrally (on a single host machine). Any Mend support engineer can DM it and get their own fully isolated experience — their SF cases, their own local folders, their own summaries. Colleagues need zero local setup beyond a one-time Slack registration flow.

---

## The Core Problem to Solve

Currently the bot is single-tenant:
- SF queries run using whoever's `sf` CLI session is active on the host machine
- Output paths are hardcoded to the host user's filesystem
- There's no concept of "who is asking"

To make it multi-tenant, every command must carry an identity, and the bot must use that identity to:
1. Authenticate to Salesforce as that user
2. Write files to that user's correct output directory

---

## Architecture (Target State)

```
Slack (shared bot)
      │  DM from any colleague
      ▼
case_bot.py  ←── user_registry.json  (Slack user ID → per-user config)
      │
      ├── SF OAuth token for that user  (stored in user_registry.json)
      │         ↓
      │    SF REST API calls (bypasses sf CLI entirely — no local session needed)
      │
      └── Per-user output path
                ↓
           C:\Users\<their_name>\...\CASES\  (or a shared drive)
```

Key shift: **replace `sf` CLI subprocess calls with direct SF REST API calls**, storing a refresh token per user. The CLI was convenient for a single user but doesn't scale to multi-user.

---

## Phases

---

### Phase 0 — Immediate Guard (1–2 hours)
**Goal:** Stop the bleeding. Prevent colleagues from accidentally using your SF session right now, before any architecture work begins.

**Changes:**
- In `case_bot.py`, at the top of every command handler, check the Slack `user_id` of the sender
- If the user is not registered (not in `user_registry.json`), respond:
  > "Hi! You're not registered yet. Send `register` to get set up."
- If the user IS the host user (your Slack ID), behaviour is unchanged

**Files:** `case_bot.py`  
**Effort:** Small — one guard function called at the top of each handler

---

### Phase 1 — User Registry (half day)
**Goal:** A persistent per-user config store that the bot reads on every command.

**New file: `user_registry.json`**
```json
{
  "U05XXXXXXX": {
    "slack_user_id": "U05XXXXXXX",
    "name": "William Forster",
    "sf_user_email": "william.forster@mend.io",
    "sf_access_token": "...",
    "sf_refresh_token": "...",
    "sf_instance_url": "https://whitesourcesoftware.lightning.force.com",
    "cases_root": "C:\\Users\\WilliamForster\\OneDrive - Mend.io\\Documents\\CASES_2",
    "registered_at": "2026-04-17T00:00:00Z"
  }
}
```

**New utility functions in `utils.py`:**
- `load_user_registry()` — reads `user_registry.json`
- `get_user_config(slack_user_id)` — returns that user's config dict or None
- `save_user_config(slack_user_id, config)` — writes back to registry

**Files:** `utils.py`, new `user_registry.json` (gitignored — contains tokens)  
**Effort:** Small

---

### Phase 2 — Salesforce OAuth Per User (1–2 days)
**Goal:** Each user authenticates to SF once via OAuth. The bot stores their refresh token and uses it for all subsequent requests, with no dependency on local `sf` CLI.

**Why this is necessary:** The `sf` CLI uses a locally stored session tied to the machine it's running on. There's no way to use another person's `sf` CLI session from a central bot. We need direct REST API auth.

**Implementation:**

#### 2a. Salesforce Connected App
- Create a Connected App in your SF org (Settings → App Manager → New Connected App)
- Enable OAuth, set callback URL to `http://localhost:8888/oauth/callback` (or a hosted URL if available)
- Grant scopes: `api`, `refresh_token`, `offline_access`
- Store `client_id` and `client_secret` in `config.json`

#### 2b. Registration OAuth flow
When a user sends `register` to the bot:
1. Bot generates a Salesforce OAuth authorization URL with a `state` param tied to their Slack user ID
2. Bot DMs them a link: "Click here to connect your Salesforce account: [link]"
3. User clicks, logs into SF, grants access
4. SF redirects to the callback URL with an auth code
5. Bot exchanges the code for `access_token` + `refresh_token`
6. Bot stores both in `user_registry.json` keyed by Slack user ID
7. Bot asks for their `cases_root` path (DM prompt: "What's the full path to your cases folder?")
8. Bot confirms: "You're registered. Try `case 00166XXX`."

#### 2c. Token refresh middleware
- Before every SF API call, check if the `access_token` is still valid (or just always refresh)
- If expired, use `refresh_token` to get a new `access_token` via SF OAuth token endpoint
- Store the new `access_token` back to registry

**New functions in `utils.py`:**
- `sf_rest_get(user_config, path)` — authenticated GET using user's token, auto-refreshes
- `sf_rest_post(user_config, path, body)` — authenticated POST
- `refresh_sf_token(user_config)` — exchanges refresh token for new access token
- `sf_soql(user_config, query)` — replaces all `sf data query` subprocess calls
- `sf_get_attachments(user_config, case_id)` — replaces attachment subprocess calls

**Files:** `utils.py`, `case_bot.py`, `config.json` (add `sf_client_id`, `sf_client_secret`)  
**Effort:** Medium-large — this is the most technically involved phase

> **Note on OAuth callback:** If running the bot on a machine that isn't publicly reachable, a local HTTP server (Python's `http.server` on port 8888) can handle the callback during the registration flow. It only needs to be running during the auth step.

---

### Phase 3 — Per-User Routing in Bot (half day)
**Goal:** Every command handler uses the caller's user config instead of the global config.

**Pattern:** Every handler changes from:
```python
def handle_case(case_number):
    # uses global config, sf CLI, hardcoded paths
```
to:
```python
def handle_case(user_config, case_number):
    # uses user_config["sf_access_token"], user_config["cases_root"]
```

**Changes:**
- Pass `user_config` into every handler function
- Replace all `subprocess.run(["sf", ...])` SF calls with `sf_rest_*` functions from Phase 2
- Replace all hardcoded path references with `user_config["cases_root"]`
- `case_watcher.py` loops over all registered users and polls for each

**Files:** `case_bot.py`, `case_watcher.py`, `utils.py`  
**Effort:** Medium — mechanical but touches most of the codebase

---

### Phase 4 — Watcher Multi-User Support (half day)
**Goal:** `case_watcher.py` polls for all registered users, not just the host.

**Changes:**
- On each poll cycle, load `user_registry.json`
- For each registered user, run the existing poll logic using their SF token and their `cases_root`
- Send Slack alerts to the individual user (their Slack user ID), not a shared channel

**Files:** `case_watcher.py`, `utils.py`  
**Effort:** Small once Phase 3 is done

---

### Phase 5 — Onboarding UX Polish (half day)
**Goal:** The `register` flow feels smooth enough that a colleague can do it without help.

**Improvements:**
- `register` command triggers the OAuth link immediately with clear instructions
- After OAuth completes, bot auto-detects the user's SF email from the token and confirms it
- Bot prompts for `cases_root` path with a sensible default suggestion
- `whoami` command lets any user see their registered details
- `unregister` command removes their entry from the registry

**Files:** `case_bot.py`  
**Effort:** Small

---

### Phase 6 — Deployment Decision (separate discussion)
**Goal:** Decide where the central bot runs long-term.

**Options:**
| Option | Pros | Cons |
|--------|------|------|
| Your laptop (current) | Zero infra cost, already running | Goes offline when laptop is off/sleeping |
| A team member's always-on PC | Easy, no cost | Still a personal machine |
| Mend internal VM / server | Reliable, proper | Needs IT involvement, setup overhead |
| Cloud VM (Azure/AWS) | Reliable, scalable | Cost, more setup |

For a small team, a dedicated always-on machine (or a VM) is the right call once the multi-tenant logic works. The code itself won't change between options — just where `case_bot.py` runs.

---

## Summary of New/Changed Files

| File | Change |
|------|--------|
| `case_bot.py` | Add user guard, per-user routing, register/whoami commands |
| `case_watcher.py` | Loop over all users instead of single user |
| `utils.py` | Add SF REST API functions, user registry functions, token refresh |
| `user_registry.json` | New — gitignored, stores per-user SF tokens and paths |
| `config.json` | Add `sf_client_id`, `sf_client_secret` for Connected App |
| `config.example.json` | Document the new fields |
| `requirements.txt` | Likely no new dependencies (stdlib `urllib` handles OAuth) |

---

## Recommended Order of Work

1. **Phase 0** — Do this today. Stops the current problem immediately.
2. **Phase 1** — Lays the foundation for everything else.
3. **Phase 2** — The biggest chunk; do this as one focused session.
4. **Phase 3 + 4** — Can be done together once Phase 2 is solid.
5. **Phase 5** — Polish after it's functionally working.
6. **Phase 6** — Separate conversation about hosting.
