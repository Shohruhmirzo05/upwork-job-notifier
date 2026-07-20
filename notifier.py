#!/usr/bin/env python3
"""
Upwork -> Telegram recent-jobs notifier.

Runs statelessly (built for GitHub Actions cron). Each run:
  1. Grabs a visitor GraphQL token from upwork.com (curl_cffi Chrome TLS impersonation).
  2. Runs several SCOPED searches (search_queries in filters.json), newest-first, and
     merges + dedupes the results by job id. This beats scanning only the newest global
     jobs — you get deep coverage of your niche instead of a shallow slice of everything.
  3. Scores each job with a LOCAL, zero-cost weighted-keyword engine (filters.json).
  4. Dedupes against seen.json (committed back by the workflow).
  5. Sends each genuinely-new job scoring >= min_score to Telegram, tagged HOT/GOOD/MAYBE,
     best score first.

No AI, no API costs — all filtering is deterministic keyword matching you fully control.
First run seeds the baseline silently (so you don't get blasted).

Method credit: adapted from asaniczka/Upwork-Job-Scraper (public GraphQL + visitor
token). Never logs in as you — only public listings — so your account stays out of it.
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests

# ---------- config (via env / GitHub secrets) ----------
def _int_env(name, default):
    """Parse an int env var, treating unset OR empty-string as the default.
    (GitHub Actions passes an unset `vars.X` as "" rather than leaving it absent.)"""
    v = os.environ.get(name, "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        return default


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
WEBSHARE_URL = os.environ.get("WEBSHARE_URL", "").strip()  # optional; empty = go direct
# Dead-man's-switch: ping this URL on every successful run. If pings stop (workflow died),
# the external monitor (e.g. healthchecks.io) alerts you. Empty = disabled.
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "").strip()
MAX_PAGES = _int_env("MAX_PAGES", 2)          # only used if no search_queries
PAGE_SIZE = 50
JOBS_PER_QUERY = _int_env("JOBS_PER_QUERY", 50)  # newest N per search lane
MAX_NOTIFS = _int_env("MAX_NOTIFS", 25)       # cap per run to avoid flood
MAX_AGE_HOURS = _int_env("MAX_AGE_HOURS", 24)  # never notify jobs older than this (0 = off)
SEEN_TTL_DAYS = _int_env("SEEN_TTL_DAYS", 7)
SEEN_PATH = Path(os.environ.get("SEEN_PATH", "seen.json"))
FILTERS_PATH = Path(os.environ.get("FILTERS_PATH", "filters.json"))
TOKEN_PATH = Path(os.environ.get("TOKEN_PATH", ".token.json"))
TOKEN_TTL_SEC = _int_env("TOKEN_TTL_SEC", 1500)  # reuse a cached visitor token ~25 min

# ---- proposal drafting (optional; free Google Gemini). Empty key = disabled. ----
# Proposals are ON DEMAND: each job card gets a "Generate Proposal" button; a draft is
# only written (spending tokens) when you tap it. No key = the button is hidden.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
# Ordered fallback chain (comma-separated). Free-tier daily quota is PER MODEL, so if the
# best model is rate-limited we fall through to one that still has quota. Pro needs billing.
# Only the top-quality free model; when its daily quota is gone we go to paid OpenAI,
# not to weaker free models. (Comma-separate here to add more top models later.)
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODEL", "gemini-2.5-flash"
).split(",") if m.strip()]
# Paid OpenAI fallback, used ONLY when free Gemini is exhausted/unavailable.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5").strip()
# low reasoning: enough for proposal writing, and stops reasoning tokens eating the output
OPENAI_REASONING = os.environ.get("OPENAI_REASONING", "low").strip()
AI_ENABLED = bool(GEMINI_API_KEY or OPENAI_API_KEY)
_AI_FAILURES = []
PROFILE_PATH = Path(os.environ.get("PROFILE_PATH", "profile.md"))
PROMPT_PATH = Path(os.environ.get("PROMPT_PATH", "proposal_prompt.md"))

# ---- private proposal/application tracker (optional; failures never block Telegram) ----
TRACKER_API_URL = os.environ.get("TRACKER_API_URL", "").strip().rstrip("/")
TRACKER_API_TOKEN = os.environ.get("TRACKER_API_TOKEN", "").strip()

# ---- serve loop + button-tap handling ----
JOB_INTERVAL = _int_env("CHECK_INTERVAL", 90)   # seconds between job scans in serve mode
SERVE_SECONDS = _int_env("SERVE_SECONDS", 0)    # >0 -> run the serve loop this many seconds
STORE_PATH = Path(os.environ.get("STORE_PATH", "store.json"))
STORE_TTL_HOURS = _int_env("STORE_TTL_HOURS", 26)  # keep tapped-job details this long
AWAITING_TTL_SEC = _int_env("AWAITING_TTL_SEC", 1800)  # "send questions" mode stays armed this long

UPWORK_HOME = "https://www.upwork.com/"
GRAPHQL_URL = "https://www.upwork.com/api/graphql/v1"
TOKEN_COOKIE = "visitor_gql_token"

GRAPHQL_QUERY = """
query VisitorJobSearch($requestVariables: VisitorJobSearchV1Request!) {
  search {
    universalSearchNuxt {
      visitorJobSearchV1(request: $requestVariables) {
        paging { total offset count }
        results {
          id
          title
          description
          ontologySkills { prefLabel }
          jobTile {
            job {
              id
              ciphertext: cipherText
              jobType
              hourlyBudgetMax
              hourlyBudgetMin
              contractorTier
              publishTime
              fixedPriceAmount { amount }
            }
          }
        }
      }
    }
  }
}
"""

HEADERS_BASE = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.8",
    "Referer": "https://www.upwork.com/nx/search/jobs/?",
    "X-Upwork-Accept-Language": "en-US",
    "Content-Type": "application/json",
}


# =====================================================================
# LOCAL FILTER ENGINE  (no AI, no cost)
# =====================================================================
# See filters.json for the annotated config. In short:
#   exclude      -> reject the job if it matches ANY of these (checked first)
#   require_any  -> job must match >=1 of these, else dropped (a GATE, adds no points)
#   boost        -> term: weight; the score is the sum of matched boost weights,
#                   with title matches counted `title_multiplier` times
#   modifiers.min_score -> drop anything below it
# Env KEYWORDS / EXCLUDE_KEYWORDS remain a fallback if filters.json is missing.

DEFAULT_FILTERS = {
    "require_any": [k.strip().lower()
                    for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()],
    "exclude": [k.strip().lower()
                for k in os.environ.get("EXCLUDE_KEYWORDS", "").split(",") if k.strip()],
    "boost": {},
    "search_queries": [],
    "title_multiplier": 2,
    "min_score": 6,
    "hot_min": 60,      # score >= this -> 🔥 HOT
    "good_min": 30,     # score >= this -> 🟢 GOOD ; below -> 🟡 MAYBE (down to min_score)
    "word_boundary": True,
}


def _compile_term(term, word_boundary):
    """Compile one keyword to a regex. Word-boundary aware but tolerant of symbols
    like c++, c#, node.js, ci/cd, objective-c — so those still match sanely."""
    esc = re.escape(term.lower().strip())
    if word_boundary:
        # (?<![a-z0-9]) / (?![a-z0-9]) instead of \b so symbol-y terms still match.
        return re.compile(rf"(?<![a-z0-9]){esc}(?![a-z0-9])")
    return re.compile(esc)


def load_filters():
    """Read filters.json (falling back to env), returning a dict with pre-compiled
    regexes. Supports both nested `modifiers` and top-level modifier keys."""
    cfg = dict(DEFAULT_FILTERS)
    if FILTERS_PATH.exists():
        try:
            raw = json.loads(FILTERS_PATH.read_text())
            for key in ("require_any", "exclude", "boost", "search_queries"):
                if key in raw:
                    cfg[key] = raw[key]
            mods = raw.get("modifiers", {}) or {}
            for key in ("title_multiplier", "min_score", "hot_min", "good_min", "word_boundary"):
                if key in mods:
                    cfg[key] = mods[key]
                elif key in raw:            # tolerate top-level too
                    cfg[key] = raw[key]
        except Exception as e:
            print(f"[warn] filters.json unreadable ({e}); using env fallback", file=sys.stderr)

    wb = bool(cfg.get("word_boundary", True))
    cfg["title_multiplier"] = float(cfg.get("title_multiplier", 2))
    cfg["min_score"] = int(cfg.get("min_score", 6))
    cfg["hot_min"] = int(cfg.get("hot_min", 60))
    cfg["good_min"] = int(cfg.get("good_min", 30))
    cfg["_require"] = [(t, _compile_term(t, wb)) for t in cfg.get("require_any", []) if t]
    cfg["_exclude"] = [(t, _compile_term(t, wb)) for t in cfg.get("exclude", []) if t]
    cfg["_boost"] = [(t, float(w), _compile_term(t, wb))
                     for t, w in (cfg.get("boost", {}) or {}).items() if t]
    return cfg


def score_job(job, cfg):
    """Deterministic local score. Returns (passes: bool, score: int, matched: [terms]).
    require_any is a gate (no points); the score is the sum of matched boost weights,
    with title matches multiplied. Fails on exclude, no required match, or score < min."""
    title = job["title"].lower()
    body = " ".join([job["title"], job["description"], " ".join(job["skills"])]).lower()

    for _term, rx in cfg["_exclude"]:
        if rx.search(body):
            return (False, 0, [])

    if cfg["_require"] and not any(rx.search(body) for _t, rx in cfg["_require"]):
        return (False, 0, [])

    tmult = cfg["title_multiplier"]
    score = 0.0
    matched = []  # (term, weight) for display, ranked by weight
    for term, weight, rx in cfg["_boost"]:
        if rx.search(body):
            score += weight * (tmult if rx.search(title) else 1.0)
            matched.append((term, weight))

    score = int(round(score))
    matched.sort(key=lambda x: -x[1])
    terms = [t for t, _w in matched]
    if score < cfg["min_score"]:
        return (False, score, terms)
    return (True, score, terms)


def tier_for(score, cfg):
    if score >= cfg["hot_min"]:
        return ("HOT", "🔥")
    if score >= cfg["good_min"]:
        return ("GOOD", "🟢")
    return ("MAYBE", "🟡")


# ---------- proxy ----------
def get_proxy_dict():
    """Return a curl_cffi proxies dict from Webshare, or None to go direct."""
    if not WEBSHARE_URL:
        return None
    try:
        resp = requests.get(WEBSHARE_URL, timeout=15)
        resp.raise_for_status()
        lines = [l.strip() for l in resp.text.strip().splitlines() if l.strip()]
        host, port, user, pw = random.choice(lines).split(":", 3)
        url = f"http://{user}:{pw}@{host}:{port}"
        return {"http": url, "https": url}
    except Exception as e:
        print(f"[warn] proxy fetch failed ({e}); trying direct", file=sys.stderr)
        return None


# ---------- upwork ----------
# Rotate TLS fingerprints across retries — helps dodge intermittent Cloudflare 403s
# (e.g. from shared GitHub Actions IPs). Kept conservative to known curl_cffi targets.
IMPERSONATE = ["chrome", "chrome131", "chrome124", "chrome120", "safari17_0"]


def _read_cached_token():
    """Return a still-fresh cached token, or None. Persisting the token across loop
    iterations means we hit the 403-prone homepage ~once per run, not once per check."""
    try:
        d = json.loads(TOKEN_PATH.read_text())
        if d.get("token") and (time.time() - d.get("ts", 0)) < TOKEN_TTL_SEC:
            return d["token"]
    except Exception:
        pass
    return None


def _write_cached_token(token):
    try:
        TOKEN_PATH.write_text(json.dumps({"token": token, "ts": time.time()}))
    except Exception:
        pass


def get_token(proxy, tries=5, force=False):
    """Return a visitor GraphQL token — from disk cache if fresh, else fetched from
    upwork.com with backoff + rotating TLS fingerprints to ride out Cloudflare 403s."""
    if not force:
        cached = _read_cached_token()
        if cached:
            return cached
    last = None
    for i in range(tries):
        imp = IMPERSONATE[i % len(IMPERSONATE)]
        try:
            resp = requests.get(UPWORK_HOME, impersonate=imp, proxies=proxy, timeout=30)
            if resp.status_code == 200:
                token = resp.cookies.get(TOKEN_COOKIE)
                if token:
                    _write_cached_token(token)
                    return token
                last = RuntimeError(f"200 but no {TOKEN_COOKIE} cookie "
                                    f"({list(resp.cookies.keys())})")
            else:
                last = RuntimeError(f"home HTTP {resp.status_code}")
        except Exception as e:
            last = e
        if i < tries - 1:
            wait = 3 * (i + 1)
            print(f"[warn] token attempt {i+1}/{tries} failed ({last}); retrying in {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"token fetch failed after {tries} tries: {last}")


def fetch_page(token, proxy, offset, user_query="", count=PAGE_SIZE, tries=3):
    headers = {**HEADERS_BASE, "Authorization": f"Bearer {token}"}
    # highlight:False -> clean title/description text (no <em>/control-char markers)
    reqvars = {"sort": "recency", "highlight": False,
               "paging": {"offset": offset, "count": count}}
    if user_query:
        reqvars["userQuery"] = user_query
    payload = {"query": GRAPHQL_QUERY, "variables": {"requestVariables": reqvars}}
    last = None
    for i in range(tries):
        imp = IMPERSONATE[i % len(IMPERSONATE)]
        try:
            resp = requests.post(GRAPHQL_URL, headers=headers, json=payload,
                                 proxies=proxy, impersonate=imp, timeout=25)
            resp.raise_for_status()
            data = resp.json()
            return (data.get("data", {}).get("search", {}).get("universalSearchNuxt", {})
                    .get("visitorJobSearchV1", {}).get("results", []) or [])
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(2 * (i + 1))
    raise last


def parse_job(r):
    job = (r.get("jobTile") or {}).get("job") or {}
    cipher = job.get("ciphertext") or ""
    skills = [s.get("prefLabel", "") for s in (r.get("ontologySkills") or []) if s.get("prefLabel")]
    return {
        "cipher": cipher,
        "title": r.get("title") or "Untitled",
        "description": r.get("description") or "",
        "skills": skills,
        "job_type": job.get("jobType") or "",
        "hourly_min": job.get("hourlyBudgetMin"),
        "hourly_max": job.get("hourlyBudgetMax"),
        "fixed": (job.get("fixedPriceAmount") or {}).get("amount"),
        "tier": job.get("contractorTier") or "",
        "publish": job.get("publishTime") or "",
        "link": f"https://www.upwork.com/jobs/{cipher}" if cipher else UPWORK_HOME,
    }


def fetch_all_jobs(token, proxy, cfg):
    """Run each search_query newest-first, merge, and dedupe by job id.
    Falls back to newest-global pages if no search_queries are configured."""
    by_cipher = {}
    queries = [q for q in (cfg.get("search_queries") or []) if q]

    if queries:
        for q in queries:
            try:
                results = fetch_page(token, proxy, 0, user_query=q, count=JOBS_PER_QUERY)
                for r in results:
                    j = parse_job(r)
                    if j["cipher"]:
                        by_cipher.setdefault(j["cipher"], j)
                print(f"[info] lane {q!r}: {len(results)} results")
            except Exception as e:
                print(f"[warn] lane {q!r} failed: {e}", file=sys.stderr)
            time.sleep(0.3)  # be gentle on Upwork
    else:
        for offset in range(0, MAX_PAGES * PAGE_SIZE, PAGE_SIZE):
            try:
                for r in fetch_page(token, proxy, offset):
                    j = parse_job(r)
                    if j["cipher"]:
                        by_cipher.setdefault(j["cipher"], j)
            except Exception as e:
                print(f"[warn] page offset {offset} failed: {e}", file=sys.stderr)

    return list(by_cipher.values())


# ---------- telegram ----------
def fmt_budget(job):
    if job["job_type"] == "HOURLY":
        lo, hi = job["hourly_min"], job["hourly_max"]
        if lo or hi:
            return f"${int(float(lo or 0))}–{int(float(hi or 0))}/hr"
        return "Hourly"
    if job["fixed"]:
        return f"${int(float(job['fixed']))} fixed"
    return job["job_type"].title() or "—"


def clean_tier(t):
    """Upwork returns e.g. 'IntermediateLevel' -> 'Intermediate'."""
    if not t:
        return ""
    t = re.sub(r"level", "", t, flags=re.IGNORECASE).strip()
    return t.title()


def _parse_publish(publish):
    """Parse Upwork publishTime (ISO string or epoch-ms) to a tz-aware datetime, or None."""
    if not publish:
        return None
    try:
        if isinstance(publish, (int, float)) or str(publish).isdigit():
            return datetime.fromtimestamp(int(publish) / 1000, tz=timezone.utc)
        dt = datetime.fromisoformat(str(publish).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def age_hours(publish):
    """Hours since a job was posted, or None if the timestamp is unknown/unparseable."""
    dt = _parse_publish(publish)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def fmt_age(publish):
    """Human 'posted X ago'. Empty on failure."""
    dt = _parse_publish(publish)
    if dt is None:
        return ""
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_message(job, cfg):
    """Compact, glanceable card — 4 short lines, no description wall."""
    label, emoji = tier_for(job["score"], cfg)
    age = fmt_age(job.get("publish", ""))
    ctier = clean_tier(job["tier"])
    matched = ", ".join(job.get("matched", [])[:4])

    head = f"{emoji} <b>{label} · {job['score']}</b>"
    if age:
        head += f" · {age}"
    budget = f"💰 {esc(fmt_budget(job))}"
    if ctier:
        budget += f" · {esc(ctier)}"

    lines = [
        head,
        f"<b>{esc(job['title'][:100])}</b>",
        budget,
    ]
    if matched:
        lines.append(f"🎯 {esc(matched)}")
    # The Upwork link is an inline button now (see send()), not a body link.
    return "\n".join(lines)


def send(job, cfg):
    payload = {
        "chat_id": CHAT_ID, "text": build_message(job, cfg), "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    # Row of inline buttons: Open (external URL) + Generate Proposal (callback, if Gemini on).
    row = []
    if job.get("link"):
        row.append({"text": "🔗 Open on Upwork", "url": job["link"]})
    if AI_ENABLED and job.get("cipher"):
        row.append({"text": "📝 Generate Proposal", "callback_data": f"p:{job['cipher']}"[:64]})
    if row:
        payload["reply_markup"] = {"inline_keyboard": [row]}
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=15,
    )
    if r.status_code != 200:
        print(f"[warn] telegram {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return False
    return True


def _tracker_job(job):
    """Normalize notifier jobs for the private Cloudflare tracker."""
    return {
        "cipher": job.get("cipher", ""), "title": job.get("title", ""),
        "description": job.get("description", ""), "skills": job.get("skills", []),
        "matched": job.get("matched", []), "budget": fmt_budget(job),
        "link": job.get("link", ""), "publish_time": job.get("publish", ""),
        "score": job.get("score", 0), "tier": clean_tier(job.get("tier", "")),
    }


def tracker_ingest(event, job, **extra):
    """Best-effort event delivery. The notifier remains fully functional if tracker is down."""
    if not TRACKER_API_URL or not TRACKER_API_TOKEN or not job.get("cipher"):
        return
    payload = {"event": event, "job": _tracker_job(job), **extra}
    try:
        r = requests.post(
            f"{TRACKER_API_URL}/api/ingest", json=payload,
            headers={"Authorization": f"Bearer {TRACKER_API_TOKEN}"}, timeout=8,
        )
        if r.status_code >= 300:
            print(f"[warn] tracker {r.status_code}: {r.text[:160]}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] tracker delivery failed: {e}", file=sys.stderr)


def send_plain(text):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text}, timeout=15,
    )


def answer_callback(callback_id, text=""):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text[:200]}, timeout=15,
        )
    except Exception as e:
        print(f"[warn] answerCallback failed: {e}", file=sys.stderr)


def tg_reply(chat_id, reply_to_message_id, text, reply_markup=None, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json=payload, timeout=20)
        return (r.json().get("result") or {}).get("message_id")
    except Exception as e:
        print(f"[warn] tg_reply failed: {e}", file=sys.stderr)
        return None


def ping_healthcheck():
    """Signal 'I'm alive' to an external dead-man's-switch monitor. No-op if unset."""
    if not HEALTHCHECK_URL:
        return
    try:
        requests.get(HEALTHCHECK_URL, timeout=10)
    except Exception as e:
        print(f"[warn] healthcheck ping failed: {e}", file=sys.stderr)


# ---------- proposal drafting (free Gemini) ----------
def load_profile():
    try:
        return PROFILE_PATH.read_text().strip()
    except Exception:
        return ""


def load_prompt_template():
    try:
        return PROMPT_PATH.read_text()
    except Exception:
        return ""


def _fill_prompt(job, template):
    """Substitute the job's data into the proposal-brain template placeholders."""
    matched = ", ".join(job.get("matched", []) or [])
    score = job.get("score", "")
    subs = {
        "{{JOB_TITLE}}": job.get("title", ""),
        "{{JOB_DESCRIPTION}}": (job.get("description", "") or "")[:4000],
        "{{SKILLS}}": ", ".join((job.get("skills") or [])[:15]),
        "{{BUDGET}}": fmt_budget(job),
        "{{CLIENT_INFO}}": "(not provided by the job feed)",
        "{{SCORE_AND_MATCHES}}": f"internal score {score}; matched keywords: {matched}"
                                 if matched else f"internal score {score}",
        "{{QUESTIONS}}": "(none provided — return only the cover letter, no screening answers)",
    }
    out = template
    for k, v in subs.items():
        out = out.replace(k, str(v))
    return out


def _fallback_prompt(job):
    profile = load_profile()
    return (
        "Write a first-person Upwork proposal for the freelancer below. Direct, confident, "
        "hyphen bullets not asterisks, no emojis, no fabrication, ~180 words.\n\n"
        f"FREELANCER:\n{profile or '(a senior mobile & AI developer)'}\n\n"
        f"JOB\nTitle: {job['title']}\nBudget: {fmt_budget(job)}\n"
        f"Skills: {', '.join((job.get('skills') or [])[:10])}\n"
        f"Description: {(job.get('description') or '')[:1500]}\n"
    )


def _record_ai_failure(provider, kind, detail=""):
    """Keep safe, user-facing failure metadata without ever retaining credentials."""
    _AI_FAILURES.append({"provider": provider, "kind": kind, "detail": detail[:160]})


def _ai_failure_message(action="proposal"):
    """Explain the real provider failure instead of blaming Gemini for every error."""
    failures = list(reversed(_AI_FAILURES))
    openai = next((x for x in failures if x["provider"] == "OpenAI"), None)
    gemini = next((x for x in failures if x["provider"] == "Gemini"), None)
    prefix = f"⚠️ Couldn't generate {action}. "
    if openai:
        kind = openai["kind"]
        if kind == "insufficient_quota":
            return prefix + ("OpenAI rejected the request because this API project's billing "
                             "quota is unavailable. Check OpenAI Platform billing/limits, then retry.")
        if kind == "rate_limit":
            return prefix + "OpenAI is temporarily rate-limited. Wait a minute, then retry."
        if kind == "auth":
            return prefix + "OpenAI rejected the API key. Replace the OPENAI_API_KEY secret and restart the workflow."
        if kind == "access":
            return prefix + f"OpenAI cannot access model {OPENAI_MODEL}. Check OPENAI_MODEL and project access."
        return prefix + "OpenAI is temporarily unavailable. Tap the button to retry."
    if gemini:
        if gemini["kind"] == "quota":
            return prefix + "Gemini quota is exhausted and no working OpenAI fallback was available."
        if gemini["kind"] == "auth":
            return prefix + "Gemini rejected the API key and no working OpenAI fallback was available."
        return prefix + "Gemini is unavailable and no working OpenAI fallback was available."
    return prefix + "No AI provider returned a response. Tap the button to retry."


def _gemini_generate(prompt, json_mode=False, max_tokens=8192):
    """One Gemini generation, walking the model fallback chain: on 429 (daily quota) move to
    the next model; on 503 (overload) retry once then move on. thinking disabled so long
    outputs don't truncate. Key goes in a header (not the URL) so it can't leak via logs."""
    if not GEMINI_API_KEY:
        return None
    gen_cfg = {"temperature": 0.6, "maxOutputTokens": max_tokens,
               "thinkingConfig": {"thinkingBudget": 0}}
    if json_mode:
        gen_cfg["responseMimeType"] = "application/json"
    headers = {"x-goog-api-key": GEMINI_API_KEY}
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen_cfg}
    for model in GEMINI_MODELS:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent")
        for attempt in range(2):
            try:
                r = requests.post(url, json=body, headers=headers, timeout=90)
                if r.status_code == 200:
                    cands = r.json().get("candidates") or []
                    parts = (cands[0].get("content") or {}).get("parts") or [] if cands else []
                    text = "".join(p.get("text", "") for p in parts).strip()
                    if text:
                        return text
                    _record_ai_failure("Gemini", "empty", model)
                    break  # empty -> try next model
                if r.status_code == 429:  # daily quota for this model -> next model, no retry
                    _record_ai_failure("Gemini", "quota", model)
                    print(f"[warn] gemini {model}: quota exhausted (429); trying next model",
                          file=sys.stderr)
                    break
                if r.status_code in (401, 403):
                    _record_ai_failure("Gemini", "auth" if r.status_code == 401 else "access", model)
                else:
                    _record_ai_failure("Gemini", "unavailable", f"{model}: HTTP {r.status_code}")
                if r.status_code == 503 and attempt == 0:  # overloaded -> one quick retry
                    time.sleep(2)
                    continue
                print(f"[warn] gemini {model} HTTP {r.status_code}: {r.text[:150]}",
                      file=sys.stderr)
                break
            except Exception as e:  # timeouts, transient network
                _record_ai_failure("Gemini", "unavailable", type(e).__name__)
                print(f"[warn] gemini {model} error: {e}", file=sys.stderr)
                if attempt == 0:
                    time.sleep(2)
                    continue
                break
    return None


def _openai_generate(prompt, json_mode=False, max_tokens=8192):
    """Paid OpenAI (GPT-5.6) generation — used only as a fallback when Gemini is unavailable."""
    if not OPENAI_API_KEY:
        return None
    body = {"model": OPENAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": max_tokens}
    if OPENAI_REASONING:
        body["reasoning_effort"] = OPENAI_REASONING
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    for attempt in range(3):
        try:
            r = requests.post("https://api.openai.com/v1/chat/completions",
                              headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                                       "Content-Type": "application/json"},
                              json=body, timeout=90)
            if r.status_code == 200:
                choices = r.json().get("choices") or [{}]
                text = (choices[0].get("message", {}).get("content") or "").strip()
                if text:
                    return text
                _record_ai_failure("OpenAI", "empty")
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                return None

            try:
                error = r.json().get("error") or {}
            except Exception:
                error = {}
            code = str(error.get("code") or "")
            if r.status_code == 429:
                kind = "insufficient_quota" if code == "insufficient_quota" else "rate_limit"
                _record_ai_failure("OpenAI", kind, code)
                print(f"[warn] openai HTTP 429 ({kind})", file=sys.stderr)
                if kind == "rate_limit" and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                return None
            if r.status_code == 401:
                _record_ai_failure("OpenAI", "auth", code)
            elif r.status_code == 403:
                _record_ai_failure("OpenAI", "access", code)
            else:
                _record_ai_failure("OpenAI", "unavailable", f"HTTP {r.status_code}")
            print(f"[warn] openai HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
            if r.status_code >= 500 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception as e:
            _record_ai_failure("OpenAI", "unavailable", type(e).__name__)
            print(f"[warn] openai error: {e}", file=sys.stderr)
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return None
    return None


def _generate(prompt, json_mode=False, max_tokens=8192):
    """Free Gemini first; fall back to paid OpenAI only if Gemini is unavailable/exhausted."""
    _AI_FAILURES.clear()
    text = _gemini_generate(prompt, json_mode=json_mode, max_tokens=max_tokens)
    if text:
        return text
    if OPENAI_API_KEY:
        print("[info] Gemini unavailable — falling back to OpenAI", file=sys.stderr)
        text = _openai_generate(prompt, json_mode=json_mode, max_tokens=max_tokens)
        if text:
            print("[info] OpenAI fallback succeeded", file=sys.stderr)
        return text
    return None


def _clean_text(t):
    """Strip AI-isms so text reads like a human typed it: smart/curly quotes -> straight,
    em/en dashes -> comma/hyphen, ellipsis normalized, markdown (bold/italic/headings/backticks)
    removed, wrapping quotes stripped. Keeps plain hyphen bullets."""
    if not t:
        return t
    for c in "“”„‟″«»":   # curly/other double quotes
        t = t.replace(c, '"')
    for c in "‘’‚‛′":               # curly single quotes / apostrophes
        t = t.replace(c, "'")
    t = re.sub(r"\s*—\s*", ", ", t)                     # em dash -> ", "
    t = t.replace("–", "-").replace("−", "-")      # en dash / minus -> hyphen
    t = t.replace("…", "...")                           # ellipsis
    t = t.replace(" ", " ").replace(" ", " ").replace("​", "")
    t = re.sub(r"^\s*[\*•]\s+", "- ", t, flags=re.M)    # * or bullet char -> "- "
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)                   # **bold**
    t = re.sub(r"\*(.+?)\*", r"\1", t)                       # *italic*
    t = t.replace("`", "")
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)      # markdown headings
    t = re.sub(r"\s+,", ",", t)
    t = re.sub(r",\s*,", ", ", t)
    t = re.sub(r",\s*([.!?])", r"\1", t)                     # ", ." -> "."
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"' and t[1:-1].count('"') == 0:
        t = t[1:-1].strip()                                 # unwrap a fully-quoted answer
    return t


def generate_proposal(job):
    """Draft a proposal with the proposal-brain prompt. Returns raw model text or None."""
    template = load_prompt_template()
    use_json = bool(template)
    prompt = _fill_prompt(job, template) if template else _fallback_prompt(job)
    raw = _generate(prompt, json_mode=use_json)
    if not raw:
        return None
    for repair_attempt in range(2):
        failures = _proposal_hard_failures(raw)
        if not failures:
            return raw

        # Models do not follow long proposal briefs consistently. Validate every repaired
        # draft too, so a self-disqualifying or structurally weak rewrite never reaches Telegram.
        print(f"[info] proposal QA retry {repair_attempt + 1}: {', '.join(failures)}",
              file=sys.stderr)
        repair = (
            f"{prompt}\n\n"
            "A draft failed mandatory quality checks. Rewrite it from scratch, not sentence "
            "by sentence. Keep every factual and output-format rule from the original brief.\n"
            f"FAILED CHECKS: {'; '.join(failures)}\n"
            "HARD REPAIR RULES:\n"
            "- After 'Hi,' begin with the specific technical risk, first milestone, business outcome, "
            "or a named shipped proof. Never begin with 'I can', 'I understand', 'I would', 'I'm', "
            "'I am', or another self-introduction.\n"
            "- Use one close portfolio project by default and at most two.\n"
            "- Keep all portfolio links below the preview paragraph.\n"
            "- Keep a normal proposal between 140 and 210 words, and a complex proposal below 260.\n"
            "- Do not infer unverified features, tools, ownership, or specialist experience.\n"
            "- Never use self-disqualifying phrases such as 'I have not', 'I haven't', 'I don't "
            "have', 'I lack', 'no direct experience', or 'this would be new'. Lead with the closest "
            "verified production proof and frame any necessary distinction neutrally.\n"
            "- For an unverified framework or API, describe the verified adjacent architecture and "
            "the proposed implementation or validation plan. Do not inventory missing experience.\n"
            "- Use no more than one precise closing question.\n\n"
            f"REJECTED DRAFT:\n{raw or '(empty)'}"
        )
        raw = _generate(repair, json_mode=use_json)

    final_failures = _proposal_hard_failures(raw)
    if final_failures:
        print(f"[error] proposal rejected after QA: {', '.join(final_failures)}",
              file=sys.stderr)
        return None
    return raw


def _proposal_hard_failures(raw):
    """Return objective proposal failures that warrant one automatic rewrite."""
    cover = _extract_cover(raw)
    if not cover:
        return ["missing or malformed cover_letter"]

    failures = []
    if not re.match(r"^\s*Hi,", cover, re.I):
        failures.append("missing 'Hi,' greeting")
    body = re.sub(r"^\s*Hi,?\s*", "", cover, flags=re.I).lstrip()
    preview = body[:300]
    if re.match(r"^(?:I can\b|I understand\b|I would\b|I'm\b|I am\b|This project\b)",
                preview, re.I):
        failures.append("generic self-led preview")
    if "http" in preview.lower():
        failures.append("portfolio link appears in preview")

    word_count = len(re.findall(r"\b[\w'+.-]+\b", cover))
    if word_count > 300:
        failures.append(f"proposal is {word_count} words")

    names = (
        "Salom AI Business", "BandMate", "Launchcast", "CrisisPath", "Clove AI",
        "Goby AI", "PicTrans", "QuarCade", "Kowl", "Karly", "Fera Tech", "Salom AI",
    )
    remaining = cover.lower()
    projects = []
    for name in names:  # longest/compound name appears before Salom AI
        needle = name.lower()
        if needle in remaining:
            projects.append(name)
            remaining = remaining.replace(needle, " ")
    if len(projects) > 2:
        failures.append(f"uses {len(projects)} portfolio projects")

    self_disqualifying = (
        r"\bI have not\b", r"\bI haven't\b", r"\bI do not have\b", r"\bI don't have\b",
        r"\bI lack\b", r"\bI have never\b", r"\bI have not personally\b",
        r"\bI haven't personally\b", r"\bno direct experience\b",
        r"\b(?:this|that|it) would be (?:a )?new\b",
        r"\b(?:would|will) be (?:a )?new (?:integration|framework|tool|stack|platform)\b",
    )
    if any(re.search(pattern, cover, re.I) for pattern in self_disqualifying):
        failures.append("uses self-disqualifying capability-gap language")

    # Launchcast is verified as a live iOS publishing/notification product, not as a
    # maps case study. This catches the specific unsupported inference seen in live QA.
    if "launchcast" in cover.lower() and re.search(
            r"\b(map marker|map markers|map pin|map pins|mapping feature|custom map)\b",
            cover, re.I):
        failures.append("attributes an unverified maps feature to Launchcast")

    questions = [line for line in cover.splitlines() if line.strip().endswith("?")]
    if len(questions) > 1:
        failures.append(f"uses {len(questions)} closing questions")
    return failures


def generate_answers(job, questions):
    """Answer the client's screening questions. Returns a list of (question, answer) — one
    per distinct question — consistent with the proposal already sent. None on failure."""
    profile = load_profile()
    proposal = job.get("proposal") or "(proposal text not stored)"
    prompt = (
        "You answer Upwork screening questions for this freelancer, in his first-person voice. "
        "Base every answer on the PROPOSAL already sent for this job and his PROFILE below, so "
        "the answers stay consistent with the proposal. Never fabricate.\n\n"
        f"PROFILE:\n{profile}\n\n"
        f"PROPOSAL ALREADY SENT FOR THIS JOB (make the answers match it):\n{proposal}\n\n"
        f"JOB:\nTitle: {job.get('title', '')}\n"
        f"Description: {(job.get('description') or '')[:1200]}\n\n"
        f"CLIENT'S SCREENING QUESTIONS (there may be several in this text):\n{questions.strip()}\n\n"
        "Identify EACH distinct question and answer it separately.\n"
        "Return JSON only: {\"answers\":[{\"question\":\"...\",\"answer\":\"...\"}]}, one object "
        "per question, in the order asked.\n"
        "Upwork may show these answers before the cover letter, so every first sentence is premium "
        "preview space. Start with the direct answer or the strongest relevant proof. Never start "
        "with 'Great question', 'Sure', 'As mentioned', a generic self-introduction, or a restatement "
        "of the question. Each answer must be 2-4 concise sentences in first person. Include one "
        "relevant example, result, or implementation fact when useful. Reuse only verified projects, "
        "links, rate and claims from the proposal/profile, but do not copy the proposal opening or "
        "repeat the same proof mechanically across answers. Never volunteer self-disqualifying "
        "phrases such as 'I have not', 'I don't have', 'I lack', or 'this would be new'. If an exact "
        "requested tool is not verified, lead with the closest production proof, state the distinction "
        "neutrally, and explain the relevant implementation plan without inventing experience.\n"
        "WRITING STYLE — very important: write like a human typing naturally. Plain text only. "
        "Use straight apostrophes ('), never curly/smart quotes. NEVER use em-dashes or en-dashes "
        "(use a comma or a new sentence). No markdown, no bold, no asterisks, no emojis, no "
        "wrapping quotation marks. Paste-ready, no preamble.\n"
    )
    raw = _generate(prompt, json_mode=True, max_tokens=4096)
    if not raw:
        return None
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        arr = json.loads(m.group(0) if m else raw).get("answers") or []
        out = [(_clean_text((x.get("question") or "").strip()),
                _clean_text((x.get("answer") or "").strip()))
               for x in arr if (x.get("answer") or "").strip()]
        return out or None
    except Exception:
        # salvage: at least return the raw text as a single answer
        return [("", raw.strip())]


def _extract_cover(raw):
    """Pull the cover_letter out of the proposal JSON (for storing, to align answers)."""
    if not raw:
        return None
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        d = json.loads(m.group(0) if m else raw)
        if isinstance(d, dict):
            return _clean_text((d.get("cover_letter") or "").strip()) or None
    except Exception:
        pass
    salv = _salvage_field(raw, "cover_letter")
    return _clean_text(salv) if salv else None


def _extract_proposal_metadata(raw):
    """Return the tracked hook family and any model-prepared screening answers."""
    data = {}
    try:
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        parsed = json.loads(m.group(0) if m else raw)
        if isinstance(parsed, dict):
            data = parsed
    except Exception:
        pass
    hook = str(data.get("hook_type") or "").strip().lower()
    if hook not in {"proof-led", "diagnostic", "plan-led", "outcome-led"}:
        opening = re.sub(r"^\s*Hi,?\s*", "", _extract_cover(raw) or "", flags=re.I)
        opening = opening.split("\n\n", 1)[0].lower()
        if re.search(r"\b(shipped|built|published|live|app store|production)\b", opening):
            hook = "proof-led"
        elif re.search(r"\b(risk|bottleneck|failure|issue|problem|before|depends on)\b", opening):
            hook = "diagnostic"
        elif re.search(r"\b(first|start|milestone|phase|plan|week|day one)\b", opening):
            hook = "plan-led"
        else:
            hook = "outcome-led"
    screening = data.get("screening_answers") or []
    return hook, screening if isinstance(screening, list) else []


def _chunk(text, limit=4000):
    """Split long text into Telegram-sized pieces, breaking at a paragraph/line boundary
    (not mid-sentence) so a >4096-char proposal reads cleanly across messages."""
    text = (text or "").strip()
    out = []
    while len(text) > limit:
        window = text[:limit]
        cut = window.rfind("\n\n")
        if cut < limit * 0.5:
            cut = window.rfind("\n")
        if cut < limit * 0.5:
            cut = limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return out


def _salvage_field(raw, field):
    """Pull one JSON string field out of possibly-truncated/malformed output."""
    m = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)', raw)
    if not m:
        return None
    frag = m.group(1)
    try:
        return json.loads('"' + frag + '"')            # proper unescape
    except Exception:
        return frag.replace("\\n", "\n").replace('\\"', '"').replace("\\t", "\t")


def format_proposal_messages(raw):
    """Turn the brain's JSON (or plain text) into paste-ready Telegram messages:
    cover letter first, then screening answers, then private notes."""
    data = None
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(m.group(0) if m else raw)
    except Exception:
        data = None
    if not isinstance(data, dict):
        # JSON was malformed/truncated — salvage the cover letter so we NEVER dump raw JSON.
        letter = _salvage_field(raw, "cover_letter")
        if letter:
            return _chunk(_clean_text(letter))
        return _chunk("⚠️ The model returned an unreadable response. Tap the button to retry.")

    # Message 1: the cover letter only — paste-ready, no header noise.
    msgs = _chunk(_clean_text((data.get("cover_letter") or "").strip())
                  or "(empty proposal — tap to retry)")

    # Message 2: ready answers to the common Upwork screening questions.
    sa = data.get("screening_answers") or []
    if sa:
        lines = ["📋 Common screening answers (copy the ones the job asks):"]
        for qa in sa:
            q = (qa.get("question") or "").strip()
            a = (qa.get("answer") or "").strip()
            if q:
                lines.append(f"\nQ: {q}")
            if a:
                lines.append(a)
        msgs += _chunk("\n".join(lines))

    # Private decision support is deliberately separate from paste-ready client copy.
    warning = _clean_text((data.get("fit_warning") or "").strip())
    if warning:
        msgs += _chunk(f"⚠️ Private fit warning (do not paste):\n{warning}")
    return msgs


# ---------- state ----------
def load_seen():
    if not SEEN_PATH.exists():
        return None  # first run
    try:
        return json.loads(SEEN_PATH.read_text())
    except Exception:
        return {}


def save_seen(seen):
    now = time.time()
    cutoff = now - SEEN_TTL_DAYS * 86400
    pruned = {k: v for k, v in seen.items() if v > cutoff}
    SEEN_PATH.write_text(json.dumps(pruned, indent=0))


def load_store():
    """State for the on-demand proposals: getUpdates offset + recently-notified job details
    (so a 'Generate Proposal' tap can look the job up later)."""
    try:
        s = json.loads(STORE_PATH.read_text())
        s.setdefault("offset", 0)
        s.setdefault("jobs", {})
        return s
    except Exception:
        return {"offset": 0, "jobs": {}}


def save_store(store):
    cutoff = time.time() - STORE_TTL_HOURS * 3600
    store["jobs"] = {k: v for k, v in store.get("jobs", {}).items() if v.get("ts", 0) > cutoff}
    try:
        STORE_PATH.write_text(json.dumps(store))
    except Exception as e:
        print(f"[warn] save_store failed: {e}", file=sys.stderr)


def remember_job(store, job):
    store.setdefault("jobs", {})[job["cipher"]] = {
        "title": job["title"], "description": job["description"], "skills": job["skills"],
        "job_type": job["job_type"], "hourly_min": job["hourly_min"],
        "hourly_max": job["hourly_max"], "fixed": job["fixed"],
        "link": job["link"], "score": job.get("score", 0), "ts": time.time(),
        "matched": job.get("matched", []), "tier": job.get("tier", ""),
        "publish": job.get("publish", ""), "cipher": job.get("cipher", ""),
    }


# ---------- button + message handling ----------
def _authorized(chat_id):
    """Only respond to the configured chat (the bot is public; ignore everyone else)."""
    return CHAT_ID and str(chat_id) == str(CHAT_ID)


def _proposal_buttons(cipher):
    return {"inline_keyboard": [[
        {"text": "❓ Answer screening questions", "callback_data": f"q:{cipher}"[:64]},
    ]]}


def _handle_callback(cq, store):
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    if not _authorized(chat_id):
        answer_callback(cq.get("id", ""))
        return
    data = cq.get("data", "") or ""

    if data.startswith("p:"):                       # Generate Proposal
        answer_callback(cq.get("id", ""), "Generating your proposal…")
        job = store.get("jobs", {}).get(data[2:])
        if not job:
            tg_reply(chat_id, mid, "⚠️ I no longer have this job's details (expired). "
                                   "Open it on Upwork to apply.")
            return
        # Backfill the key for jobs restored from a pre-tracker Actions cache. New jobs
        # already store it, but this makes the rollout compatible with the last 26h of cards.
        job.setdefault("cipher", data[2:])
        print(f"[info] proposal requested: {job['title'][:60]!r}")
        raw = generate_proposal(job)
        if not raw:
            tg_reply(chat_id, mid, _ai_failure_message("a proposal"))
            return
        cover = _extract_cover(raw)
        if cover:
            job["proposal"] = cover                 # store so answers can align with it
            hook_type, screening = _extract_proposal_metadata(raw)
            tracker_ingest("proposal_generated", job, proposal=cover,
                           hook_type=hook_type, screening=screening)
        chunks = format_proposal_messages(raw)
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            sent_id = tg_reply(chat_id, mid, chunk,
                               reply_markup=_proposal_buttons(data[2:]) if last else None)
            if i == 0 and sent_id:
                job["proposal_mid"] = sent_id       # answers will reply to this
            time.sleep(0.4)

    elif data.startswith("q:"):                     # Answer screening questions
        answer_callback(cq.get("id", ""), "Send me the questions")
        cipher = data[2:]
        job = store.get("jobs", {}).get(cipher)
        title = (job or {}).get("title", "this job")
        store["awaiting"] = {"cipher": cipher, "ts": time.time()}
        tg_reply(chat_id, mid,
                 f"📝 Send the screening questions for “{title[:60]}” — paste them in one "
                 f"message or several. I'll answer each, aligned with your proposal. "
                 f"(Auto-clears in {AWAITING_TTL_SEC // 60} min; send /cancel to stop.)")
    else:
        answer_callback(cq.get("id", ""))


def _handle_message(msg, store):
    chat_id = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    text = (msg.get("text") or "").strip()
    if not _authorized(chat_id) or not text:
        return
    if time.time() - msg.get("date", 0) > 600:      # ignore stale msgs (e.g. resurfaced on deploy)
        return
    if text.lower() in ("/cancel", "/stop", "/done"):
        store.pop("awaiting", None)
        tg_reply(chat_id, mid, "✅ Cleared.")
        return

    aw = store.get("awaiting")
    if not aw or (time.time() - aw.get("ts", 0)) > AWAITING_TTL_SEC:
        store.pop("awaiting", None)
        tg_reply(chat_id, mid,
                 "To get screening answers: tap 📝 Generate Proposal on a job, then "
                 "❓ Answer screening questions, then send me the questions.")
        return
    job = store.get("jobs", {}).get(aw["cipher"])
    if not job:
        store.pop("awaiting", None)
        tg_reply(chat_id, mid, "⚠️ That job expired from my memory. Generate a fresh proposal first.")
        return
    job.setdefault("cipher", aw["cipher"])
    aw["ts"] = time.time()                          # keep armed for follow-up questions
    print(f"[info] answering questions for: {job['title'][:50]!r}")
    answers = generate_answers(job, text)
    if not answers:
        tg_reply(chat_id, mid, _ai_failure_message("answers"))
        return
    # One message PER answer, replying to the proposal, so each is easy to copy-paste.
    # The answer sits in a <pre> block -> Telegram shows a one-tap Copy button on it.
    target = job.get("proposal_mid") or mid
    for i, (q, a) in enumerate(answers, 1):
        head = f"<b>❓ {esc(q)}</b>\n" if q else f"<b>Answer {i}</b>\n"
        tg_reply(chat_id, target, f"{head}<pre>{esc(a)}</pre>", parse_mode="HTML")
        time.sleep(0.35)
    tracker_ingest("screening_generated", job,
                   screening=[{"question": q, "answer": a} for q, a in answers])


def handle_updates(cfg, long_poll=0):
    """Poll Telegram for button taps and question messages; reply proposals + answers."""
    if not AI_ENABLED:
        return
    store = load_store()
    params = {"timeout": long_poll,
              "allowed_updates": json.dumps(["callback_query", "message"])}
    if store.get("offset"):
        params["offset"] = store["offset"] + 1
    try:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                         params=params, timeout=long_poll + 15)
        updates = r.json().get("result", []) or []
    except Exception as e:
        print(f"[warn] getUpdates failed: {e}", file=sys.stderr)
        return
    if not updates:
        return
    # Advance + persist the offset BEFORE the slow generation so a crash can't reprocess.
    start_offset = store.get("offset", 0)
    for u in updates:
        store["offset"] = max(store.get("offset", 0), u.get("update_id", 0))
    save_store(store)
    for u in updates:
        if u.get("update_id", 0) <= start_offset:
            continue
        if u.get("callback_query"):
            _handle_callback(u["callback_query"], store)
        elif u.get("message"):
            _handle_message(u["message"], store)
    save_store(store)


# ---------- one job scan ----------
def run_job_check(cfg, proxy):
    token = get_token(proxy)
    jobs = fetch_all_jobs(token, proxy, cfg)
    if not jobs:
        # Likely a stale cached token (401) — force a fresh one and retry once.
        print("[warn] no jobs on first pass; refreshing token and retrying", file=sys.stderr)
        token = get_token(proxy, force=True)
        jobs = fetch_all_jobs(token, proxy, cfg)
    print(f"[info] fetched {len(jobs)} unique jobs across lanes")

    hits = []
    for j in jobs:
        passes, score, matched = score_job(j, cfg)
        if passes:
            j["score"], j["matched"] = score, matched
            hits.append(j)
    hot = sum(1 for j in hits if j["score"] >= cfg["hot_min"])
    good = sum(1 for j in hits if cfg["good_min"] <= j["score"] < cfg["hot_min"])
    print(f"[info] {len(hits)} pass filters  (HOT={hot}, GOOD={good}, MAYBE={len(hits)-hot-good})")

    seen = load_seen()
    now = time.time()

    if seen is None:  # first run: seed silently
        seed = {j["cipher"]: now for j in jobs if j["cipher"]}
        save_seen(seed)
        send_plain(f"✅ Upwork notifier armed. Watching {len(seed)} jobs across "
                   f"{len(cfg.get('search_queries') or [])} search lanes. "
                   f"You'll get HOT/GOOD/MAYBE pings for new matching jobs.")
        print("[info] seeded baseline, no notifications sent")
        ping_healthcheck()
        return

    fresh = [j for j in hits if j["cipher"] not in seen]

    # Freshness guard: jobs older than MAX_AGE_HOURS are "handled" (marked seen below) but
    # never notified. Unknown age is treated as recent (kept).
    if MAX_AGE_HOURS > 0:
        recent, n_old = [], 0
        for j in fresh:
            a = age_hours(j["publish"])
            if a is not None and a > MAX_AGE_HOURS:
                n_old += 1
            else:
                recent.append(j)
        if n_old:
            print(f"[info] {n_old} matching jobs older than {MAX_AGE_HOURS}h (skipped)")
        fresh = recent

    fresh.sort(key=lambda j: -j["score"])  # best score first
    to_send = fresh[:MAX_NOTIFS]
    # Cap overflow is NOT marked seen -> it gets sent next check instead of being lost.
    overflow = {j["cipher"] for j in fresh[MAX_NOTIFS:]}
    if overflow:
        print(f"[info] {len(overflow)} over MAX_NOTIFS cap; will send next check")
    print(f"[info] {len(to_send)} new to notify")

    store = load_store()
    for j in to_send:
        delivered = send(j, cfg)     # card + on-demand proposal button
        if delivered:
            tracker_ingest("notified", j)
        remember_job(store, j)       # so a later tap can look this job up
        time.sleep(0.6)              # gentle on Telegram rate limits
    save_store(store)

    # Mark seen: everything we fetched EXCEPT the cap-overflow hits (so none are lost).
    for j in jobs:
        c = j["cipher"]
        if c and c not in overflow:
            seen[c] = now
    save_seen(seen)
    ping_healthcheck()
    print("[info] done")


# ---------- serve loop ----------
def serve(cfg, proxy):
    """Run for SERVE_SECONDS: scan jobs every JOB_INTERVAL and, in between, long-poll for
    button taps so proposals come back within seconds."""
    end = time.time() + SERVE_SECONDS
    last_check = 0.0
    print(f"[info] serve mode for {SERVE_SECONDS}s (job scan every {JOB_INTERVAL}s; "
          f"proposal button {'ON' if AI_ENABLED else 'off'})")
    while True:
        if time.time() >= end:
            break
        if time.time() - last_check >= JOB_INTERVAL:
            try:
                run_job_check(cfg, proxy)
            except Exception as e:
                print(f"[warn] job check failed: {e}", file=sys.stderr)
            last_check = time.time()
        remaining = end - time.time()
        if remaining <= 0:
            break
        if AI_ENABLED:
            handle_updates(cfg, long_poll=min(25, max(1, int(remaining))))
        else:
            time.sleep(min(15, max(1, int(remaining))))


# ---------- main ----------
def main():
    if "--check-openai" in sys.argv:
        if not OPENAI_API_KEY:
            sys.exit("[error] OPENAI_API_KEY is not configured")
        _AI_FAILURES.clear()
        result = _openai_generate("Reply with exactly: OK", max_tokens=256)
        if not result:
            sys.exit("[error] " + _ai_failure_message("the OpenAI smoke test"))
        print(f"[ok] OpenAI API smoke test passed with model {OPENAI_MODEL}")
        return
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    cfg = load_filters()
    print(f"[info] filters: {len(cfg['_require'])} required, {len(cfg['_exclude'])} excluded, "
          f"{len(cfg['_boost'])} boosts, {len(cfg.get('search_queries') or [])} search lanes, "
          f"min_score={cfg['min_score']}")

    proxy = get_proxy_dict()

    if SERVE_SECONDS > 0:
        serve(cfg, proxy)
    else:
        run_job_check(cfg, proxy)
        handle_updates(cfg, long_poll=0)  # drain any pending taps/messages once


if __name__ == "__main__":
    main()
