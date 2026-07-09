# Upwork → Telegram Job Notifier (serverless, GitHub Actions)

Pushes new Upwork jobs matching your keywords to Telegram, running 24/7 in the cloud
on GitHub Actions — **your computer can be off**. Cost: **$0** (free Actions minutes +
free Webshare proxy tier if needed).

## How it works
Every ~10 min, a GitHub Actions cron:
1. Gets a *visitor* GraphQL token from upwork.com (Chrome TLS impersonation via curl_cffi).
2. Queries Upwork's **public** job search API, sorted by most recent.
3. Filters by your `KEYWORDS` / `EXCLUDE_KEYWORDS`.
4. Dedupes against `seen.json` (committed back to the repo each run).
5. *(optional)* Has **Claude score each new job 1-10** against your profile and drops anything below `MIN_SCORE`.
6. Sends each surviving job to your Telegram chat (with its ⭐ score + one-line reason).

It **never logs in as you** — only public listings — so your Upwork account is not
in the loop. Worst case is a proxy IP getting blocked, not your profile flagged.
Still a ToS grey area; keep the interval sane and treat it as personal tooling.

---

## Setup (≈10 min)

### 1. Make a private GitHub repo
Create a repo and add these files (keep `seen.json` out — the workflow creates it):
```
notifier.py
requirements.txt
.github/workflows/notifier.yml
```

### 2. Create a Telegram bot
1. In Telegram, message **@BotFather** → `/newbot` → follow prompts.
2. Copy the **bot token** (looks like `123456:ABC...`).
3. Get your **chat ID**: message your new bot once (say "hi"), then open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and read
   `result[0].message.chat.id`. (Or message **@userinfobot** for your ID.)

### 3. (Optional) Free proxy — only if direct is blocked
GitHub's IPs may get Cloudflare-blocked. If your first run fails on token/fetch:
1. Sign up free at **webshare.io** (free tier: 10 proxies, 1 GB/month — plenty).
2. Dashboard → **Proxy → List → Download**, copy the **download link** (returns lines
   `host:port:user:pass`).
3. Add it as the `WEBSHARE_URL` secret below.
Skip this entirely if direct works.

### 4. Add GitHub Secrets and Variables
Repo → **Settings → Secrets and variables → Actions**.

**Secrets** (encrypted):
| Name | Value |
|------|-------|
| `TELEGRAM_BOT_TOKEN` | your BotFather token |
| `TELEGRAM_CHAT_ID` | your chat ID |
| `WEBSHARE_URL` | Webshare download link (only if using a proxy) |
| `ANTHROPIC_API_KEY` | Claude API key (only if using AI scoring) |

**Variables** (plain, under the *Variables* tab):
| Name | Example |
|------|---------|
| `KEYWORDS` | `swiftui,ios,swift,flutter,ai agent,openai,claude,supabase` |
| `EXCLUDE_KEYWORDS` | `wordpress,php,data entry` |
| `MAX_PAGES` | `2` |
| `MIN_SCORE` | `7` (only used if AI scoring is on) |
| `SCORE_MODEL` | `claude-haiku-4-5-20251001` (optional) |
| `PROFILE` | one line describing you, e.g. `Senior iOS + AI dev; SwiftUI, Flutter, LLM agents; wants real budgets` |

### AI scoring (optional but recommended)
Keyword filtering is broad — it lets through jobs that *mention* your stack but aren't
a real fit. With scoring on, Claude reads each new job and rates it **1-10** against your
`PROFILE`; only jobs `≥ MIN_SCORE` ping you, and each ping shows its ⭐ score + a one-line
reason. It runs **one batched call per cron run** over just the new jobs, so on Haiku it's
fractions of a cent a day. To enable: add the `ANTHROPIC_API_KEY` secret and (optionally)
the `MIN_SCORE` / `PROFILE` variables. Leave the key unset to keep pure keyword mode.

### 5. Turn it on
Repo → **Actions** tab → enable workflows → open **Upwork Job Notifier** → **Run workflow**
to test immediately. First run seeds the baseline and sends one "✅ armed" message.
After that, you get a ping per new matching job.

---

## Tuning
- **Faster:** change cron to `*/5 * * * *` (GitHub still throttles; ~5-15 min real).
- **Broader/narrower:** edit the `KEYWORDS` / `EXCLUDE_KEYWORDS` variables — no code change.
- **More jobs scanned:** raise `MAX_PAGES` (each page = 50 newest jobs).
- **Too many pings:** `MAX_NOTIFS` (env, default 15) caps notifications per run.

## Local test
```bash
cp .env.example .env      # fill in your values
pip install -r requirements.txt
set -a && source .env && set +a
python notifier.py
```

## Troubleshooting
- **No `visitor_gql_token` cookie / 403:** direct IP blocked → add the Webshare proxy (step 3).
- **401 from GraphQL:** token expired mid-run; next cron run refreshes it automatically.
- **No jobs match:** loosen `KEYWORDS`, or check with `MAX_PAGES=3` and no exclude list.
- **seen.json commit noise:** it commits every run there are new jobs. If you dislike the
  history, point the workflow at a dedicated `state` branch, or swap to `actions/cache`
  (less reliable for dedup). History noise is harmless otherwise.
- **Selectors/schema drift:** if Upwork changes the GraphQL schema, the `GRAPHQL_QUERY`
  in `notifier.py` is the one place to update.

## Credit
GraphQL + visitor-token method adapted from
[asaniczka/Upwork-Job-Scraper](https://github.com/asaniczka/Upwork-Job-Scraper).
