interface Env {
  DB: D1Database;
  ASSETS: Fetcher;
  ADMIN_PASSWORD: string;
  SESSION_SECRET: string;
  INGEST_TOKEN: string;
}

type JobStatus = 'new' | 'generated' | 'applied' | 'viewed' | 'replied' | 'interview' | 'won' | 'lost' | 'skipped';

const COOKIE_NAME = 'proposal_radar_session';
const SESSION_SECONDS = 30 * 24 * 60 * 60;
const VALID_STATUSES = new Set<JobStatus>(['new', 'generated', 'applied', 'viewed', 'replied', 'interview', 'won', 'lost', 'skipped']);

function json(data: unknown, status = 200, headers: HeadersInit = {}): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', ...headers },
  });
}

async function readJson(request: Request): Promise<Record<string, unknown>> {
  try {
    const value = await request.json();
    return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
  } catch {
    return {};
  }
}

function bytes(value: string): Uint8Array<ArrayBuffer> {
  return new TextEncoder().encode(value) as Uint8Array<ArrayBuffer>;
}

function base64url(input: Uint8Array | string): string {
  const data = typeof input === 'string' ? bytes(input) : input;
  let binary = '';
  for (const byte of data) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function constantTimeEqual(a: string, b: string): boolean {
  const aa = bytes(a);
  const bb = bytes(b);
  let mismatch = aa.length ^ bb.length;
  const length = Math.max(aa.length, bb.length);
  for (let i = 0; i < length; i += 1) mismatch |= (aa[i % aa.length] ?? 0) ^ (bb[i % bb.length] ?? 0);
  return mismatch === 0;
}

async function hmac(value: string, secret: string): Promise<string> {
  const key = await crypto.subtle.importKey('raw', bytes(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return base64url(new Uint8Array(await crypto.subtle.sign('HMAC', key, bytes(value))));
}

async function makeSession(secret: string): Promise<string> {
  const payload = base64url(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + SESSION_SECONDS, nonce: crypto.randomUUID() }));
  return `${payload}.${await hmac(payload, secret)}`;
}

async function validSession(request: Request, secret: string): Promise<boolean> {
  const cookie = request.headers.get('cookie') ?? '';
  const token = cookie.split(';').map(v => v.trim()).find(v => v.startsWith(`${COOKIE_NAME}=`))?.slice(COOKIE_NAME.length + 1);
  if (!token) return false;
  const [payload, signature] = token.split('.');
  if (!payload || !signature || !constantTimeEqual(signature, await hmac(payload, secret))) return false;
  try {
    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/');
    const decoded = JSON.parse(atob(normalized)) as { exp?: number };
    return typeof decoded.exp === 'number' && decoded.exp > Date.now() / 1000;
  } catch {
    return false;
  }
}

function sessionCookie(value: string, maxAge = SESSION_SECONDS): string {
  return `${COOKIE_NAME}=${value}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${maxAge}`;
}

function bearer(request: Request): string {
  const header = request.headers.get('authorization') ?? '';
  return header.startsWith('Bearer ') ? header.slice(7).trim() : '';
}

function stringValue(value: unknown, max = 100_000): string {
  return typeof value === 'string' ? value.slice(0, max) : '';
}

function jsonArray(value: unknown, maxItems = 100): string {
  const array = Array.isArray(value) ? value.slice(0, maxItems) : [];
  return JSON.stringify(array);
}

function publicJob(row: Record<string, unknown>): Record<string, unknown> {
  const parsed = { ...row };
  for (const field of ['skills_json', 'matched_json', 'screening_json', 'tags_json']) {
    const target = field.replace('_json', '');
    try { parsed[target] = JSON.parse(String(parsed[field] ?? '[]')); } catch { parsed[target] = []; }
    delete parsed[field];
  }
  delete parsed.search_rank;
  parsed.applied_confirmed = Boolean(parsed.applied_confirmed);
  return parsed;
}

function searchTokens(value: string): string[] {
  const stopWords = new Set([
    'and', 'are', 'for', 'from', 'have', 'job', 'need', 'the', 'this', 'with', 'you', 'your',
    'application', 'description', 'details', 'proposal', 'telegram', 'upwork',
  ]);
  return [...new Set(value.normalize('NFKC').toLocaleLowerCase().match(/[\p{L}\p{N}+#._~-]{2,}/gu) ?? [])]
    .filter(token => !stopWords.has(token))
    .slice(0, 18);
}

function validDate(value: string | null): value is string {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

function classifyHook(proposal: string): string {
  const opening = proposal.replace(/^\s*Hi,?\s*/i, '').split(/\n\s*\n/)[0].toLowerCase();
  if (/\b(shipped|built|published|live|app store|production)\b/.test(opening)) return 'proof-led';
  if (/\b(risk|bottleneck|failure|issue|problem|before|depends on)\b/.test(opening)) return 'diagnostic';
  if (/\b(first|start|milestone|phase|plan|week|day one)\b/.test(opening)) return 'plan-led';
  return 'outcome-led';
}

async function login(request: Request, env: Env): Promise<Response> {
  const body = await readJson(request);
  const password = stringValue(body.password, 500);
  if (!password || !constantTimeEqual(await hmac(password, env.SESSION_SECRET), await hmac(env.ADMIN_PASSWORD, env.SESSION_SECRET))) {
    await new Promise(resolve => setTimeout(resolve, 450));
    return json({ error: 'Invalid password' }, 401);
  }
  return json({ ok: true }, 200, { 'set-cookie': sessionCookie(await makeSession(env.SESSION_SECRET)) });
}

async function ingest(request: Request, env: Env): Promise<Response> {
  if (!env.INGEST_TOKEN || !constantTimeEqual(bearer(request), env.INGEST_TOKEN)) return json({ error: 'Unauthorized' }, 401);
  const body = await readJson(request);
  const job = body.job && typeof body.job === 'object' ? body.job as Record<string, unknown> : {};
  const cipher = stringValue(job.cipher, 200);
  if (!cipher) return json({ error: 'job.cipher is required' }, 400);

  const event = stringValue(body.event, 40) || 'notified';
  const proposal = stringValue(body.proposal, 30_000);
  const hookType = stringValue(body.hook_type, 40) || (proposal ? classifyHook(proposal) : 'unclassified');
  const screening = Array.isArray(body.screening) ? body.screening : [];
  const isGenerated = event === 'proposal_generated' && Boolean(proposal);
  const now = new Date().toISOString();

  const current = await env.DB.prepare('SELECT status FROM jobs WHERE cipher = ?').bind(cipher).first<{ status: JobStatus }>();
  const prior = current?.status ?? null;
  const next = isGenerated && (!prior || prior === 'new' || prior === 'skipped') ? 'applied' : prior ?? 'new';
  const autoApplied = isGenerated && next === 'applied';

  await env.DB.batch([
    env.DB.prepare(`
      INSERT INTO jobs (
        cipher,title,description,skills_json,matched_json,budget,link,publish_time,score,tier,
        proposal,hook_type,screening_json,status,applied_confirmed,notified_at,generated_at,applied_at,created_at,updated_at
      ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(cipher) DO UPDATE SET
        title=CASE WHEN excluded.title != '' THEN excluded.title ELSE jobs.title END,
        description=CASE WHEN excluded.description != '' THEN excluded.description ELSE jobs.description END,
        skills_json=CASE WHEN excluded.skills_json != '[]' THEN excluded.skills_json ELSE jobs.skills_json END,
        matched_json=CASE WHEN excluded.matched_json != '[]' THEN excluded.matched_json ELSE jobs.matched_json END,
        budget=CASE WHEN excluded.budget != '' THEN excluded.budget ELSE jobs.budget END,
        link=CASE WHEN excluded.link != '' THEN excluded.link ELSE jobs.link END,
        publish_time=COALESCE(excluded.publish_time,jobs.publish_time),
        score=CASE WHEN excluded.score != 0 THEN excluded.score ELSE jobs.score END,
        tier=CASE WHEN excluded.tier != '' THEN excluded.tier ELSE jobs.tier END,
        proposal=CASE WHEN excluded.proposal != '' THEN excluded.proposal ELSE jobs.proposal END,
        hook_type=CASE WHEN excluded.proposal != '' THEN excluded.hook_type ELSE jobs.hook_type END,
        screening_json=CASE WHEN excluded.screening_json != '[]' THEN excluded.screening_json ELSE jobs.screening_json END,
        status=CASE WHEN jobs.status IN ('new','skipped') AND excluded.status='applied' THEN 'applied' ELSE jobs.status END,
        applied_confirmed=CASE WHEN jobs.status IN ('new','skipped') AND excluded.status='applied' THEN 1 ELSE jobs.applied_confirmed END,
        notified_at=COALESCE(jobs.notified_at,excluded.notified_at),
        generated_at=COALESCE(excluded.generated_at,jobs.generated_at),
        applied_at=COALESCE(jobs.applied_at,excluded.applied_at),
        updated_at=excluded.updated_at
    `).bind(
      cipher, stringValue(job.title, 500), stringValue(job.description), jsonArray(job.skills),
      jsonArray(job.matched), stringValue(job.budget, 200), stringValue(job.link, 1000),
      stringValue(job.publish_time, 100) || null, Number(job.score) || 0, stringValue(job.tier, 100),
      proposal, hookType, jsonArray(screening), next, autoApplied ? 1 : 0, event === 'notified' ? now : null,
      isGenerated ? now : null, autoApplied ? now : null, now, now,
    ),
    env.DB.prepare('INSERT INTO events (job_cipher,event_type,from_status,to_status,metadata_json) VALUES (?,?,?,?,?)')
      .bind(cipher, event, prior, next, JSON.stringify({ source: 'notifier' })),
  ]);
  return json({ ok: true, cipher, status: next });
}

async function listJobs(url: URL, env: Env): Promise<Response> {
  const tab = url.searchParams.get('tab') ?? 'inbox';
  const rawQuery = (url.searchParams.get('q') ?? '').trim().slice(0, 2000);
  const limit = Math.min(Math.max(Number(url.searchParams.get('limit')) || 100, 1), 200);
  const conditions: string[] = [];
  const bindings: unknown[] = [];

  // A search is intentionally global so a job can be found without remembering which tab it is in.
  if (!rawQuery && tab === 'applications') conditions.push("status != 'new'");
  else if (!rawQuery && tab === 'inbox') conditions.push("status = 'new'");

  let searchRank = '';
  if (rawQuery) {
    const cipher = rawQuery.match(/~[A-Za-z0-9_-]{8,}/)?.[0];
    if (cipher) {
      searchRank = 'CASE WHEN cipher = ? THEN 100 ELSE 0 END';
      bindings.push(cipher);
    } else {
      const document = `coalesce(title,'') || ' ' || coalesce(description,'') || ' ' || coalesce(proposal,'') || ' ' ||
        coalesce(link,'') || ' ' || coalesce(cipher,'') || ' ' || coalesce(budget,'') || ' ' || coalesce(tier,'') || ' ' ||
        coalesce(hook_type,'') || ' ' || coalesce(status,'') || ' ' || coalesce(skills_json,'') || ' ' ||
        coalesce(matched_json,'') || ' ' || coalesce(screening_json,'') || ' ' || coalesce(tags_json,'') || ' ' || coalesce(notes,'')`;
      const tokens = searchTokens(rawQuery);
      if (tokens.length) {
        const exact = rawQuery.normalize('NFKC').toLocaleLowerCase().slice(0, 500);
        searchRank = `CASE WHEN instr(lower(${document}), ?) > 0 THEN 20 ELSE 0 END + ${tokens
          .map(() => `CASE WHEN instr(lower(${document}), ?) > 0 THEN 1 ELSE 0 END`).join(' + ')}`;
        bindings.push(exact, ...tokens);
      } else {
        searchRank = `CASE WHEN instr(lower(${document}), ?) > 0 THEN 1 ELSE 0 END`;
        bindings.push(rawQuery.normalize('NFKC').toLocaleLowerCase());
      }
    }
  }

  const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
  const sql = searchRank
    ? `SELECT * FROM (SELECT jobs.*, (${searchRank}) search_rank FROM jobs ${where}) WHERE search_rank > 0 ORDER BY search_rank DESC, updated_at DESC LIMIT ?`
    : `SELECT * FROM jobs ${where} ORDER BY updated_at DESC LIMIT ?`;
  const result = await env.DB.prepare(sql).bind(...bindings, limit).all<Record<string, unknown>>();
  return json({ jobs: (result.results ?? []).map(publicJob), count: result.results?.length ?? 0 });
}

async function getJob(cipher: string, env: Env): Promise<Response> {
  const row = await env.DB.prepare('SELECT * FROM jobs WHERE cipher = ?').bind(cipher).first<Record<string, unknown>>();
  if (!row) return json({ error: 'Not found' }, 404);
  const events = await env.DB.prepare('SELECT * FROM events WHERE job_cipher = ? ORDER BY created_at DESC LIMIT 50').bind(cipher).all();
  return json({ job: publicJob(row), events: events.results ?? [] });
}

async function updateStatus(cipher: string, request: Request, env: Env): Promise<Response> {
  const body = await readJson(request);
  const status = stringValue(body.status, 30) as JobStatus;
  if (!VALID_STATUSES.has(status)) return json({ error: 'Invalid status' }, 400);
  const current = await env.DB.prepare('SELECT status FROM jobs WHERE cipher = ?').bind(cipher).first<{ status: JobStatus }>();
  if (!current) return json({ error: 'Not found' }, 404);
  const now = new Date().toISOString();
  const timestampColumn: Partial<Record<JobStatus, string>> = {
    applied: 'applied_at', viewed: 'viewed_at', replied: 'replied_at', interview: 'interview_at',
    won: 'won_at', lost: 'lost_at', skipped: 'skipped_at', generated: 'generated_at',
  };
  const column = timestampColumn[status];
  const confirmed = ['applied', 'viewed', 'replied', 'interview', 'won', 'lost'].includes(status) ? 1 : 0;
  const statements = [
    env.DB.prepare(`UPDATE jobs SET status=?, applied_confirmed=?, updated_at=?${column ? `, ${column}=COALESCE(${column},?)` : ''} WHERE cipher=?`)
      .bind(status, confirmed, now, ...(column ? [now] : []), cipher),
    env.DB.prepare('INSERT INTO events (job_cipher,event_type,from_status,to_status,metadata_json) VALUES (?,?,?,?,?)')
      .bind(cipher, 'status_changed', current.status, status, '{}'),
  ];
  await env.DB.batch(statements);
  return getJob(cipher, env);
}

async function updateDetails(cipher: string, request: Request, env: Env): Promise<Response> {
  const body = await readJson(request);
  const notes = stringValue(body.notes, 20_000);
  const tags = Array.isArray(body.tags) ? body.tags.map(v => String(v).trim()).filter(Boolean).slice(0, 20) : [];
  const screening = Array.isArray(body.screening) ? body.screening : undefined;
  const result = await env.DB.prepare(`UPDATE jobs SET notes=?, tags_json=?, screening_json=COALESCE(?,screening_json), updated_at=? WHERE cipher=?`)
    .bind(notes, JSON.stringify(tags), screening ? JSON.stringify(screening) : null, new Date().toISOString(), cipher).run();
  if (!result.meta.changes) return json({ error: 'Not found' }, 404);
  return getJob(cipher, env);
}

async function stats(url: URL, env: Env): Promise<Response> {
  const from = url.searchParams.get('from');
  const to = url.searchParams.get('to');
  if ((from && !validDate(from)) || (to && !validDate(to)) || (from && to && from > to)) {
    return json({ error: 'Invalid date range' }, 400);
  }
  const rangeConditions = ['applied_confirmed=1'];
  const rangeBindings: string[] = [];
  if (from) { rangeConditions.push('date(COALESCE(applied_at,generated_at,updated_at)) >= date(?)'); rangeBindings.push(from); }
  if (to) { rangeConditions.push('date(COALESCE(applied_at,generated_at,updated_at)) <= date(?)'); rangeBindings.push(to); }
  const appliedWhere = rangeConditions.join(' AND ');
  const bindRange = (query: string) => env.DB.prepare(query).bind(...rangeBindings);
  const [funnel, hooks, weekly, totals] = await Promise.all([
    bindRange(`SELECT
      COUNT(*) applied,
      SUM(CASE WHEN status IN ('viewed','replied','interview','won') THEN 1 ELSE 0 END) viewed,
      SUM(CASE WHEN status IN ('replied','interview','won') THEN 1 ELSE 0 END) replied,
      SUM(CASE WHEN status IN ('interview','won') THEN 1 ELSE 0 END) interview,
      SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) won
      FROM jobs WHERE ${appliedWhere}`).first(),
    bindRange(`SELECT hook_type,
      COUNT(*) applied,
      SUM(CASE WHEN status IN ('replied','interview','won') THEN 1 ELSE 0 END) replied,
      SUM(CASE WHEN status IN ('interview','won') THEN 1 ELSE 0 END) interview,
      SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) won
      FROM jobs WHERE ${appliedWhere} AND hook_type != 'unclassified' GROUP BY hook_type ORDER BY applied DESC`).all(),
    bindRange(`SELECT substr(COALESCE(applied_at,generated_at,updated_at),1,10) day,
      COUNT(*) applied,
      SUM(CASE WHEN status IN ('replied','interview','won') THEN 1 ELSE 0 END) replies
      FROM jobs WHERE ${appliedWhere}
      GROUP BY day ORDER BY day`).all(),
    bindRange(`SELECT COUNT(*) total, COUNT(*) applied FROM jobs WHERE ${appliedWhere}`).first(),
  ]);
  return json({
    funnel: funnel ?? {}, hooks: hooks.results ?? [], weekly: weekly.results ?? [], totals: totals ?? {},
    range: { from: from ?? null, to: to ?? null },
  });
}

async function api(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  if (request.method === 'POST' && url.pathname === '/api/login') return login(request, env);
  if (request.method === 'POST' && url.pathname === '/api/ingest') return ingest(request, env);
  if (request.method === 'POST' && url.pathname === '/api/logout') return json({ ok: true }, 200, { 'set-cookie': sessionCookie('', 0) });
  if (!(await validSession(request, env.SESSION_SECRET))) return json({ error: 'Unauthorized' }, 401);
  if (request.method === 'GET' && url.pathname === '/api/session') return json({ authenticated: true });
  if (request.method === 'GET' && url.pathname === '/api/jobs') return listJobs(url, env);
  if (request.method === 'GET' && url.pathname === '/api/stats') return stats(url, env);

  const match = url.pathname.match(/^\/api\/jobs\/([^/]+)(?:\/(status|details))?$/);
  if (match) {
    const cipher = decodeURIComponent(match[1]);
    if (request.method === 'GET' && !match[2]) return getJob(cipher, env);
    if (request.method === 'PATCH' && match[2] === 'status') return updateStatus(cipher, request, env);
    if (request.method === 'PATCH' && match[2] === 'details') return updateDetails(cipher, request, env);
  }
  return json({ error: 'Not found' }, 404);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname.startsWith('/api/')) return api(request, env);
    const response = await env.ASSETS.fetch(request);
    const headers = new Headers(response.headers);
    headers.set('x-content-type-options', 'nosniff');
    headers.set('referrer-policy', 'same-origin');
    headers.set('permissions-policy', 'camera=(), microphone=(), geolocation=()');
    headers.set('content-security-policy', "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'");
    return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
  },
} satisfies ExportedHandler<Env>;
