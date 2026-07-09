#!/usr/bin/env python3
"""
Upwork -> Telegram recent-jobs notifier.

Runs statelessly (built for GitHub Actions cron). Each run:
  1. Grabs a visitor GraphQL token from upwork.com (curl_cffi Chrome TLS impersonation).
  2. Queries Upwork's public job search GraphQL API, sorted by recency.
  3. Filters by your KEYWORDS / EXCLUDE_KEYWORDS.
  4. Dedupes against seen.json (committed back by the workflow).
  5. Sends each genuinely-new job to your Telegram chat.

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
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
WEBSHARE_URL = os.environ.get("WEBSHARE_URL", "").strip()  # optional; empty = go direct
KEYWORDS = [k.strip().lower() for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()]
EXCLUDE = [k.strip().lower() for k in os.environ.get("EXCLUDE_KEYWORDS", "").split(",") if k.strip()]
MAX_PAGES = int(os.environ.get("MAX_PAGES", "2"))     # 50 jobs/page
PAGE_SIZE = 50
MAX_NOTIFS = int(os.environ.get("MAX_NOTIFS", "15"))  # cap per run to avoid flood
SEEN_TTL_DAYS = int(os.environ.get("SEEN_TTL_DAYS", "5"))
SEEN_PATH = Path(os.environ.get("SEEN_PATH", "seen.json"))

# ---------- AI scoring (optional; set ANTHROPIC_API_KEY to enable) ----------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MIN_SCORE = int(os.environ.get("MIN_SCORE", "7"))     # only notify jobs scored >= this
SCORE_MODEL = os.environ.get("SCORE_MODEL", "claude-haiku-4-5-20251001").strip()
PROFILE = os.environ.get("PROFILE", "").strip()
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_PROFILE = (
    "Senior mobile + AI developer. Strong in SwiftUI, iOS, Swift, Flutter, and "
    "cross-platform mobile apps, plus AI agent / LLM integration work (OpenAI, "
    "Anthropic Claude) and Supabase backends. Wants well-scoped jobs from serious "
    "clients with real budgets. Not interested in WordPress, PHP, data entry, or "
    "generic virtual-assistant work."
)

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


# ---------- filtering ----------
def matches(job):
    hay = " ".join([job["title"], job["description"], " ".join(job["skills"])]).lower()
    if EXCLUDE and any(x in hay for x in EXCLUDE):
        return False
    if not KEYWORDS:
        return True  # no keywords set = everything (not recommended)
    return any(k in hay for k in KEYWORDS)


# ---------- ai scoring ----------
def score_jobs(jobs):
    """Rate each job 1-10 vs the profile in ONE batched Claude call.

    Returns a list of (score:int, reason:str) aligned to `jobs`, or None if
    scoring is disabled/failed (caller then sends every job unscored).
    Only fresh, post-dedup jobs are ever passed here, so this is ~1 cheap call/run.
    """
    if not ANTHROPIC_API_KEY or not jobs:
        return None
    profile = PROFILE or DEFAULT_PROFILE
    listing = []
    for i, j in enumerate(jobs):
        skills = ", ".join(j["skills"][:8])
        desc = j["description"][:600].replace("\n", " ").strip()
        listing.append(
            f"[{i}] title: {j['title']}\n"
            f"    budget: {fmt_budget(j)} | tier: {j['tier'] or 'n/a'}\n"
            f"    skills: {skills}\n"
            f"    desc: {desc}"
        )
    prompt = (
        "You screen freelance Upwork jobs for a developer with this profile:\n\n"
        f"{profile}\n\n"
        "Rate how well each job below fits this profile from 1 to 10 "
        "(10 = strong fit, worth applying now; 1 = irrelevant). Weigh skill match, "
        "budget quality, and how well-scoped/serious the client seems.\n\n"
        f"Jobs:\n{chr(10).join(listing)}\n\n"
        "Respond with ONLY a JSON array, one object per job in the same order:\n"
        '[{"i":0,"score":8,"reason":"<=12 words why"}]\n'
        "No prose, no markdown fences."
    )
    body = {
        "model": SCORE_MODEL,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        r = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
            timeout=60,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
        m = re.search(r"\[.*\]", text, re.DOTALL)  # tolerate stray fences/prose
        arr = json.loads(m.group(0) if m else text)
        by_i = {}
        for o in arr:
            by_i[int(o["i"])] = (int(o["score"]), str(o.get("reason", "")).strip())
        # Missing/malformed entries default to MIN_SCORE so a keyword hit is never
        # silently lost to a scoring hiccup — it just gets sent without a reason.
        return [by_i.get(i, (MIN_SCORE, "")) for i in range(len(jobs))]
    except Exception as e:
        print(f"[warn] scoring failed ({e}); sending unscored", file=sys.stderr)
        return None


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
    score_line = ""
    if job.get("score") is not None:
        reason = f" — {esc(job['reason'])}" if job.get("reason") else ""
        score_line = f"⭐ <b>{job['score']}/10</b>{reason}\n"
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
    hits = [j for j in jobs if j["cipher"] and matches(j)]
    print(f"[info] {len(hits)} match filters")

    seen = load_seen()
    now = time.time()

    if seen is None:  # first run: seed silently
        seed = {j["cipher"]: now for j in jobs if j["cipher"]}
        save_seen(seed)
        ai = f" AI-scoring on (≥{MIN_SCORE}/10)." if ANTHROPIC_API_KEY else ""
        send_plain(f"✅ Upwork notifier armed. Watching {len(seed)} jobs. "
                   f"You'll get pings for new {'/'.join(KEYWORDS) or 'all'} jobs.{ai}")
        print("[info] seeded baseline, no notifications sent")
        return

    fresh = [j for j in hits if j["cipher"] not in seen]
    fresh.sort(key=lambda j: j["publish"])  # oldest first

    # Optional AI scoring layer: rate the fresh keyword hits, keep only >= MIN_SCORE.
    scores = score_jobs(fresh)
    if scores is not None:
        for j, (sc, reason) in zip(fresh, scores):
            j["score"], j["reason"] = sc, reason
        kept = [j for j in fresh if j["score"] >= MIN_SCORE]
        kept.sort(key=lambda j: -j["score"])  # best first
        print(f"[info] scoring on: {len(kept)}/{len(fresh)} scored >= {MIN_SCORE}")
        to_notify = kept
    else:
        to_notify = fresh
    print(f"[info] {len(to_notify)} new to notify")

    for j in to_notify[:MAX_NOTIFS]:
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
