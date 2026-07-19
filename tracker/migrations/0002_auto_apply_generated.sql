UPDATE jobs
SET status = 'applied',
    applied_confirmed = 1,
    applied_at = COALESCE(applied_at, generated_at, updated_at)
WHERE status = 'generated';
