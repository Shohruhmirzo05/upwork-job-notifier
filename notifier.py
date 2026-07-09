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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash").strip()
PROPOSAL_MIN_SCORE = _int_env("PROPOSAL_MIN_SCORE", 30)  # only draft for jobs >= this
PROFILE_PATH = Path(os.environ.get("PROFILE_PATH", "profile.md"))

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
    lines.append(f'<a href="{job["link"]}">Open ↗</a>')
    return "\n".join(lines)


def send(job, cfg):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": build_message(job, cfg), "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"[warn] telegram {r.status_code}: {r.text[:200]}", file=sys.stderr)


def send_plain(text):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text}, timeout=15,
    )


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


def _proposal_prompt(job, profile):
    skills = ", ".join(job["skills"][:10])
    desc = job["description"][:1500]
    return (
        "Write a first-person Upwork proposal for the freelancer described below.\n\n"
        f"FREELANCER PROFILE:\n{profile or '(a senior mobile & AI developer)'}\n\n"
        "JOB POST:\n"
        f"Title: {job['title']}\n"
        f"Budget: {fmt_budget(job)}\n"
        f"Skills: {skills}\n"
        f"Description: {desc}\n\n"
        "RULES:\n"
        "- 140-180 words, plain text, no markdown, no bullet symbols, no emojis.\n"
        "- Open with a specific hook about THEIR project (never 'I saw your job post').\n"
        "- Use the 2-3 most relevant proofs from the profile that match THIS job.\n"
        "- Include one concrete idea or one sharp clarifying question.\n"
        "- Confident and human. No 'I am excited', no fluff.\n"
        "- End with a brief call to action.\n"
    )


def generate_proposal(job, profile):
    """Draft a tailored proposal via Gemini's free tier. None on any failure/disabled."""
    if not GEMINI_API_KEY:
        return None
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    body = {
        "contents": [{"parts": [{"text": _proposal_prompt(job, profile)}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 700},
    }
    try:
        r = requests.post(url, json=body, timeout=45)
        if r.status_code != 200:
            print(f"[warn] gemini HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return None
        cands = r.json().get("candidates") or []
        if not cands:
            return None
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None
    except Exception as e:
        print(f"[warn] gemini error: {e}", file=sys.stderr)
        return None


def send_proposal(text):
    msg = f"📝 Draft proposal — review, tweak, send:\n\n{text}"
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": msg[:4000], "disable_web_page_preview": True},
        timeout=15,
    )


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


# ---------- main ----------
def main():
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    cfg = load_filters()
    print(f"[info] filters: {len(cfg['_require'])} required, {len(cfg['_exclude'])} excluded, "
          f"{len(cfg['_boost'])} boosts, {len(cfg.get('search_queries') or [])} search lanes, "
          f"min_score={cfg['min_score']}")

    proxy = get_proxy_dict()
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
                   f"You'll get HOT/GOOD/MAYBE pings for new matching jobs (no AI, local scoring).")
        print("[info] seeded baseline, no notifications sent")
        ping_healthcheck()
        return

    fresh = [j for j in hits if j["cipher"] not in seen]

    # Freshness guard: jobs older than MAX_AGE_HOURS are "handled" (marked seen below) but
    # never notified. Unknown age is treated as recent (kept).
    if MAX_AGE_HOURS > 0:
        recent = []
        n_old = 0
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
    # Cap overflow is NOT marked seen -> it gets sent on the next check instead of being lost.
    overflow = {j["cipher"] for j in fresh[MAX_NOTIFS:]}
    if overflow:
        print(f"[info] {len(overflow)} over MAX_NOTIFS cap; will send next check")
    print(f"[info] {len(to_send)} new to notify")

    profile = load_profile() if GEMINI_API_KEY else ""
    for j in to_send:
        send(j, cfg)
        time.sleep(0.6)  # gentle on Telegram rate limits
        # Draft a tailored proposal for the better jobs (free Gemini; disabled if no key).
        if GEMINI_API_KEY and j["score"] >= PROPOSAL_MIN_SCORE:
            proposal = generate_proposal(j, profile)
            if proposal:
                send_proposal(proposal)
                time.sleep(0.6)

    # Mark seen: everything we fetched EXCEPT the cap-overflow hits (so none are lost).
    for j in jobs:
        c = j["cipher"]
        if c and c not in overflow:
            seen[c] = now
    save_seen(seen)
    ping_healthcheck()
    print("[info] done")


if __name__ == "__main__":
    main()
