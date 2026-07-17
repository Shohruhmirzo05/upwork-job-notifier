import './styles.css';
import '@phosphor-icons/web/regular';

type Tab = 'inbox' | 'applications' | 'stats';
type Status = 'new' | 'generated' | 'applied' | 'viewed' | 'replied' | 'interview' | 'won' | 'lost' | 'skipped';

interface Job {
  cipher: string;
  title: string;
  description: string;
  skills: string[];
  matched: string[];
  budget: string;
  link: string;
  publish_time?: string;
  score: number;
  tier: string;
  proposal: string;
  hook_type: string;
  screening: Array<{ question?: string; answer?: string }>;
  status: Status;
  applied_confirmed: boolean;
  tags: string[];
  notes: string;
  updated_at: string;
  notified_at?: string;
}

interface Stats {
  totals: Record<string, number>;
  funnel: Record<string, number>;
  hooks: Array<Record<string, string | number>>;
  weekly: Array<Record<string, string | number>>;
}

const root = document.querySelector<HTMLDivElement>('#app')!;
let tab: Tab = 'inbox';
let jobs: Job[] = [];
let selected: Job | null = null;
let authenticated = false;
let query = '';
let loading = false;
let debounce = 0;

const statusLabel: Record<Status, string> = {
  new: 'New', generated: 'Likely applied', applied: 'Applied', viewed: 'Viewed', replied: 'Replied',
  interview: 'Interview', won: 'Won', lost: 'Lost', skipped: "Didn't apply",
};

const statusIcon: Record<Status, string> = {
  new: 'ph-inbox', generated: 'ph-file-text', applied: 'ph-paper-plane-tilt', viewed: 'ph-eye',
  replied: 'ph-chat-circle-dots', interview: 'ph-video-camera', won: 'ph-trophy',
  lost: 'ph-x-circle', skipped: 'ph-skip-forward',
};

const selectableStatuses: Array<{ value: Status; label: string }> = [
  { value: 'new', label: 'Back to inbox' },
  { value: 'applied', label: 'Applied' },
  { value: 'viewed', label: 'Viewed by client' },
  { value: 'replied', label: 'Client replied' },
  { value: 'interview', label: 'Interview' },
  { value: 'won', label: 'Won' },
  { value: 'lost', label: 'Lost' },
  { value: 'skipped', label: "Didn't apply" },
];

function nextStatus(job: Job): { value: Status; label: string; icon: string } | null {
  const next: Partial<Record<Status, { value: Status; label: string; icon: string }>> = {
    new: { value: 'applied', label: 'Mark applied', icon: 'ph-paper-plane-tilt' },
    generated: { value: 'applied', label: 'Confirm applied', icon: 'ph-paper-plane-tilt' },
    applied: { value: 'viewed', label: 'Client viewed', icon: 'ph-eye' },
    viewed: { value: 'replied', label: 'Mark replied', icon: 'ph-chat-circle-dots' },
    replied: { value: 'interview', label: 'Got interview', icon: 'ph-video-camera' },
    interview: { value: 'won', label: 'Mark won', icon: 'ph-trophy' },
    lost: { value: 'applied', label: 'Reopen as applied', icon: 'ph-arrow-counter-clockwise' },
    skipped: { value: 'applied', label: 'Reopen as applied', icon: 'ph-arrow-counter-clockwise' },
  };
  return next[job.status] ?? null;
}

function esc(value: unknown): string {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]!);
}

function timeAgo(value?: string): string {
  if (!value) return '';
  const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 3600) return `${Math.max(1, Math.floor(seconds / 60))}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { 'content-type': 'application/json', ...(options.headers ?? {}) },
  });
  if (response.status === 401) {
    authenticated = false;
    renderLogin();
    throw new Error('Please sign in again');
  }
  const body = await response.json() as T & { error?: string };
  if (!response.ok) throw new Error(body.error || 'Something went wrong');
  return body;
}

function toast(message: string): void {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = message;
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  window.setTimeout(() => { el.classList.remove('show'); window.setTimeout(() => el.remove(), 250); }, 2300);
}

function renderLogin(): void {
  root.innerHTML = `
    <main class="login-shell">
      <section class="login-card">
        <div class="brand-mark" aria-hidden="true"><i class="ph ph-chart-line-up"></i></div>
        <p class="eyebrow">FERA TECH · PRIVATE WORKSPACE</p>
        <h1>Proposal Radar</h1>
        <p class="login-copy">One quiet place for every opportunity, proposal, reply, and win.</p>
        <form id="login-form">
          <label for="password">Password</label>
          <div class="password-row"><input id="password" name="password" type="password" autocomplete="current-password" required autofocus placeholder="Enter your password"><button type="submit">Sign in</button></div>
          <p id="login-error" class="error" role="alert"></p>
        </form>
      </section>
    </main>`;
  document.querySelector<HTMLFormElement>('#login-form')!.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const button = form.querySelector('button')!;
    const error = form.querySelector<HTMLElement>('#login-error')!;
    button.textContent = 'Signing in…'; button.disabled = true; error.textContent = '';
    try {
      const password = new FormData(form).get('password');
      const response = await fetch('/api/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ password }) });
      if (!response.ok) throw new Error('That password is not correct.');
      authenticated = true;
      await loadJobs();
    } catch (reason) {
      error.textContent = reason instanceof Error ? reason.message : 'Could not sign in.';
      button.textContent = 'Sign in'; button.disabled = false;
    }
  });
}

function shell(content: string): string {
  const nav = (value: Tab, icon: string, label: string, badge = '') => `
    <button class="nav-item ${tab === value ? 'active' : ''}" data-tab="${value}">
      <span class="nav-icon">${icon}</span><span>${label}</span>${badge}
    </button>`;
  return `
    <div class="app-shell">
      <aside class="sidebar">
        <div class="brand"><div class="brand-mark small"><i class="ph ph-chart-line-up"></i></div><div><strong>Proposal Radar</strong><small>FERA TECH</small></div></div>
        <nav>
          ${nav('inbox', inboxIcon, 'Job inbox')}
          ${nav('applications', applicationsIcon, 'Applications')}
          ${nav('stats', statsIcon, 'Performance')}
        </nav>
        <div class="side-note"><span class="live-dot"></span>Automation connected</div>
        <button class="logout" id="logout">Sign out</button>
      </aside>
      <main class="workspace">${content}</main>
      <nav class="mobile-nav">
        ${nav('inbox', inboxIcon, 'Inbox')}
        ${nav('applications', applicationsIcon, 'Applied')}
        ${nav('stats', statsIcon, 'Stats')}
      </nav>
    </div>`;
}

const inboxIcon = '<i class="ph ph-inbox"></i>';
const applicationsIcon = '<i class="ph ph-clipboard-text"></i>';
const statsIcon = '<i class="ph ph-chart-bar"></i>';

function bindShell(): void {
  document.querySelectorAll<HTMLButtonElement>('[data-tab]').forEach(button => button.addEventListener('click', () => {
    const next = button.dataset.tab as Tab;
    if (next === tab) return;
    tab = next; selected = null; query = '';
    if (tab === 'stats') void loadStats(); else void loadJobs();
  }));
  document.querySelector<HTMLButtonElement>('#logout')?.addEventListener('click', async () => {
    await fetch('/api/logout', { method: 'POST' }); authenticated = false; renderLogin();
  });
}

async function loadJobs(): Promise<void> {
  if (!authenticated) return;
  loading = true;
  renderJobs();
  try {
    const result = await api<{ jobs: Job[] }>(`/api/jobs?tab=${tab}&q=${encodeURIComponent(query)}`);
    jobs = result.jobs;
  } catch (reason) {
    if (authenticated) toast(reason instanceof Error ? reason.message : 'Could not load jobs');
  } finally {
    loading = false;
    if (authenticated) renderJobs();
  }
}

function jobCard(job: Job): string {
  const chips = [...(job.matched ?? []), ...(job.skills ?? [])].filter(Boolean).slice(0, 3);
  const next = nextStatus(job);
  return `
    <article class="job-card" data-cipher="${esc(job.cipher)}" tabindex="0">
      <div class="card-top">
        <span class="status status-${esc(job.status)}"><i></i>${esc(statusLabel[job.status])}</span>
        <span class="age">${esc(timeAgo(job.publish_time || job.updated_at))}</span>
      </div>
      <h3>${esc(job.title)}</h3>
      <div class="job-meta"><strong>${esc(job.budget || 'Budget not listed')}</strong>${job.score ? `<span>Match ${job.score}</span>` : ''}</div>
      <div class="chips">${chips.map(chip => `<span>${esc(chip)}</span>`).join('')}</div>
      <div class="card-status-controls">
        ${next ? `<button class="card-next" data-card-status="${next.value}" data-cipher="${esc(job.cipher)}"><i class="ph ${next.icon}"></i><span>${next.label}</span></button>` : `<span class="card-complete"><i class="ph ph-check-circle"></i> Pipeline complete</span>`}
        <label class="card-status-select">
          <i class="ph ph-sliders-horizontal"></i>
          <select data-card-select data-cipher="${esc(job.cipher)}" aria-label="Change status for ${esc(job.title)}">
            <option value="">Change status</option>
            ${selectableStatuses.map(option => `<option value="${option.value}" ${option.value === job.status ? 'disabled' : ''}>${esc(option.label)}</option>`).join('')}
          </select>
          <i class="ph ph-caret-down"></i>
        </label>
      </div>
      ${job.tags?.length ? `<div class="card-tags">${job.tags.map(tag => `<span>#${esc(tag)}</span>`).join('')}</div>` : ''}
      <span class="card-arrow"><i class="ph ph-caret-right"></i></span>
    </article>`;
}

function renderJobs(): void {
  const isApps = tab === 'applications';
  root.innerHTML = shell(`
    <header class="page-header">
      <div><p class="eyebrow">${isApps ? 'PIPELINE' : 'OPPORTUNITIES'}</p><h1>${isApps ? 'Applications' : 'Job inbox'}</h1><p>${isApps ? 'Confirm what you sent, then move replies toward a win.' : 'Every matching job delivered by your Telegram automation.'}</p></div>
      <div class="header-stat"><strong>${jobs.length}</strong><span>${isApps ? 'in pipeline' : 'waiting'}</span></div>
    </header>
    <section class="search-wrap">
      <i class="ph ph-magnifying-glass" aria-hidden="true"></i>
      <input id="search" type="search" value="${esc(query)}" placeholder="Search title, ID, proposal — or paste a Telegram message" autocomplete="off">
      ${query ? '<button id="clear-search" aria-label="Clear search"><i class="ph ph-x"></i></button>' : ''}
    </section>
    <section class="list-head"><span>${isApps ? 'Your pipeline' : 'Latest matches'}</span><button id="refresh">Refresh</button></section>
    <section class="job-list ${loading ? 'is-loading' : ''}">
      ${loading ? skeletons() : jobs.length ? jobs.map(jobCard).join('') : emptyState(isApps)}
    </section>
  `);
  bindShell();
  const search = document.querySelector<HTMLInputElement>('#search');
  search?.addEventListener('input', () => {
    query = search.value;
    window.clearTimeout(debounce);
    debounce = window.setTimeout(() => void loadJobs(), 320);
  });
  document.querySelector('#clear-search')?.addEventListener('click', () => { query = ''; void loadJobs(); });
  document.querySelector('#refresh')?.addEventListener('click', () => void loadJobs());
  document.querySelectorAll<HTMLElement>('.job-card').forEach(card => {
    card.addEventListener('click', event => {
      if ((event.target as HTMLElement).closest('button, select, label, details, summary')) return;
      const job = jobs.find(item => item.cipher === card.dataset.cipher);
      if (job) openJob(job);
    });
    card.addEventListener('keydown', event => { if (event.key === 'Enter') card.click(); });
  });
  document.querySelectorAll<HTMLButtonElement>('[data-card-status]').forEach(button => button.addEventListener('click', () => {
    button.disabled = true;
    void setStatus(button.dataset.cipher!, button.dataset.cardStatus as Status);
  }));
  document.querySelectorAll<HTMLInputElement>('[data-card-select]').forEach(select => select.addEventListener('change', () => {
    if (!select.value) return;
    select.disabled = true;
    void setStatus(select.dataset.cipher!, select.value as Status);
  }));
}

function skeletons(): string {
  return Array.from({ length: 4 }, () => '<div class="job-card skeleton"><span></span><span></span><span></span></div>').join('');
}

function emptyState(applications: boolean): string {
  return `<div class="empty"><div>${applications ? applicationsIcon : inboxIcon}</div><h3>${query ? 'No matching jobs' : applications ? 'No applications yet' : 'Inbox is clear'}</h3><p>${query ? 'Try fewer words or paste the complete Upwork job link.' : applications ? 'Generated proposals appear here automatically.' : 'New Telegram matches will land here automatically.'}</p></div>`;
}

async function setStatus(cipher: string, status: Status): Promise<void> {
  try {
    const result = await api<{ job: Job }>(`/api/jobs/${encodeURIComponent(cipher)}/status`, { method: 'PATCH', body: JSON.stringify({ status }) });
    selected = result.job;
    toast(status === 'applied' ? 'Counted as applied' : `Moved to ${statusLabel[status]}`);
    await loadJobs();
    if (document.querySelector('.drawer') && selected) openJob(selected);
  } catch (reason) { toast(reason instanceof Error ? reason.message : 'Update failed'); }
}

async function openJob(job: Job): Promise<void> {
  selected = job;
  try {
    const result = await api<{ job: Job; events: Array<Record<string, string>> }>(`/api/jobs/${encodeURIComponent(job.cipher)}`);
    selected = result.job;
    renderDrawer(result.job, result.events);
  } catch (reason) { toast(reason instanceof Error ? reason.message : 'Could not open job'); }
}

function renderDrawer(job: Job, events: Array<Record<string, string>>): void {
  document.querySelector('.drawer-layer')?.remove();
  const layer = document.createElement('div');
  layer.className = 'drawer-layer';
  const mainStatuses: Status[] = ['applied', 'viewed', 'replied', 'interview', 'won'];
  const screening = (job.screening ?? []).map(item => `
    <div class="qa"><strong>${esc(item.question || 'Screening question')}</strong><p>${esc(item.answer || '')}</p><button class="copy" data-copy="${esc(item.answer || '')}">Copy answer</button></div>`).join('');
  layer.innerHTML = `
    <div class="drawer-backdrop" data-close></div>
    <aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
      <div class="drawer-topbar"><div class="drawer-handle"></div><button class="drawer-close" data-close aria-label="Close"><i class="ph ph-x"></i></button></div>
      <div class="drawer-scroll">
        <div class="drawer-title-row"><span class="status status-${esc(job.status)}"><i></i>${esc(statusLabel[job.status])}</span><span>${esc(timeAgo(job.publish_time || job.updated_at))}</span></div>
        <h2 id="drawer-title">${esc(job.title)}</h2>
        <div class="drawer-meta"><strong>${esc(job.budget || 'Budget not listed')}</strong><span>Match ${esc(job.score || '—')}</span><span>${esc(job.tier || '')}</span></div>
        <div class="status-control-head"><strong>Update status</strong><span>Current: ${esc(statusLabel[job.status])}</span></div>
        <div class="status-actions">
          ${mainStatuses.map(value => `<button data-status="${value}" aria-pressed="${value === job.status}" class="status-option ${value === job.status ? 'current' : ''}" ${value === job.status ? 'disabled' : ''}><i class="ph ${statusIcon[value]}"></i><span>${esc(statusLabel[value])}</span>${value === job.status ? '<small>Current</small>' : ''}</button>`).join('')}
          <details class="status-more ${job.status === 'lost' || job.status === 'skipped' ? 'current' : ''}">
            <summary><i class="ph ph-dots-three-circle"></i><span>More</span>${job.status === 'lost' || job.status === 'skipped' ? '<small>Current</small>' : ''}</summary>
            <div class="status-more-menu">
              <button data-status="lost" ${job.status === 'lost' ? 'disabled' : ''}><i class="ph ph-x-circle"></i><span>Lost</span></button>
              <button data-status="skipped" ${job.status === 'skipped' ? 'disabled' : ''}><i class="ph ph-skip-forward"></i><span>Didn't apply</span></button>
            </div>
          </details>
        </div>
        ${job.link ? `<a class="upwork-link" href="${esc(job.link)}" target="_blank" rel="noopener">Open on Upwork <i class="ph ph-arrow-up-right"></i></a>` : ''}
        <section class="drawer-section"><div class="section-title"><h3>Job brief</h3></div><p class="description">${esc(job.description || 'No description stored.')}</p><div class="chips">${(job.skills ?? []).map(skill => `<span>${esc(skill)}</span>`).join('')}</div></section>
        ${job.proposal ? `<section class="drawer-section proposal"><div class="section-title"><h3>Proposal</h3><div><span class="hook">${esc(job.hook_type)}</span><button class="copy" data-copy="${esc(job.proposal)}">Copy</button></div></div><pre>${esc(job.proposal)}</pre></section>` : ''}
        ${screening ? `<section class="drawer-section"><div class="section-title"><h3>Screening answers</h3></div>${screening}</section>` : ''}
        <section class="drawer-section"><div class="section-title"><h3>Notes & labels</h3><button id="save-details" class="save">Save</button></div><label class="field-label" for="tags">Labels <small>comma-separated</small></label><input id="tags" value="${esc((job.tags ?? []).join(', '))}" placeholder="high-value, follow-up"><label class="field-label" for="notes">Notes</label><textarea id="notes" placeholder="Client context, follow-up date, conversation notes…">${esc(job.notes || '')}</textarea></section>
        ${events.length ? `<section class="drawer-section timeline"><div class="section-title"><h3>History</h3></div>${events.slice(0, 8).map(event => `<div><i></i><p><strong>${esc(event.event_type?.replaceAll('_', ' '))}</strong><span>${event.from_status && event.to_status && event.from_status !== event.to_status ? `${esc(event.from_status)} → ${esc(event.to_status)}` : ''}</span></p><time>${esc(timeAgo(event.created_at))}</time></div>`).join('')}</section>` : ''}
        <div class="drawer-safe"></div>
      </div>
    </aside>`;
  document.body.appendChild(layer);
  layer.querySelector<HTMLElement>('.drawer-scroll')!.scrollTop = 0;
  requestAnimationFrame(() => layer.classList.add('open'));
  layer.querySelectorAll('[data-close]').forEach(el => el.addEventListener('click', () => closeDrawer(layer)));
  layer.querySelectorAll<HTMLButtonElement>('[data-status]').forEach(button => button.addEventListener('click', () => void setStatus(job.cipher, button.dataset.status as Status)));
  layer.querySelectorAll<HTMLButtonElement>('[data-copy]').forEach(button => button.addEventListener('click', async () => {
    await navigator.clipboard.writeText(button.dataset.copy || ''); toast('Copied');
  }));
  layer.querySelector('#save-details')?.addEventListener('click', async () => {
    const notes = layer.querySelector<HTMLTextAreaElement>('#notes')!.value;
    const tags = layer.querySelector<HTMLInputElement>('#tags')!.value.split(',').map(value => value.trim()).filter(Boolean);
    try {
      const result = await api<{ job: Job }>(`/api/jobs/${encodeURIComponent(job.cipher)}/details`, { method: 'PATCH', body: JSON.stringify({ notes, tags }) });
      selected = result.job; toast('Notes saved');
    } catch (reason) { toast(reason instanceof Error ? reason.message : 'Could not save'); }
  });
}

function closeDrawer(layer: Element): void {
  layer.classList.remove('open'); selected = null; window.setTimeout(() => layer.remove(), 220);
}

async function loadStats(): Promise<void> {
  root.innerHTML = shell('<div class="stats-loading">Loading performance…</div>'); bindShell();
  try {
    const data = await api<Stats>('/api/stats'); renderStats(data);
  } catch (reason) { if (authenticated) toast(reason instanceof Error ? reason.message : 'Could not load stats'); }
}

function rate(numerator: number, denominator: number): string {
  return denominator ? `${Math.round(numerator / denominator * 100)}%` : '—';
}

function renderStats(data: Stats): void {
  const f = data.funnel;
  const applied = Number(f.applied ?? 0);
  const funnelItems = [
    ['Applied', applied], ['Viewed', Number(f.viewed ?? 0)], ['Replied', Number(f.replied ?? 0)],
    ['Interview', Number(f.interview ?? 0)], ['Won', Number(f.won ?? 0)],
  ];
  const max = Math.max(applied, 1);
  root.innerHTML = shell(`
    <header class="page-header"><div><p class="eyebrow">FEEDBACK LOOP</p><h1>Performance</h1><p>Know which hooks earn replies—not which ones merely sound good.</p></div></header>
    <section class="metric-grid">
      <article><span>Confirmed applications</span><strong>${applied}</strong><small>${Number(data.totals.likely ?? 0)} still need confirmation</small></article>
      <article><span>Reply rate</span><strong>${rate(Number(f.replied ?? 0), applied)}</strong><small>${Number(f.replied ?? 0)} replies</small></article>
      <article><span>Interview rate</span><strong>${rate(Number(f.interview ?? 0), applied)}</strong><small>${Number(f.interview ?? 0)} interviews</small></article>
      <article class="win"><span>Win rate</span><strong>${rate(Number(f.won ?? 0), applied)}</strong><small>${Number(f.won ?? 0)} contracts won</small></article>
    </section>
    <section class="analytics-grid">
      <article class="panel funnel"><div class="panel-head"><div><p class="eyebrow">CONVERSION</p><h2>Application funnel</h2></div><span>All time</span></div>
        <div class="funnel-list">${funnelItems.map(([label, value]) => `<div><span>${label}</span><div><i style="width:${Math.max(Number(value) / max * 100, Number(value) ? 5 : 0)}%"></i></div><strong>${value}</strong></div>`).join('')}</div>
      </article>
      <article class="panel hooks"><div class="panel-head"><div><p class="eyebrow">A/B SIGNAL</p><h2>Hook performance</h2></div></div>
        ${data.hooks.length ? `<div class="hook-table"><div class="hook-row head"><span>Opening style</span><span>Sent</span><span>Reply</span><span>Interview</span></div>${data.hooks.map(hook => `<div class="hook-row"><strong>${esc(hook.hook_type)}</strong><span>${hook.applied}</span><span>${rate(Number(hook.replied), Number(hook.applied))}</span><span>${rate(Number(hook.interview), Number(hook.applied))}</span></div>`).join('')}</div>` : '<div class="empty mini"><p>Confirm a few applications to begin comparing hook styles.</p></div>'}
      </article>
    </section>
    <section class="insight"><div><i class="ph ph-lightbulb"></i></div><p><strong>Your next useful signal</strong><span>${applied < 10 ? `Track ${10 - applied} more confirmed application${10 - applied === 1 ? '' : 's'} before judging a hook. Small samples are noisy.` : 'Prioritize the hook with the strongest interview rate, then keep testing against one alternative.'}</span></p></section>
  `);
  bindShell();
}

async function boot(): Promise<void> {
  try {
    const response = await fetch('/api/session');
    authenticated = response.ok;
  } catch { authenticated = false; }
  if (authenticated) await loadJobs(); else renderLogin();
}

void boot();
