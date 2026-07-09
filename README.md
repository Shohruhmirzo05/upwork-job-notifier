# Upwork → Telegram Job Notifier (serverless, GitHub Actions)

Pushes new Upwork jobs matching your keywords to Telegram, running 24/7 in the cloud
on GitHub Actions — **your computer can be off**. Cost: **$0** (free Actions minutes +
free Webshare proxy tier if needed). No AI, no paid APIs — filtering is 100% local.

## How it works
Every ~10 min, a GitHub Actions cron:
1. Gets a *visitor* GraphQL token from upwork.com (Chrome TLS impersonation via curl_cffi).
2. Queries Upwork's **public** job search API, sorted by most recent.
3. Scores each job with a **local weighted-keyword engine** you control in `filters.json`.
4. Dedupes against `seen.json` (committed back to the repo each run).
5. Sends each new job that clears your score threshold to Telegram — best matches first,
   each showing its ⭐ score and which of your keywords hit.

It **never logs in as you** — only public listings — so your Upwork account is not
in the loop. Worst case is a proxy IP getting blocked, not your profile flagged.
Still a ToS grey area; keep the interval sane and treat it as personal tooling.

---

## Filtering — the `filters.json` file

This one file controls everything; edit it and commit — no code changes. It scales to
hundreds of keywords without touching GitHub Variables.

```json
{
  "require_any": ["swiftui", "ios", "flutter", "ai agent", "claude"],
  "exclude":     ["wordpress", "php", "data entry"],
  "boost":       {"swiftui": 4, "claude": 4, "long-term": 2},
  "title_multiplier": 2,
  "min_score": 2,
  "word_boundary": true
}
```

| Key | What it does |
|-----|--------------|
| `require_any` | Job must match **at least one** of these, or it's dropped. Empty `[]` = allow everything. |
| `exclude` | Job is **rejected** if it matches **any** of these (checked first, wins over everything). |
| `boost` | `term: weight` — adds `weight` to the job's score per matched term. Higher-weight terms rank jobs higher and push them to the top of your notifications. |
| `title_multiplier` | A match in the **job title** counts this many times vs. a match in the description (default `2`). |
| `min_score` | Jobs scoring below this are dropped. Raise it to cut noise, lower it to see more. |
| `word_boundary` | `true` → whole-word matching, so `ios` doesn't match "k**ios**k" and `java` doesn't match "**java**script". Symbols like `c++`, `c#`, `node.js` still match correctly. |

**How scoring works:** each required-keyword hit adds 1 (×`title_multiplier` if in the title),
then each `boost` term adds its weight (also ×multiplier in the title). Example: a job titled
"SwiftUI iOS app, long-term" scores ~22; a "Kiosk data entry" job scores 0 and is dropped.

> If `filters.json` is missing, it falls back to the `KEYWORDS` / `EXCLUDE_KEYWORDS`
> GitHub Variables (simple comma lists) so nothing breaks.

---

## Setup (≈10 min)

The private repo and files are already set up. Remaining steps:

### 1. Telegram bot + chat ID
1. Bot already created: **@upwork_notificationsbot**.
2. **Get your chat ID:** open Telegram → message the bot once (tap **Start** / send "hi") →
   open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[0].message.chat.id`
   (or message **@userinfobot**).

### 2. Add GitHub Secrets and Variables
Repo → **Settings → Secrets and variables → Actions**.

**Secrets** (encrypted):
| Name | Value |
|------|-------|
| `TELEGRAM_BOT_TOKEN` | your BotFather token |
| `TELEGRAM_CHAT_ID` | your chat ID |
| `WEBSHARE_URL` | Webshare download link (only if using a proxy) |

**Variables** (plain, under the *Variables* tab — optional, all have defaults):
| Name | Example | Default |
|------|---------|---------|
| `MAX_PAGES` | `2` | 2 (newest 100 jobs) |
| `MAX_NOTIFS` | `20` | 20 (cap per run) |

### 3. (Optional) Free proxy — only if direct is blocked
GitHub's IPs may get Cloudflare-blocked. If your first run fails on token/fetch:
1. Sign up free at **webshare.io** (free tier: 10 proxies, 1 GB/month — plenty).
2. Dashboard → **Proxy → List → Download**, copy the **download link**.
3. Add it as the `WEBSHARE_URL` secret.

### 4. Turn it on
Repo → **Actions** tab → enable workflows → **Upwork Job Notifier** → **Run workflow**.
First run seeds the baseline and sends one "✅ armed" message. After that you get a ping
per new matching job.

---

## Tuning
- **Change what matches:** edit `filters.json` (add/remove keywords, adjust weights, raise
  `min_score` to cut noise) and commit — that's the whole loop.
- **Faster cadence:** change cron to `*/5 * * * *` (GitHub still throttles; ~5-15 min real).
- **More jobs scanned:** raise `MAX_PAGES` (each page = 50 newest jobs).
- **Too many pings:** lower `MAX_NOTIFS` or raise `min_score` in `filters.json`.

## Local test
```bash
cp .env.example .env      # fill in your values
pip install -r requirements.txt
set -a && source .env && set +a
python notifier.py
```

## Troubleshooting
- **No `visitor_gql_token` cookie / 403:** direct IP blocked → add the Webshare proxy.
- **401 from GraphQL:** token expired mid-run; next cron run refreshes it automatically.
- **No jobs match:** loosen `require_any`, lower `min_score`, or clear `exclude` to test.
- **seen.json commit noise:** it commits when there are new jobs. Harmless; if you dislike
  the history, point the workflow at a dedicated `state` branch or use `actions/cache`.
- **Schema drift:** if Upwork changes the GraphQL schema, `GRAPHQL_QUERY` in `notifier.py`
  is the one place to update.

## Credit
GraphQL + visitor-token method adapted from
[asaniczka/Upwork-Job-Scraper](https://github.com/asaniczka/Upwork-Job-Scraper).
