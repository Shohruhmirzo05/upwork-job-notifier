# Upwork → Telegram Job Notifier (serverless, GitHub Actions)

Pushes new Upwork jobs matching your profile to Telegram, 24/7 in the cloud on GitHub
Actions — **your computer can be off**. Cost: **$0** (public repo = unlimited Actions
minutes, no AI, no paid APIs). All filtering is deterministic keyword scoring you control.

## How it works
The workflow runs continuously (each run loops internally, checking every ~90s; runs chain
back-to-back), so you're pinged **~1–2 min after a matching job is posted**. Each check:
1. Gets a *visitor* GraphQL token from upwork.com (Chrome TLS impersonation via curl_cffi),
   **cached ~25 min** and reused across checks so the (403-prone) homepage is hit rarely.
2. Runs **multiple scoped searches** (the `search_queries` in `filters.json`), newest-first,
   and **merges + dedupes** the results by job id — deep niche coverage, not a thin slice
   of the newest *global* jobs.
3. **Scores** each job with a local weighted-keyword engine (`filters.json`).
4. Dedupes against `seen.json`, which persists in the **GitHub Actions cache** (no repo
   commits — history stays clean; if the cache is ever lost it just re-seeds silently).
5. Skips anything posted more than `MAX_AGE_HOURS` (24h) ago.
6. Sends each new job scoring ≥ `min_score` to Telegram — tagged **🔥 HOT / 🟢 GOOD / 🟡 MAYBE**,
   best score first, showing which keywords hit and how long ago it was posted.
7. Mirrors every delivered job, generated proposal, hook style, and screening answer to the
   private [Proposal Radar](https://upworkproposals.fera-tech.com) tracker. Tracker failures
   are best-effort and never interrupt Telegram notifications.

It **never logs in as you** — only public listings — so your Upwork account is not in the
loop. Worst case is a proxy IP getting blocked, not your profile flagged. Still a ToS grey
area; keep the interval sane and treat it as personal tooling.

---

## Why scoped searches (the key upgrade)

Fetching only the newest *global* jobs is shallow — on busy Upwork the newest 100 jobs span
just a few minutes, so niche postings scroll past before the next run sees them. Instead we
run ~17 targeted searches (`swift`, `ios app store testflight`, `flutter dart firebase`,
`arkit realitykit lidar`, …), each returning the newest 50 of *that* niche, then merge and
dedupe. One run currently surfaces ~430 unique relevant jobs — nothing in your niche slips
through between runs.

---

## Filtering — the `filters.json` file

One file controls everything; edit and commit, no code changes. Scales to hundreds of terms.

```jsonc
{
  "search_queries": ["swift", "ios app store testflight app review", "flutter dart firebase supabase", ...],
  "exclude":     ["data entry", "wix", "shopify store", ...],   // reject if ANY match (checked first)
  "require_any": ["ios", "swift", "flutter", "openai", ...],    // job must match >=1, else dropped (a GATE)
  "boost":       {"swiftui": 7, "revenuecat": 7, "arkit": 8, ...}, // score = sum of matched weights
  "modifiers": {
    "title_multiplier": 2,   // matches in the TITLE count double
    "min_score": 6,          // drop anything below this
    "good_min": 30,          // score >= this -> 🟢 GOOD
    "hot_min": 60,           // score >= this -> 🔥 HOT   (6..29 -> 🟡 MAYBE)
    "word_boundary": true    // "ios" != "kiosk", "java" != "javascript"; c++/node.js still match
  }
}
```

**Scoring model:** `require_any` is a gate (must match ≥1, adds no points). The score is the
sum of matched **`boost`** weights, with title matches multiplied by `title_multiplier`.
`exclude` wins over everything. Tiers are calibrated to the real score distribution
(median ≈ 33, top jobs 100–180), so 🔥 HOT genuinely means "drop everything and apply".

> Not too narrow by design: `php`, `laravel`, `backend`, `android`, `node` are **not** hard-
> excluded, because good mobile jobs often mention them (e.g. a native iOS app with a PHP
> backend). Only clearly-irrelevant work (data entry, WordPress-only, Shopify stores, crypto,
> etc.) is excluded.

If `filters.json` is missing, it falls back to the `KEYWORDS` / `EXCLUDE_KEYWORDS` GitHub
Variables (simple comma lists).

---

## Setup — already done

- Private repo, files, and workflow: **live**.
- Bot: **@upwork_notificationsbot** → your chat. Secrets `TELEGRAM_BOT_TOKEN` +
  `TELEGRAM_CHAT_ID`: **set**.
- Baseline seeded. It's running on the ~10-min cron now.
- Private tracker: **https://upworkproposals.fera-tech.com** on Cloudflare Workers + D1.
  Its login lasts 30 days; the generated password is stored only in the local ignored
  `.tracker-credentials` file and encrypted Cloudflare Worker secrets.

---

## Proposal Radar tracker

`tracker/` is a mobile-first application pipeline running entirely on Cloudflare's free tier.

- **Job inbox:** every matching job actually sent to Telegram, with compact cards and full
  details in a tap-open drawer/bottom sheet.
- **Applications:** generating a proposal moves the job here as **Likely applied**. Confirm it
  with one tap, or mark **Didn't apply** so it never pollutes conversion stats.
- **Pipeline:** Applied → Viewed → Replied → Interview → Won/Lost, with an immutable event
  history, notes, and labels.
- **Search:** title, description, Upwork cipher/link, proposal, or labels. Pasting the complete
  Telegram notification resolves the embedded Upwork job ID directly.
- **Proposal record:** exact submitted draft, hook family, and screening answers remain attached
  to the job for later client conversations.
- **Performance:** confirmed-only funnel rates and results grouped by proof-led, diagnostic,
  plan-led, and outcome-led openings. Small-sample guidance prevents premature conclusions.
- **Security:** single-admin password, signed `HttpOnly`/`Secure`/`SameSite=Strict` 30-day cookie,
  separate notifier ingestion token, same-origin API, CSP, and no public tracker data.

Cloudflare resources:

- Worker/custom domain: `upwork-proposals` / `upworkproposals.fera-tech.com`
- D1 database: `upwork-proposals` (schema migrations in `tracker/migrations/`)
- Worker secrets: `ADMIN_PASSWORD`, `SESSION_SECRET`, `INGEST_TOKEN`

Local verification/deploy:

```bash
cd tracker
npm install
npm run typecheck
npm run build
npx wrangler d1 migrations apply upwork-proposals --remote
npm run deploy
```

### Optional GitHub Variables (all have sane defaults)
Repo → **Settings → Secrets and variables → Actions → Variables**:
| Name | Meaning | Default |
|------|---------|---------|
| `MAX_NOTIFS` | max pings per check (best-scored first) | 25 |
| `MAX_AGE_HOURS` | never notify jobs older than this | 24 |
| `JOBS_PER_QUERY` | newest jobs fetched per search lane | 50 |
| `CHECK_INTERVAL` | seconds between checks inside a run | 90 |
| `LOOP_SECONDS` | how long each run loops before it exits | 630 |

### Optional proxy (only if GitHub's IP gets blocked)
If a run fails on the token/fetch: sign up free at **webshare.io**, copy the proxy-list
**download link**, add it as the `WEBSHARE_URL` secret. (Direct currently works, so skip it.)

---

## Tuning
- **Change what matches / ranking:** edit `filters.json` (keywords, weights, `search_queries`)
  and commit — that's the whole loop.
- **Fewer / more pings:** raise `min_score` (or `good_min`/`hot_min`) to cut noise; lower to
  see more. Or trim/add `search_queries`.
- **Faster cadence:** change cron to `*/5 * * * *` (GitHub still throttles; ~5–15 min real).

## Local test
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in your values
set -a && source .env && set +a
python notifier.py
```

## Troubleshooting
- **No `visitor_gql_token` cookie / 403:** direct IP blocked → add the Webshare proxy secret.
- **401 from GraphQL:** token expired mid-run; the next cron run refreshes it automatically.
- **Too many / too few pings:** adjust `min_score` and `search_queries` in `filters.json`.
- **Schema drift:** if Upwork changes the GraphQL schema, `GRAPHQL_QUERY` in `notifier.py`
  is the one place to update.

## Credit
GraphQL + visitor-token method adapted from
[asaniczka/Upwork-Job-Scraper](https://github.com/asaniczka/Upwork-Job-Scraper).
