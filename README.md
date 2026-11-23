# Official Upwork plugin results → Telegram

The existing Telegram bot and Proposal Radar receive curated job alerts, copy-ready
proposals, Connects snapshots and qualified performance reports. Upwork reads happen
**on demand through the connected official plugin in ChatGPT/Codex**. This bot does not
connect to Upwork, extract plugin credentials, automate its website, scrape listings,
rotate proxies or poll for marketplace changes. No recurring discovery is configured.

## Rules

Use Shohruh Alijonov's personal Freelancer account. Follow current guidance:
- https://support.upwork.com/hc/en-us/articles/55446516654611-How-to-use-Upwork-with-AI-agents-through-MCP
- https://support.upwork.com/hc/en-us/articles/56577997707923-Rate-limits-for-Upwork-MCP

Do not recreate checks every few minutes or bypass a restriction. The host plugin's
connection is not a server API key that this bot can reuse. This is an on-demand result
relay, not a standalone connected MCP client. Submission still requires the official
complete preview and explicit confirmation. The bot cannot submit, boost, buy Connects,
edit profiles, accept offers or send Upwork client messages.

Accepted means Submitted, not hired. A view is not a reply. Partial/status-filtered
proposal lists cannot establish whole-account conversion rates. Current access does not
prove that an account-risk warning has been removed.

## Existing configuration retained

Telegram: @upwork_notificationsbot, configured owner's chat.
Private tracker: https://upworkproposals.fera-tech.com
Verified evidence/voice: profile.md, proposal_prompt.md, notifier.py's portfolio routing
and proposal quality gate. filters.json can score locally supplied jobs, never search.
notifier.py's service loop handles Telegram buttons only; retired Upwork discovery
entry points fail closed. Server deployment is separate from local implementation.

## Deliver results

Create a temporary JSON batch from actual official plugin responses:

```json
{
  "source": "upwork_official_mcp",
  "account_role": "Freelancer",
  "items": [{
    "kind": "connects",
    "observed_at": "COPY_REAL_OBSERVATION_TIME_RFC3339",
    "evidence": {
      "tool": "get_freelancer_financials",
      "action": "connects_balance",
      "trace_id": "COPY_REAL_TRACE_ID",
      "result": {"status": "ok", "trace_id": "COPY_REAL_TRACE_ID", "balance": {"connectsBalance": 31}}
    }
  }]
}
```

This is a schema illustration, not current evidence. Supported kinds: job, draft,
connects, proposal_snapshot, report. Job/draft items require full find_jobs/get evidence,
active status and application eligibility. Drafts also require draft.cover_letter,
optional screening_answers/fit_warning, and proposed_terms for review. The local quality
gate runs without AI calls. Reports cannot change application pipeline status.

Preview (no network):
```bash
python official_bridge.py --input /private/tmp/upwork-official-batch.json
```
Deliver with the existing Telegram/tracker environment:
```bash
python official_bridge.py --input /private/tmp/upwork-official-batch.json --deliver
```
Alternatively put the batch into the GitHub secret OFFICIAL_RELAY_BATCH, then dispatch notifier.yml
with deliver_results=true. Never put account data in public workflow inputs. Existing
Telegram/tracker secrets are reused; plugin credentials are never copied. The receipt
cache is encrypted using OFFICIAL_RELAY_STATE_KEY and contains no plaintext account data.
Delete the temporary batch secret after delivery.
Bootstrap its delivery ledger once with initialize_delivery_state=true; subsequent
missing ledgers fail closed. Normal regression checks run offline only.

## Delivery and retention

SQLite claims each part before sending and records Telegram message IDs. A crash/timeout
stays sending/uncertain and is never blindly retried. Reconcile in Telegram before any
retry. Tracker receipts are independent; never claim a mirror without one. The delivery
ledger retains identities/receipts for 30 days, not marketplace listings. Raw batches
are temporary, never committed or uploaded as artifacts. Job/button details expire in
26 hours. Drafts never count as submitted applications.

## Checks
```bash
python -m unittest -q
```
Tests do not access Upwork, generate paid AI or send notifications. Legacy live-sample
proposal QA cannot fetch listings; provide local samples instead.
