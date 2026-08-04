-- Proposal generation creates a draft, not proof that it was submitted on Upwork.
-- Preserve every application with a manual funnel event and repair only legacy rows
-- that migration 0002 auto-counted without user confirmation.
UPDATE jobs
SET status = 'generated',
    applied_confirmed = 0,
    applied_at = NULL
WHERE status = 'applied'
  AND proposal != ''
  AND applied_confirmed = 1
  AND NOT EXISTS (
    SELECT 1
    FROM events
    WHERE events.job_cipher = jobs.cipher
      AND events.event_type = 'status_changed'
      AND events.to_status IN ('applied','viewed','replied','interview','won','lost')
  );
