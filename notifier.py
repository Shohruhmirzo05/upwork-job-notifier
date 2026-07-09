#!/usr/bin/env python3
"""
Upwork -> Telegram recent-jobs notifier.

Runs statelessly (built for GitHub Actions cron). Each run:
  1. Grabs a visitor GraphQL token from upwork.com (curl_cffi Chrome TLS impersonation).
  2. Queries Upwork's public job search GraphQL API, sorted by recency.
  3. Scores each job with a LOCAL, zero-cost weighted-keyword engine (filters.json).
  4. Dedupes against seen.json (committed back by the workflow).
  5. Sends each genuinely-new job that clears your score threshold to Telegram.

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
from pathlib import Path

from curl_cffi import requests

# ---------- config (via env / GitHub secrets) ----------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
WEBSHARE_URL = os.environ.get("WEBSHARE_URL", "").strip()  # optional; empty = go direct
MAX_PAGES = int(os.environ.get("MAX_PAGES", "2"))     # 50 jobs/page
PAGE_SIZE = 50
MAX_NOTIFS = int(os.environ.get("MAX_NOTIFS", "20"))  # cap per run to avoid flood
SEEN_TTL_DAYS = int(os.environ.get("SEEN_TTL_DAYS", "5"))
SEEN_PATH = Path(os.environ.get("SEEN_PATH", "seen.json"))
FILTERS_PATH = Path(os.environ.get("FILTERS_PATH", "filters.json"))

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
# Rules live in filters.json (see that file for the full annotated example):
#   {
#     "require_any": [...],   # job must match >=1 of these (empty list = allow all)
#     "exclude":     [...],   # reject the job if it matches ANY of these
#     "boost":       {term: weight, ...},  # +weight to score per matched term
#     "title_multiplier": 2,  # matches in the TITLE count this many times
#     "min_score": 1,         # drop jobs scoring below this
#     "word_boundary": true   # match whole words ("ios" != "kiosk")
#   }
# Env vars KEYWORDS / EXCLUDE_KEYWORDS still work as a fallback if filters.json
# is absent, so nothing breaks if you'd rather keep it simple.

DEFAULT_FILTERS = {
    "require_any": [k.strip().lower()
                    for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()],
    "exclude": [k.strip().lower()
                for k in os.environ.get("EXCLUDE_KEYWORDS", "").split(",") if k.strip()],
    "boost": {},
    "title_multiplier": 2,
    "min_score": 1,
    "word_boundary": True,
}


def _compile_term(term, word_boundary):
    """Compile one keyword to a regex. Word-boundary aware but tolerant of
    symbols like c++, c#, node.js — so those still match sanely."""
    term = term.lower().strip()
    esc = re.escape(term)
    if word_boundary:
        # (?<![a-z0-9]) / (?![a-z0-9]) instead of \b so "c++" and "node.js" work.
        return re.compile(rf"(?<![a-z0-9]){esc}(?![a-z0-9])")
    return re.compile(esc)


def load_filters():
    """Read filters.json if present, else fall back to env KEYWORDS/EXCLUDE.
    Returns a dict with pre-compiled regex lists for speed over many terms."""
    cfg = dict(DEFAULT_FILTERS)
    if FILTERS_PATH.exists():
        try:
            user = json.loads(FILTERS_PATH.read_text())
            for key in ("require_any", "exclude", "boost",
                        "title_multiplier", "min_score", "word_boundary"):
                if key in user:
                    cfg[key] = user[key]
        except Exception as e:
            print(f"[warn] filters.json unreadable ({e}); using env fallback", file=sys.stderr)

    wb = bool(cfg.get("word_boundary", True))
    cfg["_require"] = [(t, _compile_term(t, wb)) for t in cfg.get("require_any", []) if t]
    cfg["_exclude"] = [(t, _compile_term(t, wb)) for t in cfg.get("exclude", []) if t]
    cfg["_boost"] = [(t, float(w), _compile_term(t, wb))
                     for t, w in (cfg.get("boost", {}) or {}).items() if t]
    return cfg


def score_job(job, cfg):
    """Deterministic local score. Returns (passes: bool, score: int, matched: list).
    passes is False if excluded, no required match, or score < min_score."""
    title = job["title"].lower()
    body = " ".join([job["title"], job["description"], " ".join(job["skills"])]).lower()

    # hard exclude
    for term, rx in cfg["_exclude"]:
        if rx.search(body):
            return (False, 0, [])

    matched = []
    score = 0.0
    tmult = float(cfg.get("title_multiplier", 2))

    # required keywords: must hit at least one (unless no require list = allow all)
    req_hits = 0
    for term, rx in cfg["_require"]:
        if rx.search(body):
            req_hits += 1
            matched.append(term)
            score += tmult if rx.search(title) else 1.0
    if cfg["_require"] and req_hits == 0:
        return (False, 0, [])

    # weighted boosts (prioritize the jobs you care most about)
    for term, weight, rx in cfg["_boost"]:
        if rx.search(body):
            if term not in matched:
                matched.append(term)
            score += weight * (tmult if rx.search(title) else 1.0)

    score = int(round(score))
    if score < int(cfg.get("min_score", 1)):
        return (False, score, matched)
    return (True, score, matched)


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
def get_token(proxy):
    resp = requests.get(UPWORK_HOME, impersonate="chrome", proxies=proxy, timeout=30)
    resp.raise_for_status()
    token = resp.cookies.get(TOKEN_COOKIE)
    if not token:
        raise RuntimeError(f"No {TOKEN_COOKIE} cookie. Got: {list(resp.cookies.keys())}")
    return token


def fetch_page(token, proxy, offset):
    headers = {**HEADERS_BASE, "Authorization": f"Bearer {token}"}
    payload = {
        "query": GRAPHQL_QUERY,
        "variables": {"requestVariables": {
            "sort": "recency", "highlight": True,
            "paging": {"offset": offset, "count": PAGE_SIZE},
        }},
    }
    resp = requests.post(GRAPHQL_URL, headers=headers, json=payload,
                         proxies=proxy, impersonate="chrome", timeout=25)
    resp.raise_for_status()
    data = resp.json()
    return (data.get("data", {}).get("search", {}).get("universalSearchNuxt", {})
            .get("visitorJobSearchV1", {}).get("results", []) or [])


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


# ---------- telegram ----------
def fmt_budget(job):
    if job["job_type"] == "HOURLY":
        lo, hi = job["hourly_min"], job["hourly_max"]
        if lo or hi:
            return f"${int(float(lo or 0))}-${int(float(hi or 0))}/hr"
        return "Hourly"
    if job["fixed"]:
        return f"${int(float(job['fixed']))} fixed"
    return job["job_type"].title() or "—"


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send(job):
    tier = f" · {job['tier'].title()}" if job["tier"] else ""
    skills = ", ".join(job["skills"][:6])
    desc = job["description"][:280].strip()
    matched = ", ".join(job.get("matched", [])[:6])
    score_line = ""
    if job.get("score") is not None:
        mtxt = f" — {esc(matched)}" if matched else ""
        score_line = f"⭐ <b>{job['score']}</b>{mtxt}\n"
    msg = (
        f"🟢 <b>{esc(job['title'][:120])}</b>\n"
        + score_line
        + f"💰 {esc(fmt_budget(job))}{esc(tier)}\n"
        + (f"🏷 {esc(skills)}\n" if skills else "")
        + (f"\n{esc(desc)}…\n" if desc else "")
        + f'\n<a href="{job["link"]}">Open on Upwork ↗</a>'
    )
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML",
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
          f"{len(cfg['_boost'])} boosts, min_score={cfg.get('min_score')}")

    proxy = get_proxy_dict()
    token = get_token(proxy)

    jobs = []
    for offset in range(0, MAX_PAGES * PAGE_SIZE, PAGE_SIZE):
        try:
            for r in fetch_page(token, proxy, offset):
                jobs.append(parse_job(r))
        except Exception as e:
            print(f"[warn] page offset {offset} failed: {e}", file=sys.stderr)

    print(f"[info] fetched {len(jobs)} jobs")

    # score every job locally
    hits = []
    for j in jobs:
        if not j["cipher"]:
            continue
        passes, score, matched = score_job(j, cfg)
        if passes:
            j["score"], j["matched"] = score, matched
            hits.append(j)
    print(f"[info] {len(hits)} pass filters")

    seen = load_seen()
    now = time.time()

    if seen is None:  # first run: seed silently
        seed = {j["cipher"]: now for j in jobs if j["cipher"]}
        save_seen(seed)
        send_plain(f"✅ Upwork notifier armed. Watching {len(seed)} jobs. "
                   f"You'll get pings for new matching jobs (local keyword scoring, no AI).")
        print("[info] seeded baseline, no notifications sent")
        return

    fresh = [j for j in hits if j["cipher"] not in seen]
    fresh.sort(key=lambda j: -j["score"])  # best score first
    print(f"[info] {len(fresh)} new to notify")

    for j in fresh[:MAX_NOTIFS]:
        send(j)
        time.sleep(0.6)  # gentle on Telegram rate limits

    # mark everything we saw as seen (not just notified) so filters can change later
    for j in jobs:
        if j["cipher"]:
            seen[j["cipher"]] = now
    save_seen(seen)
    print("[info] done")


if __name__ == "__main__":
    main()
