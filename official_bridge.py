#!/usr/bin/env python3
"""Deliver results of user-requested official Upwork plugin calls. No Upwork client.

The connected plugin remains inside ChatGPT/Codex. This bridge receives only results,
never its credentials. Preview by default; --deliver sends to the owner's Telegram.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request

import notifier

ALLOWED = {
    ("find_jobs", "get"), ("find_jobs", "search"), ("find_jobs", "smart_search"),
    ("list_freelancer_proposals", "list"), ("list_freelancer_proposals", "get"),
    ("list_freelancer_proposals", "invitations"),
    ("get_freelancer_financials", "connects_balance"),
    ("get_messages", "list_rooms"), ("list_offers", "list_mine"),
    ("list_contracts", "search"), ("get_profile", "get"),
}


def text(value):
    return re.sub(r"</?untrusted_participant_content>", "", str(value or "")).strip()


def instant(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("A timezone is required")
    return dt


def evidence(item):
    e = item.get("evidence", {})
    if (e.get("tool"), e.get("action")) not in ALLOWED:
        raise ValueError("Only allowlisted official read results are accepted")
    result = e.get("result")
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise ValueError("An unsuccessful/missing result is not a successful account read")
    if not e.get("trace_id") or result.get("trace_id") != e["trace_id"]:
        raise ValueError("The source trace must match the official response")
    return e, result


def job_from_get(item):
    e, r = evidence(item)
    if (e["tool"], e["action"]) != ("find_jobs", "get"):
        raise ValueError("A job alert requires full official job details")
    p = r.get("data", {}).get("marketplaceJobPosting", {})
    url = p.get("url") or r.get("url", "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "www.upwork.com" or not parsed.path.startswith("/jobs/"):
        raise ValueError("Expected the Upwork job URL returned by the plugin")
    cipher = re.search(r"~02\d+", parsed.path)
    if not cipher or not p.get("id"):
        raise ValueError("Missing job identity")
    if r.get("applied") or r.get("can_apply") is False:
        raise ValueError("Do not send an application lead for an already-applied/ineligible job")
    if p.get("workFlowState", {}).get("status") != "ACTIVE":
        raise ValueError("Job is not verified active")
    terms = p.get("contractTerms", {})
    content = p.get("content", {})
    job = {
        "cipher": cipher.group(), "job_id": str(p["id"]), "link": url,
        "title": text(content.get("title")), "description": text(content.get("description")),
        "skills": item.get("skills", []), "tier": terms.get("experienceLevel", ""),
        "job_type": terms.get("contractType", ""), "publish": item.get("published_date", ""),
        "hourly_min": None, "hourly_max": None,
        "fixed": terms.get("fixedPriceContractTerms", {}).get("amount", {}).get("rawValue"),
        "score": 0, "matched": [],
    }
    return job, r


def record(item):
    kind = item.get("kind")
    e, r = evidence(item)
    parts, tracker = [], []
    if kind in ("job", "draft"):
        job, result = job_from_get(item)
        cost = result.get("connects_cost")
        cost_label = str(cost) if cost is not None else "Unknown"
        header = (f"<b>{html.escape(job['title'])}</b>\n"
                  f"Budget: {html.escape(str(item.get('budget_label') or notifier.fmt_budget(job)))}\n"
                  f"Connects to apply: {cost_label}\n"
                  f"<a href=\"{html.escape(job['link'], quote=True)}\">Open on Upwork</a>")
        if item.get("review_notes"):
            header += "\n\nFor review: " + html.escape(text(item["review_notes"]))
        parts.append(header)
        tracker.append({"event": "notified", "job": notifier._tracker_job(job), "source": "upwork_official_mcp"})
        if kind == "draft":
            draft = item.get("draft", {})
            failures = notifier._proposal_hard_failures(json.dumps(draft), "individual", job)
            if failures:
                raise ValueError("Proposal QA failed: " + "; ".join(failures))
            terms = text(item.get("proposed_terms"))
            if not terms:
                raise ValueError("Proposed terms for review are required")
            parts.append("<b>Draft only. Not submitted.</b>\nTerms for review: " + html.escape(terms))
            cover = draft["cover_letter"]
            parts.append("<pre>" + html.escape(cover) + "</pre>")
            for answer in draft.get("screening_answers", []):
                parts.append(html.escape(text(answer.get("question"))) + "\n<pre>" + html.escape(text(answer.get("answer"))) + "</pre>")
            if draft.get("fit_warning"):
                parts.append("Private fit warning: " + html.escape(text(draft["fit_warning"])))
            tracker.append({"event": "proposal_generated", "job": notifier._tracker_job(job),
                            "proposal": cover, "screening": draft.get("screening_answers", []),
                            "hook_type": draft.get("hook_type", "unclassified"), "source": "upwork_official_mcp"})
        key = f"{kind}:{job['job_id']}"
        if kind == "draft":
            key += ":" + hashlib.sha256(json.dumps(item["draft"], sort_keys=True).encode()).hexdigest()[:16]
    elif kind == "connects":
        if (e["tool"], e["action"]) != ("get_freelancer_financials", "connects_balance"):
            raise ValueError("Expected official Connects result")
        balance = r.get("balance", {}).get("connectsBalance")
        if balance is None:
            raise ValueError("Connects balance unknown")
        parts.append(f"<b>Upwork Connects: {html.escape(str(balance))}</b>\nObserved: {html.escape(item['observed_at'])}\nRecent usage page only; product ad credits are separate.")
        key = f"connects:{item['observed_at']}:{balance}"
    elif kind == "proposal_snapshot":
        if (e["tool"], e["action"]) != ("list_freelancer_proposals", "list"):
            raise ValueError("Expected official proposal list")
        rows = [x["node"] for x in r.get("data", {}).get("vendorProposals", {}).get("edges", [])]
        submitted = [p for p in rows if p.get("status", {}).get("status") == "Accepted"]
        viewed = sum(p.get("viewedByClient") is True for p in submitted)
        parts.append(f"<b>Upwork application snapshot</b>\nObserved: {html.escape(item['observed_at'])}\n"
                     f"Submitted proposals on this page: {len(submitted)}\nViewed by client: {viewed}\n"
                     f"Coverage: {'partial; more pages exist' if r.get('hasMore') else 'this returned status page is complete'}\n"
                     "Submitted does not mean hired. Replies, interviews and wins are unknown unless separately verified.")
        key = "proposals:" + e["trace_id"]
    elif kind == "report":
        # Human analysis must be explicitly qualified; it cannot set pipeline status.
        report = text(item.get("report"))
        if not report or len(report) > 3500:
            raise ValueError("A report must contain 1-3500 characters")
        parts.append("<b>Upwork review</b>\n" + html.escape(report))
        key = "report:" + hashlib.sha256(report.encode()).hexdigest()[:24]
    else:
        raise ValueError("Unknown event kind")
    if any(len(p) > 3900 for p in parts):
        raise ValueError("Notification too long; shorten it before delivery")
    return {"key": key, "parts": parts, "tracker": tracker}


def prepare(batch):
    if batch.get("source") != "upwork_official_mcp" or batch.get("account_role") != "Freelancer":
        raise ValueError("Only personal Freelancer official-plugin batches are accepted")
    items = batch.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 12:
        raise ValueError("A bounded batch of 1-12 items is required")
    records = []
    for item in items:
        age = datetime.now(timezone.utc) - instant(item["observed_at"])
        if not -300 <= age.total_seconds() <= 86400:
            raise ValueError("Official results must have been observed in the last day")
        records.append(record(item))
    return records


def post_json(url, payload, headers=None):
    request = Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json", **(headers or {})}, method="POST")
    # Redirects are refused so authorization cannot leak to a redirected host.
    from urllib.request import HTTPRedirectHandler, build_opener
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    with build_opener(NoRedirect).open(request, timeout=20) as response:
        return json.load(response)


def deliver(records, path):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise ValueError("Existing Telegram configuration is unavailable")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    os.chmod(path, 0o600)
    db.execute("CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, status TEXT, receipt TEXT, created REAL)")
    db.execute("DELETE FROM deliveries WHERE created < ?", (time.time() - 30 * 86400,))
    db.commit()
    results = []
    for row in records:
        channels = [("telegram", i, p) for i, p in enumerate(row["parts"])]
        channels += [("tracker", i, p) for i, p in enumerate(row["tracker"])]
        for channel, index, payload in channels:
            identity = f"{row['key']}:{channel}:{index}"
            prior = db.execute("SELECT status FROM deliveries WHERE id=?", (identity,)).fetchone()
            if prior:
                results.append({"id": identity, "status": prior[0], "replayed": False})
                continue
            tracker_url = os.environ.get("TRACKER_API_URL", "").rstrip("/")
            tracker_token = os.environ.get("TRACKER_API_TOKEN", "")
            if channel == "tracker" and (not tracker_url or not tracker_token):
                results.append({"id": identity, "status": "not_configured"})
                continue
            # Claim before network I/O. A crash/timeout is never blindly resent.
            try:
                db.execute("INSERT INTO deliveries VALUES (?, 'sending', '', ?)", (identity, time.time()))
                db.commit()
            except sqlite3.IntegrityError:
                results.append({"id": identity, "status": "already_claimed"})
                continue
            try:
                if channel == "telegram":
                    result = post_json(f"https://api.telegram.org/bot{token}/sendMessage", {
                        "chat_id": chat, "text": payload, "parse_mode": "HTML", "disable_web_page_preview": True,
                    })
                    if not result.get("ok") or not result.get("result", {}).get("message_id"):
                        raise ValueError("Telegram did not return a delivery receipt")
                    receipt = str(result["result"]["message_id"])
                else:
                    if urlparse(tracker_url).scheme != "https":
                        raise ValueError("Tracker must use HTTPS")
                    result = post_json(tracker_url + "/api/ingest", payload, {"Authorization": "Bearer " + tracker_token})
                    if result.get("ok") is not True:
                        raise ValueError("Tracker did not confirm ingestion")
                    receipt = "ingested"
                status = "delivered"
            except Exception:
                # Do not log request objects, credentials or remote error bodies.
                status, receipt = "uncertain", "Reconcile delivery before retrying"
            db.execute("UPDATE deliveries SET status=?, receipt=? WHERE id=?", (status, receipt, identity))
            db.commit()
            results.append({"id": identity, "status": status, "receipt": receipt})
    db.close()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Official-plugin batch JSON, or - for stdin")
    parser.add_argument("--deliver", action="store_true", help="Send the previewed notifications")
    parser.add_argument("--state", default=os.environ.get("BRIDGE_STATE_PATH", "official-delivery.sqlite3"))
    args = parser.parse_args()
    raw = sys.stdin.read(250_001) if args.input == "-" else Path(args.input).read_text()
    if len(raw) > 250_000:
        raise ValueError("Batch exceeds size limit")
    records = prepare(json.loads(raw))
    if args.deliver:
        results = deliver(records, args.state)
        print(json.dumps(results))
        if any(r["status"] not in ("delivered", "not_configured") for r in results):
            raise SystemExit(2)
    else:
        print(json.dumps(records, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
