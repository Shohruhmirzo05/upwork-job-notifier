#!/usr/bin/env python3
"""Manual, isolated QA for proposal generation.

Fetches jobs through the same public Upwork search and scoring code as the notifier,
selects a technically diverse sample, generates proposals without contacting Telegram,
and writes deterministic plus model-judged quality reports.
"""

import argparse
import json
import re
from pathlib import Path

import notifier


CATEGORIES = {
    "b2b_ai": ("rag", "ai agent", "chatbot", "automation", "crm", "business"),
    "voice_ai": ("voice", "realtime", "speech", "stt", "tts", "elevenlabs"),
    "native_ios": ("swift", "swiftui", "uikit", "ios developer", "iphone"),
    "flutter": ("flutter", "dart", "cross-platform"),
    "app_store": ("app store", "testflight", "storekit", "revenuecat", "subscription"),
    "backend": ("fastapi", "python", "backend", "postgresql", "api", "websocket"),
    "vision_camera": ("ocr", "vision", "camera", "image recognition", "computer vision"),
    "mobile_general": ("mobile app", "android", "firebase", "supabase"),
}

KNOWN_PROJECTS = (
    "BandMate", "Salom AI Business", "Salom AI", "Launchcast", "CrisisPath",
    "Clove AI", "Goby AI", "PicTrans", "QuarCade", "Kowl", "Karly", "Fera Tech",
)

GENERIC_OPENERS = (
    "i'm a senior", "i am a senior", "i have ", "i can help", "i understand you need",
    "this project caught my attention", "i'm excited", "i am excited", "dear hiring manager",
    "i would love the opportunity", "i am the perfect fit", "i'm the perfect fit",
)


def _category(job):
    text = " ".join((job.get("title", ""), job.get("description", ""),
                     " ".join(job.get("skills") or []))).lower()
    scored = []
    for name, terms in CATEGORIES.items():
        scored.append((sum(1 for term in terms if term in text), name))
    best, name = max(scored)
    return name if best else "other"


def _select_diverse(hits, count):
    selected = []
    used = set()
    for job in hits:
        category = _category(job)
        if category not in used:
            job["qa_category"] = category
            selected.append(job)
            used.add(category)
            if len(selected) == count:
                return selected
    for job in hits:
        if job not in selected:
            job["qa_category"] = _category(job)
            selected.append(job)
            if len(selected) == count:
                break
    return selected


def fetch_sample(count):
    cfg = notifier.load_filters()
    proxy = notifier.get_proxy_dict()
    token = notifier.get_token(proxy)
    jobs = notifier.fetch_all_jobs(token, proxy, cfg)
    hits = []
    for job in jobs:
        passes, score, matched = notifier.score_job(job, cfg)
        age = notifier.age_hours(job.get("publish"))
        if passes and (age is None or age <= notifier.MAX_AGE_HOURS):
            job["score"], job["matched"] = score, matched
            hits.append(job)
    hits.sort(key=lambda item: (-item["score"], notifier.age_hours(item.get("publish")) or 0))
    if len(hits) < count:
        raise RuntimeError(f"Only {len(hits)} recent matching jobs were available")
    return _select_diverse(hits, count), len(jobs), len(hits)


def _preview(cover):
    body = re.sub(r"^\s*Hi,?\s*", "", cover or "", flags=re.I)
    return body.lstrip()[:300]


def deterministic_audit(job, cover):
    preview = _preview(cover)
    preview_lower = preview.lower()
    words = re.findall(r"\b[\w'+.-]+\b", cover or "")
    urls = re.findall(r"https?://\S+", cover or "")
    # Match longest names first and consume them so "Salom AI" is not double-counted
    # inside "Salom AI Business".
    project_text = (cover or "").lower()
    projects = []
    for name in sorted(KNOWN_PROJECTS, key=len, reverse=True):
        needle = name.lower()
        if needle in project_text:
            projects.append(name)
            project_text = project_text.replace(needle, " ")
    projects.sort(key=KNOWN_PROJECTS.index)
    questions = [line for line in (cover or "").splitlines() if line.strip().endswith("?")]

    title_terms = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9.+#-]{2,}",
                                                 job.get("title", ""))
                   if w.lower() not in {"the", "and", "for", "with", "need", "developer"}}
    skill_terms = {s.lower() for s in (job.get("skills") or []) if len(s) > 2}
    overlap = sorted(term for term in title_terms | skill_terms if term in preview_lower)
    generic = [phrase for phrase in GENERIC_OPENERS if preview_lower.startswith(phrase)]
    link_in_preview = "http" in preview_lower

    score = 0
    score += 14 if not generic else 0
    score += min(11, len(overlap) * 4)
    score += 10 if projects else 0
    score += 10 if urls and not link_in_preview else (4 if projects else 0)
    score += 8 if 1 <= len(projects) <= 2 else (4 if len(projects) == 3 else 0)
    execution_terms = ("audit", "verify", "reproduce", "implement", "integrate", "test",
                       "qa", "release", "deploy", "milestone", "monitor", "isolate")
    score += min(18, sum(3 for term in execution_terms if term in (cover or "").lower()))
    score += 14 if 80 <= len(words) <= 300 else (7 if 60 <= len(words) <= 350 else 0)
    score += 7 if len(questions) <= 1 else 2
    score += 8 if "perfect fit" not in (cover or "").lower() and "guarantee" not in (cover or "").lower() else 0

    flags = []
    if generic:
        flags.append(f"generic preview opener: {generic[0]}")
    if len(overlap) < 2:
        flags.append("fewer than two title/skill terms appear in the preview")
    if link_in_preview:
        flags.append("portfolio link consumes preview space")
    if not projects:
        flags.append("no named portfolio proof")
    if len(projects) > 2:
        flags.append("more than two portfolio projects")
    if not 80 <= len(words) <= 300:
        flags.append(f"length outside preferred range: {len(words)} words")
    if len(questions) > 1:
        flags.append("more than one CTA question")

    return {
        "score": min(100, score),
        "word_count": len(words),
        "preview": preview,
        "preview_job_terms": overlap,
        "projects": projects,
        "url_count": len(urls),
        "question_count": len(questions),
        "flags": flags,
    }


def model_judge(results):
    compact = []
    for item in results:
        compact.append({
            "id": item["id"],
            "job": {
                "title": item["job"]["title"],
                "description": item["job"]["description"][:1600],
                "skills": item["job"]["skills"],
            },
            "proposal": item["cover_letter"],
        })
    prompt = """You are a strict Upwork proposal QA reviewer. Evaluate the proposals below.

Research-backed rubric:
- The first 2-3 sentences are list-view preview space and must form a specific mini-proposal.
- The opening should use a concrete job detail plus credible proof, diagnostic insight, or a first milestone.
- Generic biography, enthusiasm, paraphrasing the post, skills dumps, links, and rates waste the preview.
- Use only 1-2 close portfolio examples and explain why each matters.
- Include a concrete execution path and the delivery risk that matters for this exact technical job.
- Be concise and scannable, normally 140-220 words and no more than 300 for complex work.
- Use a single low-effort question only when its answer changes scope, architecture, cost, or timing.
- Penalize fake certainty, invented diagnosis, vague claims, keyword stuffing, and any mismatch between proof and job.

Score preview, specificity, proof, execution_plan, credibility, brevity, and cta from 0-5. Give a total from 0-100. A passing proposal needs total >= 80, preview >= 4, specificity >= 4, credibility >= 4, and no critical issue. Be demanding. Return JSON only:
{"evaluations":[{"id":"1","total":0,"preview":0,"specificity":0,"proof":0,"execution_plan":0,"credibility":0,"brevity":0,"cta":0,"verdict":"pass|revise","issues":["..."],"best_fix":"..."}]}

PROPOSALS:
""" + json.dumps(compact, ensure_ascii=False)
    raw = notifier._generate(prompt, json_mode=True, max_tokens=8192)
    if not raw:
        return []
    try:
        match = re.search(r"\{.*\}", raw, re.S)
        return json.loads(match.group(0) if match else raw).get("evaluations") or []
    except Exception as exc:
        return [{"id": "judge-error", "verdict": "revise", "issues": [str(exc)],
                 "best_fix": "Inspect the raw generation manually."}]


def render_markdown(report):
    lines = [
        "# Proposal QA report",
        "",
        f"Fetched {report['fetched_jobs']} unique jobs; {report['matching_recent_jobs']} recent jobs passed filters.",
        f"Generated {len(report['results'])} proposals.",
        "",
    ]
    judges = {str(item.get("id")): item for item in report.get("model_evaluations", [])}
    for item in report["results"]:
        job, audit = item["job"], item["deterministic_audit"]
        judge = judges.get(str(item["id"]), {})
        lines.extend([
            f"## {item['id']}. {job['title']}", "",
            f"- Category: {job['qa_category']}",
            f"- Match score: {job['score']}",
            f"- Job: {job['link']}",
            f"- Deterministic QA: {audit['score']}/100",
            f"- Model QA: {judge.get('total', 'unavailable')}/100 ({judge.get('verdict', 'unavailable')})",
            f"- Flags: {', '.join(audit['flags']) if audit['flags'] else 'none'}",
            f"- Judge issues: {'; '.join(judge.get('issues') or []) or 'none'}",
            f"- Best fix: {judge.get('best_fix', 'none')}", "",
            "### Proposal", "", item["cover_letter"], "",
        ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=8, choices=range(5, 11))
    parser.add_argument("--output-dir", default="proposal-qa-output")
    args = parser.parse_args()
    if not notifier.AI_ENABLED:
        raise SystemExit("Set GEMINI_API_KEY or OPENAI_API_KEY for proposal QA")

    jobs, fetched, matching = fetch_sample(args.count)
    results = []
    for index, job in enumerate(jobs, 1):
        print(f"[qa] generating {index}/{len(jobs)}: {job['title']}")
        raw = notifier.generate_proposal(job)
        cover = notifier._extract_cover(raw)
        if not cover:
            cover = f"GENERATION FAILED\n\nRaw output:\n{raw or '(empty)'}"
        results.append({
            "id": str(index),
            "job": {key: job.get(key) for key in (
                "title", "description", "skills", "job_type", "hourly_min", "hourly_max",
                "fixed", "link", "score", "matched", "qa_category")},
            "cover_letter": cover,
            "deterministic_audit": deterministic_audit(job, cover),
        })

    print("[qa] running batch model review")
    report = {
        "fetched_jobs": fetched,
        "matching_recent_jobs": matching,
        "results": results,
        "model_evaluations": model_judge(results),
    }
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (output / "report.md").write_text(render_markdown(report))
    print(f"[qa] reports written to {output}")


if __name__ == "__main__":
    main()
