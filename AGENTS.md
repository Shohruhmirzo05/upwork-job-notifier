# Official Upwork result delivery

Read README.md. The owner wants official-plugin account reads and useful results in the
existing owner Telegram bot and private Proposal Radar. Use the personal Freelancer.

- No Upwork browser automation, scraping, visitor tokens, private GraphQL, proxy fallback,
  access-control bypass or repeated background marketplace polling. Never revive the
  deleted upwork-mobile-job-checks automation. Honor current MCP policy and Retry-After.
- The host plugin connection is not bot authentication. Never extract OAuth tokens,
  cookies or secrets. Official reads run in the host app; official_bridge.py only relays
  bounded results from authorized, on-demand reviews.
- For suitable jobs, read full details, existing proposals and invitations first. Draft
  with profile.md/proposal_prompt.md and the local quality gate. Relay useful leads,
  drafts and qualified statistics with actual official-response evidence.
- No bot submission. Official preview-confirm and explicit approval of the concrete
  proposal and terms are required. No automatic Connects purchase or boost.
- Preview relay output before authorized delivery. Verify Telegram and tracker receipts
  separately. Reconcile uncertain sends; never blindly retry.
- The repository is public. Keep account records, proposal drafts, raw batches and
  plaintext delivery receipts out of commits, workflow inputs/logs and public caches.
  Use temporary OFFICIAL_RELAY_BATCH secret plus deliver_results=true in notifier.yml.
  Receipt caches must be authenticated/encrypted. Reuse OFFICIAL_RELAY_STATE_KEY; never
  overwrite it once configured. Initialize missing receipt state only after reconciling
  any earlier deliveries. Delete the temporary batch secret after the run ends.
- Preserve user edits. Tests must not poll Upwork, invoke paid AI or send externally.
- Accepted means submitted, viewed is not replied/hired. Snapshots are page/status scoped.
  Never synchronize old applications with today's date as their submission date.
- Target mobile apps first, then web/backend, AI voice, RAG, SIP/Asterisk, CRM/API
  integrations and business automation. Claim only verified portfolio experience.
