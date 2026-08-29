/**
 * Stagenator dashboard — live view over the agent's Firestore state.
 * Read-mostly; the one write surface is the "message the agent" box.
 * Locked to the owner's Google account (rules enforce it server-side too).
 */
import { useEffect, useMemo, useState } from 'react';
import { onAuthStateChanged, type User } from 'firebase/auth';
import {
  addDoc,
  collection,
  doc,
  limit,
  onSnapshot,
  orderBy,
  query,
  serverTimestamp,
  Timestamp,
  where,
} from 'firebase/firestore';
import { auth, db, gisSignIn, isOwner } from './firebase';
import stegoLogo from './assets/stegosaurus.svg';

type Doc = Record<string, unknown> & { id: string };

function reportListenerError(path: string, err: unknown) {
  // Never fail silently: a permission / App Check / missing-index error otherwise
  // leaves a blank authenticated screen with no clue. Log it AND surface a banner.
  // eslint-disable-next-line no-console
  console.error(`[stagenator] Firestore listener failed for ${path}:`, err);
  const message = (err as { message?: string })?.message ?? String(err);
  window.dispatchEvent(new CustomEvent('sg-listener-error', { detail: { path, message } }));
}

declare const __BUILD_INFO__: string;

// One entry per game the agent runs — label, chart/tab color, store links.
const GAME_META: Record<string, { label: string; color: string; fg: string; appStore: string; play: string }> = {
  'subliminal-words': {
    label: 'Subliminal Words', color: 'var(--sw-c)', fg: 'var(--sw-t)',
    appStore: 'https://apps.apple.com/app/subliminal-words/id6468366578',
    play: 'https://play.google.com/store/apps/details?id=com.indest.subliminalwords',
  },
  'ai-movie-quiz': {
    label: 'AI Movie Quiz', color: '#d9a514', fg: '#1c1917',
    appStore: 'https://apps.apple.com/app/ai-movie-quiz/id6752119990',
    play: 'https://play.google.com/store/apps/details?id=com.indest.aimoviequiz',
  },
  'palindrome': {
    label: 'Palindrome', color: '#0d9488', fg: '#ffffff',
    appStore: 'https://apps.apple.com/app/hah-palindrome-puzzles/id1673006365',
    play: 'https://play.google.com/store/apps/details?id=com.indest.hah',
  },
};
const GAME_KEYS = Object.keys(GAME_META);
// stable per-game ordering for sections fed by backend docs (unknown games last)
const gameOrder = (g: string) => { const i = GAME_KEYS.indexOf(g); return i < 0 ? 99 : i; };
const gameLabel = (g: string) => GAME_META[g]?.label ?? g;

function useCollection(path: string, orderField: string, n = 50): Doc[] {
  const [docs, setDocs] = useState<Doc[]>([]);
  useEffect(() => {
    const q = query(collection(db, path), orderBy(orderField, 'desc'), limit(n));
    return onSnapshot(q,
      (snap) => setDocs(snap.docs.map((d) => ({ id: d.id, ...d.data() }))),
      (err) => reportListenerError(path, err));
  }, [path, orderField, n]);
  return docs;
}

// Ledger entries from the last `hours` hours — its own query so a busy live
// feed (capped at 60 entries) can't push older level ships out of view.
function useLedgerSince(hours: number, n = 400): Doc[] {
  const [docs, setDocs] = useState<Doc[]>([]);
  useEffect(() => {
    const since = Timestamp.fromMillis(Date.now() - hours * 3600_000);
    const q = query(
      collection(db, 'stagenator_ledger'),
      where('ts', '>', since),
      orderBy('ts', 'desc'),
      limit(n),
    );
    return onSnapshot(q,
      (snap) => setDocs(snap.docs.map((d) => ({ id: d.id, ...d.data() }))),
      (err) => reportListenerError('stagenator_ledger(72h)', err));
  }, [hours, n]);
  return docs;
}

function useDoc(path: string): Doc | null {
  const [data, setData] = useState<Doc | null>(null);
  useEffect(() => {
    return onSnapshot(doc(db, path),
      (snap) => setData(snap.exists() ? ({ id: snap.id, ...snap.data() } as Doc) : null),
      (err) => reportListenerError(path, err));
  }, [path]);
  return data;
}

const ts = (v: unknown): string => {
  const d = (v as { toDate?: () => Date })?.toDate?.();
  return d ? d.toLocaleString('en-GB', { hour12: false }) : '';
};
// compact form for section titles — full date+time wraps them onto two lines
const tsShort = (v: unknown): string => {
  const d = (v as { toDate?: () => Date })?.toDate?.();
  return d
    ? d.toLocaleString('en-GB', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
    : '';
};
// feed rows: local time to the millisecond, so two entries in the same second
// (a decision and the rejection it produced) read in the right order
const tsMs = (v: unknown): string => {
  const d = (v as { toDate?: () => Date })?.toDate?.();
  return d
    ? `${d.toLocaleString('en-GB', { hour12: false })}.${String(d.getMilliseconds()).padStart(3, '0')}`
    : '';
};
// hover detail: exact UTC time with milliseconds (the agent reasons in UTC)
const tsUtc = (v: unknown): string => {
  const d = (v as { toDate?: () => Date })?.toDate?.();
  return d ? d.toISOString().replace('T', ' ').replace('Z', ' UTC') : '';
};

function ledgerLine(e: Doc): string {
  const r = (e.result ?? {}) as Record<string, unknown>;
  const parts: string[] = [];
  const push = (v: unknown) => v != null && v !== '' && parts.push(String(v));
  switch (String(e.kind)) {
    case 'signal': {
      push(e.signal);
      if (e.count != null) push(`${e.count} active`);
      const bd = (e.data as Record<string, unknown> | undefined)?.breakdown;
      if (bd && typeof bd === 'object')
        push(Object.entries(bd as Record<string, number>).map(([k, v]) => `${k}:${v}`).join(' · '));
      else if (e.detail && e.detail !== e.signal) push(e.detail);
      break;
    }
    case 'decision':
      if (e.action === 'strategist') push(`${e.enqueued ?? 0} enqueued · ${e.rejected ?? 0} rejected`);
      push(String(e.notes ?? e.reason ?? e.product ?? '').slice(0, 160));
      if (Array.isArray(e.ruled_out) && e.ruled_out.length)
        push(`— skipped: ${(e.ruled_out as string[]).join('; ').slice(0, 160)}`);
      break;
    case 'action': {
      push(e.action); push(e.status);
      // pull the most meaningful result field per action type
      push(r.word ?? r.movie ?? r.palindrome ?? r.published);
      if (r.level != null) push(`#${r.level}`);
      if (r.levelId != null) push(`#${r.levelId}`);
      if (r.qa) push(`qa:${r.qa}`);
      if (r.codes != null) push(`${r.codes} codes`);
      if (typeof r.imported === 'number') push(`imported ${r.imported}`);
      else if (r.imported && typeof r.imported === 'object' && Object.keys(r.imported).length)
        push(`imported ${Object.values(r.imported as Record<string, number>).reduce((a, b) => a + Number(b || 0), 0)}`);
      if (r.minted != null) push(`minted ${r.minted}`);
      if (r.escalated) push('escalated');
      if (e.reason) push(`— ${e.reason}`);
      break;
    }
    case 'rejected':
      push(e.action); push(`✕ ${e.reason}`);
      break;
    case 'error':
      push(e.message);
      if (e.likely_cause) push(`· likely: ${e.likely_cause}`);
      break;
    case 'brief':
      push(String(e.brief ?? '').replace(/#+\s?/g, '').replace(/\n+/g, ' · ').slice(0, 140));
      break;
    case 'outcome':
      push(e.action ?? e.signal);
      push(r.word ?? r.movie ?? r.note ?? r.detail);
      if (r.claimed != null) push(`${r.claimed} claimed`);
      break;
    default:
      push(e.action ?? e.reason);
  }
  return parts.join(' · ') || String(e.action ?? '');
}

function fullEntry(e: Doc): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(e)) {
    if (k === 'id') continue;
    out[k] = v && typeof v === 'object' && 'toDate' in (v as object) ? ts(v) : v;
  }
  return out;
}

const KIND_COLORS: Record<string, string> = {
  signal: 'text-sky-700 dark:text-sky-400 border-sky-400',
  decision: 'text-violet-600 dark:text-violet-400 border-violet-400',
  action: 'text-emerald-600 dark:text-emerald-400 border-emerald-400',
  rejected: 'text-amber-600 dark:text-amber-400 border-amber-400',
  error: 'text-red-600 dark:text-red-400 border-red-400',
  brief: 'text-zinc-700 dark:text-zinc-300 border-zinc-200 dark:border-zinc-800',
  outcome: 'text-teal-600 dark:text-teal-400 border-teal-400',
};

export function App() {
  const [user, setUser] = useState<User | null>(null);
  const [authReady, setAuthReady] = useState(false);
  useEffect(
    () =>
      onAuthStateChanged(auth, (u) => {
        setUser(u);
        setAuthReady(true);
      }),
    [],
  );

  if (!authReady) return <Center>loading…</Center>;
  if (!user) return <SignIn />;

  return <Dashboard owner={isOwner(user)} />;
}

function SignIn() {
  const [error, setError] = useState('');
  const buttonRef = (el: HTMLDivElement | null) => {
    if (el && !el.hasChildNodes()) gisSignIn(el, setError).catch((e) => setError(String(e)));
  };
  return (
    <Center>
      <div className="flex flex-col items-center gap-4">
        <img src={stegoLogo} alt="Stagenator" className="h-24 w-auto" />
        <div className="flex flex-col items-center gap-1">
          <div className="font-display font-bold text-zinc-800 dark:text-zinc-200 text-sm uppercase tracking-widest">Stagenator</div>
          <div className="text-zinc-500 dark:text-zinc-400 text-xs">Pulling the app portfolio out of stagnation</div>
        </div>
        {error && <div className="text-red-600 dark:text-red-400 text-xs max-w-xs text-center">{error}</div>}
        <div ref={buttonRef} />
      </div>
    </Center>
  );
}

function ListenerErrorBanner() {
  const [err, setErr] = useState<{ path: string; message: string } | null>(null);
  useEffect(() => {
    const h = (e: Event) => setErr((e as CustomEvent).detail);
    window.addEventListener('sg-listener-error', h);
    return () => window.removeEventListener('sg-listener-error', h);
  }, []);
  if (!err) return null;
  return (
    <div className="bg-red-50 dark:bg-red-950/40 border border-red-400 text-red-700 dark:text-red-300 rounded-lg px-4 py-2 text-xs flex items-center gap-2">
      <span className="font-bold uppercase tracking-wide">Data error</span>
      <span className="text-red-600 dark:text-red-400">{err.path}: {err.message}</span>
      <span className="text-red-500 dark:text-red-400 ml-auto">check App Check · rules · indexes</span>
    </div>
  );
}

function Center({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex items-center justify-center text-zinc-700 dark:text-zinc-300">{children}</div>
  );
}

function ThemeToggle() {
  const [dark, setDark] = useState(
    () => typeof document !== 'undefined' && document.documentElement.classList.contains('dark'),
  );
  const toggle = () => {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle('dark', next);
    try {
      localStorage.setItem('theme', next ? 'dark' : 'light');
    } catch {
      /* ignore */
    }
  };
  return (
    <button
      onClick={toggle}
      aria-label={dark ? 'Switch to light mode' : 'Switch to dark mode'}
      title={dark ? 'Light mode' : 'Dark mode'}
      className="text-sm leading-none rounded-lg px-2 py-1 hover:bg-zinc-100 dark:hover:bg-zinc-800 transition-colors"
    >
      {dark ? '\u2600\ufe0f' : '\ud83c\udf19'}
    </button>
  );
}


// Circular countdown to the next 5-min pulse: the ring fills as the window
// elapses; amber once the heartbeat is overdue.
function HeartbeatRing({ at }: { at: unknown }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);
  const d = (at as { toDate?: () => Date })?.toDate?.();
  if (!d) return null;
  const PERIOD = 300; // scheduler pulses every 5 min
  const elapsed = (Date.now() - d.getTime()) / 1000;
  const frac = Math.min(1, elapsed / PERIOD);
  const overdue = elapsed > PERIOD + 90; // grace: run duration + clock skew
  const R = 6;
  const C = 2 * Math.PI * R;
  const remain = Math.max(0, PERIOD - elapsed);
  const label = overdue
    ? `pulse overdue — ${Math.round(elapsed / 60)} min since last heartbeat`
    : `next pulse in ~${Math.floor(remain / 60)}:${String(Math.floor(remain % 60)).padStart(2, '0')}`;
  return (
    <span title={label} className="inline-flex items-center align-middle">
      <svg viewBox="0 0 16 16" className="w-4 h-4 -rotate-90">
        <circle cx="8" cy="8" r={R} fill="none" stroke="currentColor" strokeWidth="2.5" opacity="0.15" />
        <circle cx="8" cy="8" r={R} fill="none" strokeWidth="2.5" strokeLinecap="round"
          className={overdue ? 'stroke-amber-500' : 'stroke-emerald-500'}
          strokeDasharray={`${C}`} strokeDashoffset={`${C * (1 - frac)}`} />
      </svg>
    </span>
  );
}

type FeedItem = { type: 'group'; decision: Doc; children: Doc[] } | { type: 'row'; row: Doc };

const rowMs = (r: Doc): number => (r.ts as { toDate?: () => Date })?.toDate?.()?.getTime() ?? 0;
// gate-phase outcomes of a decision (logged within ms of it); a later `done` row is not one
const isGateOutcome = (r: Doc): boolean =>
  r.kind === 'rejected' || (r.kind === 'action' && r.status === 'enqueued');

// Display-only grouping: attach each enqueue/reject row to the decision that produced
// it (same pulse, within a few ms) so the decision reads as a header with its outcomes
// nested beneath — while every row keeps its own real timestamp. Signals and later
// `done` rows stay standalone. No row is ever dropped: anything unattached renders as-is.
function groupFeed(rows: Doc[]): FeedItem[] {
  const decisions = rows.filter((r) => r.kind === 'decision');
  const childOf = new Map<string, string>();
  for (const r of rows) {
    if (!isGateOutcome(r)) continue;
    let best: Doc | null = null;
    let bestDt = Infinity;
    for (const d of decisions) {
      const dt = Math.abs(rowMs(r) - rowMs(d));
      if (dt < bestDt) { bestDt = dt; best = d; }
    }
    if (best && bestDt <= 5000) childOf.set(r.id, best.id);
  }
  const kids = new Map<string, Doc[]>();
  for (const r of rows) {
    const did = childOf.get(r.id);
    if (did) { const a = kids.get(did) ?? []; a.push(r); kids.set(did, a); }
  }
  for (const a of kids.values()) a.sort((x, y) => rowMs(x) - rowMs(y)); // causal order under header
  const items: FeedItem[] = [];
  for (const r of rows) {
    if (childOf.has(r.id)) continue; // nested under its decision
    if (r.kind === 'decision') items.push({ type: 'group', decision: r, children: kids.get(r.id) ?? [] });
    else items.push({ type: 'row', row: r });
  }
  return items;
}

function FeedRow({ e, open, onToggle }: { e: Doc; open: boolean; onToggle: (id: string) => void }) {
  return (
    <div
      onClick={() => onToggle(e.id)}
      className={`border-l-2 pl-3 pr-2.5 py-2 text-xs bg-white dark:bg-zinc-900 rounded-r-lg transition-colors hover:bg-zinc-100 dark:hover:bg-zinc-800 cursor-pointer ${
        KIND_COLORS[String(e.kind)] ?? 'text-zinc-600 dark:text-zinc-400 border-zinc-200 dark:border-zinc-800'
      }`}
    >
      <div className="flex gap-2 items-baseline flex-wrap">
        <span className="text-zinc-600 dark:text-zinc-400">{open ? '▾' : '▸'}</span>
        <span className="uppercase font-bold">{String(e.kind)}</span>
        {e.game ? <span className="text-zinc-600 dark:text-zinc-400">{String(e.game)}</span> : null}
        <span title={tsUtc(e.ts)} className="text-zinc-500 dark:text-zinc-400 ml-auto font-mono text-[10px]">{tsMs(e.ts)}</span>
      </div>
      <div className="text-zinc-600 dark:text-zinc-400 mt-0.5 break-words font-mono text-[11px]">{ledgerLine(e)}</div>
      {open && (
        <pre className="mt-2 font-mono text-[10.5px] leading-relaxed text-zinc-600 dark:text-zinc-400 bg-zinc-100 dark:bg-zinc-800 border border-zinc-200 dark:border-zinc-800 rounded-lg p-2.5 whitespace-pre-wrap break-words overflow-x-auto">
          {JSON.stringify(fullEntry(e), null, 2)}
        </pre>
      )}
    </div>
  );
}

function Dashboard({ owner }: { owner: boolean }) {
  const [feedLimit, setFeedLimit] = useState(60);
  const ledger = useCollection('stagenator_ledger', 'ts', feedLimit);
  const feed = useMemo(() => groupFeed(ledger), [ledger]);
  const [openLog, setOpenLog] = useState<Set<string>>(new Set());
  const toggleLog = (id: string) =>
    setOpenLog((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const tasks = useCollection('stagenator_tasks', 'updated', 40);
  const briefs = useCollection('stagenator_briefs', 'ts', 3);
  const playbook = useDoc('stagenator_playbook/current');
  const heartbeat = useDoc('stagenator_playbook/heartbeat');
  const health = useDoc('stagenator_playbook/health');

  const taskBuckets = useMemo(() => {
    const b: Record<string, Doc[]> = { pending: [], running: [], done: [], dead: [] };
    tasks.forEach((t) => (b[String(t.status)] ?? (b[String(t.status)] = [])).push(t));
    return b;
  }, [tasks]);


  return (
    <div className="min-h-screen text-zinc-800 dark:text-zinc-200 max-w-[1750px] mx-auto flex flex-col gap-5 sm:gap-6 px-4 sm:px-6 pb-12">
      <header className="sticky top-0 z-30 -mx-4 sm:-mx-6 px-4 sm:px-6 py-3.5 bg-white/90 dark:bg-zinc-900/90 backdrop-blur-md border-b border-zinc-200 dark:border-zinc-800 shadow-sm flex items-center justify-between flex-wrap gap-x-4 gap-y-2">
        <div className="flex items-center gap-3">
          <img src={stegoLogo} alt="Stagenator" className="h-11 w-auto shrink-0" />
          <div className="flex flex-col leading-tight">
            <h1 className="font-display font-bold tracking-widest uppercase text-sm text-zinc-900 dark:text-zinc-100">Stagenator</h1>
            <span className="text-[10px] tracking-wide text-zinc-600 dark:text-zinc-400 normal-case">Pulling the app portfolio out of stagnation</span>
          </div>
        </div>
        <div className="flex items-center flex-wrap gap-x-4 gap-y-1 text-[11px] text-zinc-600 dark:text-zinc-400 font-mono">
          <span className="flex items-center gap-1.5">
            <HeartbeatRing at={heartbeat?.at} />
            last run {heartbeat ? ts(heartbeat.at) : '—'}
          </span>
          <span className="flex items-center gap-1.5">
            <span
              className={`inline-block w-2 h-2 rounded-full animate-pulse ${
                !health ? 'bg-zinc-400'
                  : health.status === 'healthy' ? 'bg-emerald-500'
                  : health.status === 'degraded' ? 'bg-amber-500' : 'bg-red-500'
              }`}
            />
            {health
              ? `${String(health.status)}${Number(health.fail) ? ` · ${health.fail} down` : ''}`
              : (taskBuckets.dead.length ? `${taskBuckets.dead.length} dead` : 'no health check yet')}
          </span>
          <ThemeToggle />
        </div>
      </header>

      <ListenerErrorBanner />

      <div className="flex flex-col xl:flex-row gap-5 items-start">
        {/* Ledger feed */}
        <section className="w-full xl:w-[30%] 2xl:w-[26%] xl:shrink-0 xl:sticky xl:top-[84px] flex flex-col gap-2 bg-white/55 dark:bg-white/[0.03] border border-zinc-300/60 dark:border-zinc-800 rounded-2xl p-3.5">
          <SectionTitle accent="bg-sky-500">Activity — live</SectionTitle>
          <div className="flex flex-col gap-2 max-h-[60vh] xl:max-h-[calc(100vh-140px)] overflow-y-auto pr-1">
            {feed.map((it) =>
              it.type === 'row' ? (
                <FeedRow key={it.row.id} e={it.row} open={openLog.has(it.row.id)} onToggle={toggleLog} />
              ) : (
                <div key={it.decision.id} className="flex flex-col gap-1">
                  <FeedRow e={it.decision} open={openLog.has(it.decision.id)} onToggle={toggleLog} />
                  {it.children.length > 0 && (
                    <div className="ml-3 pl-1.5 flex flex-col gap-1 border-l border-dashed border-zinc-300 dark:border-zinc-700">
                      {it.children.map((c) => (
                        <FeedRow key={c.id} e={c} open={openLog.has(c.id)} onToggle={toggleLog} />
                      ))}
                    </div>
                  )}
                </div>
              ),
            )}
            {ledger.length === 0 && <Empty>Nothing yet</Empty>}
            {ledger.length >= feedLimit && (
              <button
                onClick={() => setFeedLimit((n) => n + 60)}
                className="mt-1 py-2 text-[11px] font-bold uppercase tracking-wide text-zinc-500 dark:text-zinc-400 hover:text-zinc-800 dark:hover:text-zinc-100 border border-zinc-200 dark:border-zinc-800 rounded-lg transition-colors"
              >
                Load more
              </button>
            )}
          </div>
        </section>

        {/* Everything else — responsive masonry of cards */}
        <div className="w-full xl:flex-1 xl:min-w-0 columns-1 md:columns-2 xl:columns-3 gap-4 [&>section]:mb-4 [&>section]:break-inside-avoid">
          <section className="bg-white/55 dark:bg-white/[0.03] border border-zinc-300/60 dark:border-zinc-800 rounded-2xl p-3.5">
            <SectionTitle accent="bg-amber-500">Tasks <span className="text-zinc-600 dark:text-zinc-400 normal-case">· 40 most recent</span></SectionTitle>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-center mb-2">
              {(['pending', 'running', 'done', 'dead'] as const).map((s) => (
                <div key={s} className="bg-zinc-50 dark:bg-zinc-900 rounded-lg py-2">
                  <div
                    className={`text-lg font-bold ${
                      s === 'dead' && taskBuckets[s].length ? 'text-red-600 dark:text-red-400' : 'text-zinc-800 dark:text-zinc-200'
                    }`}
                  >
                    {taskBuckets[s]?.length ?? 0}
                  </div>
                  <div className="text-[10px] text-zinc-500 dark:text-zinc-400 uppercase">{s}</div>
                </div>
              ))}
            </div>
            <div className="flex flex-col gap-1 max-h-40 overflow-y-auto">
              {tasks.slice(0, 8).map((t) => (
                <div key={t.id} className="text-[11px] bg-white dark:bg-zinc-900 rounded px-2.5 py-1.5 flex gap-2">
                  <span
                    className={
                      t.status === 'dead'
                        ? 'text-red-600 dark:text-red-400'
                        : t.status === 'done'
                          ? 'text-emerald-600 dark:text-emerald-400'
                          : 'text-amber-600 dark:text-amber-400'
                    }
                  >
                    ●
                  </span>
                  <span className="text-zinc-700 dark:text-zinc-300">{String(t.type)}</span>
                  <span className="text-zinc-500 dark:text-zinc-400">{String(t.game)}</span>
                  <span title={tsUtc(t.updated)} className="text-zinc-600 dark:text-zinc-400 ml-auto font-mono text-[10px]">{tsShort(t.updated)}</span>
                  <span className="text-zinc-600 dark:text-zinc-400">{String(t.attempts)}×</span>
                </div>
              ))}
            </div>
          </section>

          <LearningOverview ledger={ledger} briefs={briefs} playbook={playbook} />

          <section className="bg-white/55 dark:bg-white/[0.03] border border-zinc-300/60 dark:border-zinc-800 rounded-2xl p-3.5">
            <SectionTitle accent="bg-teal-500">Daily summary</SectionTitle>
            <div className="flex flex-col gap-2 max-h-56 overflow-y-auto">
              {briefs.map((b) => (
                <div key={b.id} className="text-[11px] bg-white dark:bg-zinc-900 rounded-lg p-3">
                  <div title={tsUtc(b.ts)} className="text-zinc-600 dark:text-zinc-400 mb-1">{ts(b.ts)}</div>
                  <div className="text-zinc-700 dark:text-zinc-300 whitespace-pre-wrap">{String(b.brief)}</div>
                </div>
              ))}
              {briefs.length === 0 && <Empty>first summary arrives after tonight's run</Empty>}
            </div>
          </section>

          <LevelsOverview tasks={tasks} />

          <CodesOverview />

          <HistoryOverview />

          <section className="bg-white/55 dark:bg-white/[0.03] border border-zinc-300/60 dark:border-zinc-800 rounded-2xl p-3.5">
            <SectionTitle accent="bg-violet-500">
              Plan{' '}
              <span className="text-zinc-600 dark:text-zinc-400 normal-case">
                v{String(playbook?.version ?? '—')} · <span title={tsUtc(playbook?.updated)}>{tsShort(playbook?.updated)}</span>
              </span>
            </SectionTitle>
            {playbook ? (
              <div className="text-[11px] bg-white dark:bg-zinc-900 rounded-lg p-3 flex flex-col gap-2 max-h-64 overflow-y-auto">
                {playbook.philosophy ? <p className="text-zinc-700 dark:text-zinc-300 italic">“{String(playbook.philosophy)}”</p> : null}
                <pre className="text-zinc-500 dark:text-zinc-400 whitespace-pre-wrap break-words overflow-x-auto">
                  {JSON.stringify(playbook.knobs, null, 1)}
                </pre>
                {(playbook.ceo_directives as unknown[] | undefined)?.map((d, i) => {
                  // entries are {text, ts, status, id} objects (older ones may be
                  // plain strings) — show the message, never the raw JSON
                  const o = d && typeof d === 'object' ? (d as Record<string, unknown>) : { text: d };
                  return (
                    <div key={i} className="mt-1.5 text-violet-600 dark:text-violet-300 border-l-2 border-violet-500 pl-2 py-1 bg-violet-50/60 dark:bg-violet-500/10 rounded-r">
                      <span className="text-zinc-500 dark:text-zinc-400">You:</span> “{String(o.text ?? '')}”
                      <span className="ml-2 text-[10px] font-mono text-zinc-500 dark:text-zinc-400">
                        {String(o.ts ?? '').slice(0, 16)}
                        {o.status ? ` · ${String(o.status)}` : ''}
                      </span>
                    </div>
                  );
                })}
              </div>
            ) : (
              <Empty>No plan yet</Empty>
            )}
          </section>

          {owner ? (
            <Directives />
          ) : (
            <div className="text-[11px] text-zinc-500 dark:text-zinc-400 bg-white dark:bg-zinc-900 rounded-lg px-3 py-2">
              You are watching a live production system, read-only. Only the owner can message the agent.
            </div>
          )}

          <HealthOverview />

        </div>
      </div>

      <footer className="mt-2 pt-4 border-t border-zinc-200 dark:border-zinc-800 flex items-center flex-wrap gap-x-4 gap-y-1 text-[11px] text-zinc-500 dark:text-zinc-400 font-mono">
        <a
          href="/blueprints.html"
          target="_blank"
          rel="noreferrer"
          className="underline decoration-zinc-400 underline-offset-2 hover:text-zinc-900 dark:hover:text-zinc-100"
        >
          how it works
        </a>
        <span className="ml-auto" title="dashboard build: git commit · build date">build {__BUILD_INFO__}</span>
      </footer>
    </div>
  );
}

function LevelDetail({ event, onClose }: { event: Doc; onClose: () => void }) {
  const r = (event.result ?? {}) as Record<string, unknown>;
  const media = (r.media ?? {}) as Record<string, string>;
  const design = (r.design ?? {}) as Record<string, unknown>;
  const skip = new Set(['media', 'design']);
  return (
    <div
      className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-2xl max-w-2xl w-full max-h-[85vh] overflow-y-auto p-5 flex flex-col gap-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-baseline gap-3 flex-wrap">
          <h3 className="text-lg font-bold text-zinc-900 dark:text-zinc-100">
            {String(r.movie ?? r.word ?? r.palindrome ?? r.published ?? 'level')}
          </h3>
          <span title={tsUtc(event.ts)} className="text-[11px] text-zinc-500 dark:text-zinc-400">{gameLabel(String(event.game))} · {ts(event.ts)}</span>
          {GAME_META[String(event.game)] && (
            <span className="text-[11px] text-zinc-500 dark:text-zinc-400">
              play it on{' '}
              <a target="_blank" rel="noreferrer" className="underline decoration-zinc-400 underline-offset-2 hover:text-zinc-900 dark:hover:text-zinc-100"
                href={GAME_META[String(event.game)].appStore}>App Store</a>
              {' · '}
              <a target="_blank" rel="noreferrer" className="underline decoration-zinc-400 underline-offset-2 hover:text-zinc-900 dark:hover:text-zinc-100"
                href={GAME_META[String(event.game)].play}>Google Play</a>
            </span>
          )}
          <button onClick={onClose} className="ml-auto text-zinc-500 dark:text-zinc-400 hover:text-zinc-800 dark:hover:text-zinc-200">✕</button>
        </div>

        {media.clip && (
          <video src={media.clip} controls autoPlay muted loop className="w-full rounded-xl" />
        )}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {media.puzzle && (
            <figure>
              <img src={media.puzzle} className="rounded-xl w-full" alt="puzzle" />
              <figcaption className="text-[10px] text-zinc-500 dark:text-zinc-400 mt-1">puzzle (what players see)</figcaption>
            </figure>
          )}
          {(media.mask || media.solution_svg) && (
            <figure>
              <img src={media.solution_svg || media.mask} className="rounded-xl w-full bg-white" alt="solution" />
              <figcaption className="text-[10px] text-zinc-500 dark:text-zinc-400 mt-1">solution</figcaption>
            </figure>
          )}
        </div>

        <div className="text-[12px] text-zinc-700 dark:text-zinc-300 flex flex-col gap-1">
          {Object.entries(r)
            .filter(([k]) => !skip.has(k))
            .map(([k, v]) => (
              <div key={k} className="flex gap-2">
                <span className="text-zinc-500 dark:text-zinc-400 w-28 shrink-0">{k}</span>
                <span className="break-words">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</span>
              </div>
            ))}
          {Object.entries(design).map(([k, v]) => (
            <div key={k} className="flex gap-2">
              <span className="text-violet-600/70 dark:text-violet-400/70 w-28 shrink-0">{k}</span>
              <span className="break-words text-zinc-600 dark:text-zinc-400">{String(v)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function LevelsOverview({ tasks }: { tasks: Doc[] }) {
  const [selected, setSelected] = useState<Doc | null>(null);
  const GAMES = GAME_KEYS;
  const ledger = useLedgerSince(72);
  const levelEvents = ledger.filter((e) => {
    const r = (e.result ?? {}) as Record<string, unknown>;
    return (
      e.kind === 'action' &&
      e.action === 'level_pipeline' &&
      (e.status === 'done' || e.status === 'preview') &&
      !r.dry_run // dry-run completions are rehearsals, not content
    );
  });
  const pendingByGame = (g: string) =>
    tasks.filter((t) => t.type === 'level_pipeline' && t.game === g && (t.status === 'pending' || t.status === 'running')).length;

  const title = (r: Record<string, unknown>) =>
    String(r.movie ?? r.word ?? r.palindrome ?? r.published ?? r.would_publish ?? '?');

  return (
    <section className="bg-white/55 dark:bg-white/[0.03] border border-zinc-300/60 dark:border-zinc-800 rounded-2xl p-3.5">
      <SectionTitle accent="bg-emerald-500">Stages created <span className="text-zinc-600 dark:text-zinc-400 normal-case">· last 72 h</span></SectionTitle>
      <div className="flex flex-col gap-3">
        {GAMES.map((g) => {
          const events = levelEvents.filter((e) => e.game === g);
          const pending = pendingByGame(g);
          return (
            <div key={g} className="bg-white dark:bg-zinc-900 rounded-lg p-3">
              <div className="flex items-baseline gap-2 text-[11px] mb-1">
                <span className="text-zinc-800 dark:text-zinc-200 font-bold">{GAME_META[g]?.label ?? g}</span>
                <span className="text-zinc-600 dark:text-zinc-400 ml-auto">
                  {events.length} created{pending ? ` · ${pending} pending` : ''}
                </span>
              </div>
              {events.length === 0 && !pending && <Empty>none yet</Empty>}
              {events.slice(0, 4).map((e) => {
                const r = (e.result ?? {}) as Record<string, unknown>;
                return (
                  <div
                    key={e.id}
                    onClick={() => setSelected(e)}
                    className="text-[11px] flex gap-2 items-baseline py-0.5 cursor-pointer hover:bg-zinc-100 dark:hover:bg-zinc-800 rounded px-1 -mx-1"
                  >
                    <span className={e.status === 'preview' ? 'text-amber-600 dark:text-amber-400' : 'text-emerald-600 dark:text-emerald-400'}>
                      {e.status === 'preview' ? '◔ preview' : '✓ live'}
                    </span>
                    <span className="text-zinc-700 dark:text-zinc-300 font-bold">{title(r)}</span>
                    {r.qa ? <span className="text-zinc-500 dark:text-zinc-400">qa:{String(r.qa)}</span> : null}
                    {r.levelId ? <span className="text-zinc-500 dark:text-zinc-400">#{String(r.levelId)}</span> : null}
                    {r.level ? <span className="text-zinc-500 dark:text-zinc-400">#{String(r.level)}</span> : null}
                    <span title={tsUtc(e.ts)} className="text-zinc-600 dark:text-zinc-400 ml-auto">{ts(e.ts)}</span>
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>
      {selected && <LevelDetail event={selected} onClose={() => setSelected(null)} />}
    </section>
  );
}

function CodesOverview() {
  const summary = useDoc('stagenator_playbook/codes_summary');
  const games = (summary?.games ?? {}) as Record<string, Record<string, unknown>>;
  return (
    <section className="bg-white/55 dark:bg-white/[0.03] border border-zinc-300/60 dark:border-zinc-800 rounded-2xl p-3.5">
      <SectionTitle accent="bg-orange-500">Codes & claims <span title={tsUtc(summary?.updated)} className="text-zinc-600 dark:text-zinc-400 normal-case">· {tsShort(summary?.updated)}</span></SectionTitle>
      <div className="flex flex-col gap-1.5">
        {Object.entries(games).sort(([a], [b]) => gameOrder(a) - gameOrder(b)).map(([g, d]) => {
          const stock = (d.stock ?? {}) as Record<string, { available?: number }>;
          const claims = (d.claims ?? { links: 0, codes_backing: 0, teared: 0 }) as Record<string, number>;
          return (
            <div key={g} className="text-[11px] bg-white dark:bg-zinc-900 rounded-lg px-3 py-2">
              <div className="flex justify-between items-baseline gap-3">
                <span className="text-zinc-800 dark:text-zinc-200 font-bold">{gameLabel(g)}</span>
                <span className="text-emerald-600 dark:text-emerald-400 shrink-0">
                  {claims.teared ?? 0} claimed · {claims.links ?? 0} drop{(claims.links ?? 0) === 1 ? '' : 's'} live
                </span>
              </div>
              <div className="text-zinc-500 dark:text-zinc-400 mt-0.5">
                stock {(stock && typeof stock === 'object' ? Object.entries(stock).sort(([a], [b]) => a.localeCompare(b)) : []).map(([p, s]) => `${p}:${(s as { available?: number })?.available ?? '?'}`).join(' · ') || '—'}
              </div>
              {(() => {
                const ex = (d as { experiment?: Record<string, { sends: number; claims: number }> }).experiment;
                if (!ex || (!ex.a && !ex.b)) return null;
                const f = (v?: { sends: number; claims: number }) => (v ? `${v.claims}/${v.sends}` : '0/0');
                return (
                  <span className="w-full text-violet-600 dark:text-violet-400 text-[10.5px]">
                    copy experiment · A {f(ex.a)} claimed · B {f(ex.b)} claimed
                  </span>
                );
              })()}
            </div>
          );
        })}
        {!summary && <Empty>Updates on the next run</Empty>}
      </div>
    </section>
  );
}

function LearningOverview({ ledger, briefs, playbook }: { ledger: Doc[]; briefs: Doc[]; playbook: Doc | null }) {
  const outcomes = ledger.filter((e) => e.kind === 'outcome').length;
  const lastChange = briefs.map((b) => String(b.changes ?? '')).find((c) => c && c.trim());
  return (
    <section className="bg-white/55 dark:bg-white/[0.03] border border-zinc-300/60 dark:border-zinc-800 rounded-2xl p-3.5">
      <SectionTitle accent="bg-violet-500">Learning <span className="text-zinc-600 dark:text-zinc-400 normal-case">· nightly</span></SectionTitle>
      <div className="bg-white dark:bg-zinc-900 rounded-lg p-3 flex flex-col gap-2 text-[11px]">
        <div className="flex justify-between items-baseline">
          <span className="text-zinc-700 dark:text-zinc-300">Plan <span className="text-zinc-500 dark:text-zinc-400">v{String(playbook?.version ?? '—')}</span></span>
          <span className="text-zinc-600 dark:text-zinc-400">updated {ts(playbook?.updated)}</span>
        </div>
        <div className="flex justify-between items-baseline">
          <span className="text-zinc-600 dark:text-zinc-400">results so far</span>
          <span className={outcomes ? 'text-emerald-600 dark:text-emerald-400 font-bold' : 'text-zinc-500 dark:text-zinc-400'}>{outcomes}</span>
        </div>
        {outcomes === 0 ? (
          <div className="text-zinc-500 dark:text-zinc-400 leading-relaxed">
            It checks results every night. So far no one has claimed a code or come back, so there is <span className="text-zinc-700 dark:text-zinc-300">nothing to go on yet</span> — it keeps the current plan rather than reacting to thin data, and starts adjusting once real numbers come in.
          </div>
        ) : (
          <div className="text-zinc-500 dark:text-zinc-400">
            last change: <span className="text-violet-600 dark:text-violet-300">{lastChange ?? 'kept the plan the same (not enough data yet)'}</span>
          </div>
        )}
      </div>
    </section>
  );
}

function HistoryOverview() {
  const hist = useDoc('stagenator_playbook/daily_history');
  const [game, setGame] = useState('subliminal-words');
  if (!hist) return null;
  const days = (hist.days ?? {}) as Record<string, Record<string, { players?: number; revenue_usd?: number; engagement_min?: number; actions?: number; errors?: number }>>;
  const keys = Object.keys(days).sort().slice(-30);
  if (keys.length < 2) return null;
  const games = GAME_KEYS;
  const W = 580, PAD = 8;
  const slot = (W - 2 * PAD) / keys.length;
  const x0 = (i: number) => PAD + i * slot;
  const xc = (i: number) => x0(i) + slot / 2 - 1;
  const acted = keys.map((k) => ((days[k]?.[game]?.actions ?? 0) > 0));
  // vertical eye-lines through a chart of height h, on agent days
  const marks = (h: number) =>
    keys.map((k, i) =>
      acted[i] ? (
        <line key={'m' + k} x1={xc(i)} y1={2} x2={xc(i)} y2={h} strokeDasharray="2 3"
          stroke="#059669" strokeWidth="1.4" opacity="0.6" />
      ) : null,
    );
  const maxOf = (f: (g: { players?: number; revenue_usd?: number; engagement_min?: number }) => number) =>
    Math.max(0.01, ...keys.map((k) => Math.max(...games.map((g) => f(days[k]?.[g] ?? {})))));
  const pMax = Math.max(1, ...keys.map((k) => days[k]?.[game]?.players ?? 0));
  const eMax = Math.max(0.01, ...keys.map((k) => days[k]?.[game]?.engagement_min ?? 0));
  const rev = keys.map((k) => days[k]?.[game]?.revenue_usd ?? 0);
  const revMax = Math.max(0.01, ...rev);
  const revSum = rev.reduce((a, b) => a + b, 0);
  const fmtD = (k: string) => `${k.slice(8, 10)}.${k.slice(5, 7)}.`;
  return (
    <section className="bg-white/55 dark:bg-white/[0.03] border border-zinc-300/60 dark:border-zinc-800 rounded-2xl p-3.5">
      <SectionTitle accent="bg-blue-500">
        Last 30 days <span className="text-zinc-600 dark:text-zinc-400 normal-case">· per game</span>
      </SectionTitle>
      <div className="flex gap-1.5 mb-2 items-center flex-wrap">
        {games.map((g) => (
          <button key={g} onClick={() => setGame(g)}
            className={`text-[11px] px-2.5 py-1 rounded-full border transition-colors ${
              game === g
                ? 'border-transparent text-white'
                : 'border-zinc-300 dark:border-zinc-700 text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100'
            }`}
            style={game === g ? { background: GAME_META[g].color, color: GAME_META[g].fg } : undefined}
          >
            {GAME_META[g].label}
          </button>
        ))}
        <span className="text-[10.5px] text-zinc-500 dark:text-zinc-400 ml-auto">
          live on{' '}
          <a target="_blank" rel="noreferrer" className="underline decoration-zinc-400 underline-offset-2 hover:text-zinc-900 dark:hover:text-zinc-100"
            href={GAME_META[game].appStore}>App Store</a>
          {' · '}
          <a target="_blank" rel="noreferrer" className="underline decoration-zinc-400 underline-offset-2 hover:text-zinc-900 dark:hover:text-zinc-100"
            href={GAME_META[game].play}>Google Play</a>
        </span>
      </div>
      <div className="bg-white dark:bg-zinc-900 rounded-lg p-3 flex flex-col gap-3 text-[11px]">

        <div>
          <div className="flex justify-between items-baseline mb-1">
            <span className="text-zinc-700 dark:text-zinc-300 font-bold">1 · Agent acted on this game</span>
            <span className="text-zinc-500 dark:text-zinc-400"><span className="text-emerald-600 dark:text-emerald-400">■</span> = dashed line below</span>
          </div>
          <svg viewBox={`0 0 ${W} 16`} className="w-full h-auto">
            {keys.map((k, i) => (
              <rect key={k} x={x0(i)} y={2} width={Math.max(3, slot - 2)} height={10} rx="2"
                fill={acted[i] ? '#059669' : 'currentColor'}
                opacity={acted[i] ? 0.95 : 0.1} />
            ))}
          </svg>
        </div>

        <div>
          <div className="flex justify-between items-baseline mb-1">
            <span className="text-zinc-700 dark:text-zinc-300 font-bold">2 · Players per day</span>
            <span className="text-zinc-500 dark:text-zinc-400">peak {pMax}</span>
          </div>
          <svg viewBox={`0 0 ${W} 76`} className="w-full h-auto">
            <line x1={PAD} y1={62} x2={W - PAD} y2={62} stroke="currentColor" opacity="0.15" />
            {marks(62)}
            {keys.map((k, i) => {
              const v = days[k]?.[game]?.players ?? 0;
              if (!v) return null;
              const bh = (v / pMax) * 54;
              return <rect key={k} x={x0(i)} y={62 - bh} width={Math.max(3, slot - 2)} height={bh} rx="1" className="fill-sky-600 dark:fill-sky-400" />;
            })}
            {keys.map((k, i) =>
              i % 7 === 0 || i === keys.length - 1 ? (
                <text key={k} x={x0(i)} y={74} fontSize="9" fill="currentColor" opacity="0.45">{fmtD(k)}</text>
              ) : null,
            )}
          </svg>
        </div>

        <div>
          <div className="flex justify-between items-baseline mb-1">
            <span className="text-zinc-700 dark:text-zinc-300 font-bold">3 · Minutes played, per player</span>
            <span className="text-zinc-500 dark:text-zinc-400">peak {eMax.toFixed(0)} min</span>
          </div>
          <svg viewBox={`0 0 ${W} 66`} className="w-full h-auto">
            <line x1={PAD} y1={56} x2={W - PAD} y2={56} stroke="currentColor" opacity="0.15" />
            {marks(56)}
            {keys.map((k, i) => {
              const v = days[k]?.[game]?.engagement_min ?? 0;
              if (!v) return null;
              const bh = (v / eMax) * 46;
              return <rect key={k} x={x0(i)} y={56 - bh} width={Math.max(3, slot - 2)} height={bh} rx="1" className="fill-violet-600 dark:fill-violet-400" opacity="0.9" />;
            })}
          </svg>
        </div>

        <div>
          <div className="flex justify-between items-baseline mb-1">
            <span className="text-zinc-700 dark:text-zinc-300 font-bold">4 · Earnings per day</span>
            <span className="text-emerald-600 dark:text-emerald-400 font-bold">${revSum.toFixed(2)} in 30 days</span>
          </div>
          <svg viewBox={`0 0 ${W} 62`} className="w-full h-auto">
            <line x1={PAD} y1={50} x2={W - PAD} y2={50} stroke="currentColor" opacity="0.15" />
            {marks(50)}
            {keys.map((k, i) => {
              const v = rev[i];
              if (v <= 0) return null;
              const bh = Math.max(4, (v / revMax) * 34);
              return (
                <g key={k}>
                  <rect x={x0(i)} y={50 - bh} width={Math.max(3, slot - 2)} height={bh} rx="1"
                    className="fill-emerald-500 dark:fill-emerald-400" />
                  <text x={x0(i) + slot / 2} y={50 - bh - 3} fontSize="8.5" textAnchor="middle"
                    className="fill-emerald-600 dark:fill-emerald-400">{v.toFixed(2)}</text>
                </g>
              );
            })}
          </svg>
        </div>

        <div className="text-zinc-500 dark:text-zinc-400">
          If the agent works, bars to the <b>right</b> of dashed lines should grow — more players back, longer sessions, more earnings.
        </div>
      </div>
    </section>
  );
}


function HealthOverview() {
  const h = useDoc('stagenator_playbook/health');
  const [open, setOpen] = useState(true);
  if (!h) return null;
  const checks = (h.checks ?? []) as { name: string; ok: boolean; warn: boolean; detail: string; ms: number }[];
  const st = String(h.status);
  const stColor = st === 'healthy' ? 'text-emerald-600 dark:text-emerald-400' : st === 'degraded' ? 'text-amber-600 dark:text-amber-400' : 'text-red-600 dark:text-red-400';
  const dot = (c: { ok: boolean; warn: boolean }) => (c.ok && !c.warn ? 'bg-emerald-500' : c.warn ? 'bg-amber-400' : 'bg-red-500');
  return (
    <section className="bg-white/55 dark:bg-white/[0.03] border border-zinc-300/60 dark:border-zinc-800 rounded-2xl p-3.5">
      <SectionTitle accent="bg-rose-500">Health <span title={tsUtc(h.ran_at)} className="text-zinc-600 dark:text-zinc-400 normal-case">· {tsShort(h.ran_at)} · {String(h.trigger ?? '')}</span></SectionTitle>
      <div className="bg-white dark:bg-zinc-900 rounded-lg p-3 flex flex-col gap-2 text-[11px]">
        <button
          onClick={() => setOpen((o) => !o)}
          className="flex justify-between items-baseline w-full text-left cursor-pointer"
          aria-expanded={open}
        >
          <span className={`font-bold uppercase tracking-wide ${stColor}`}>
            <span className="text-zinc-500 dark:text-zinc-400 mr-1.5 normal-case font-normal">{open ? '▾' : '▸'}</span>
            {st}
          </span>
          <span className="text-zinc-500 dark:text-zinc-400">{Number(h.ok ?? 0)} ok · {Number(h.warn ?? 0)} warn · <span className={Number(h.fail) ? 'text-red-600 dark:text-red-400' : ''}>{Number(h.fail ?? 0)} down</span></span>
        </button>
        {open && <div className="flex flex-col gap-1.5">
          {checks.map((c) => (
            <div key={c.name} className="grid grid-cols-[10px_1fr] gap-x-2 items-start">
              <span className={`inline-block w-1.5 h-1.5 rounded-full mt-[5px] ${dot(c)}`} />
              <div className="flex flex-wrap justify-between gap-x-3">
                <span className="text-zinc-700 dark:text-zinc-300">{c.name}</span>
                <span className={c.ok && !c.warn ? 'text-zinc-500 dark:text-zinc-400' : c.warn ? 'text-amber-600 dark:text-amber-400' : 'text-red-600 dark:text-red-400 font-medium'}>{c.detail}</span>
              </div>
            </div>
          ))}
          {checks.length === 0 && <span className="text-zinc-600 dark:text-zinc-400 italic">runs at deploy + daily</span>}
        </div>}
      </div>
    </section>
  );
}



function Directives() {
  const [text, setText] = useState('');
  const [sent, setSent] = useState(false);
  const directives = useCollection('stagenator_directives', 'ts', 5);

  const send = async () => {
    if (!text.trim()) return;
    await addDoc(collection(db, 'stagenator_directives'), {
      text: text.trim(),
      from: 'ceo',
      status: 'new',
      ts: serverTimestamp(),
    });
    setText('');
    setSent(true);
    setTimeout(() => setSent(false), 2500);
  };

  return (
    <section className="bg-white/55 dark:bg-white/[0.03] border border-zinc-300/60 dark:border-zinc-800 rounded-2xl p-3.5">
      <SectionTitle accent="bg-fuchsia-500">Message the agent</SectionTitle>
      <div className="flex gap-2">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
          placeholder="leave a note — it reads this on the next run"
          className="flex-1 bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-lg px-3 py-2 text-xs text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-600 dark:placeholder:text-zinc-400 focus:outline-none focus:border-zinc-300 dark:focus:border-zinc-700"
        />
        <button
          onClick={send}
          className="border border-zinc-300 dark:border-zinc-700 rounded-lg px-4 py-2 text-xs hover:bg-zinc-100 dark:hover:bg-zinc-800 shrink-0"
        >
          {sent ? '✓' : 'send'}
        </button>
      </div>
      <div className="flex flex-col gap-1 mt-2">
        {directives.map((d) => (
          <div key={d.id} className="text-[11px] text-zinc-600 dark:text-zinc-400 flex gap-2">
            <span className={d.status === 'new' ? 'text-amber-600 dark:text-amber-400' : 'text-emerald-600 dark:text-emerald-400'}>
              {d.status === 'new' ? '◔' : '✓'}
            </span>
            <span>{String(d.text)}</span>
            {typeof d.response === 'string' && d.response && (
              <span className="text-zinc-600 dark:text-zinc-400">→ {d.response.slice(0, 80)}</span>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

function SectionTitle({ children, accent = 'bg-zinc-400 dark:bg-zinc-500' }: { children: React.ReactNode; accent?: string }) {
  return (
    <h2 className="font-display font-bold text-[12px] uppercase tracking-[0.18em] text-zinc-700 dark:text-zinc-100 mb-2.5 flex items-baseline gap-2">
      <span className={`self-center inline-block w-1 h-3.5 rounded-full shrink-0 ${accent}`} />
      <span>{children}</span>
    </h2>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="text-[11px] text-zinc-600 dark:text-zinc-400 italic">{children}</div>;
}
