/**
 * Stagenator Mission Control — live observability over the agent's Firestore
 * state. Read-mostly; the one write surface is the CEO directive box.
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
} from 'firebase/firestore';
import { auth, db, gisSignIn, isOwner } from './firebase';

type Doc = Record<string, unknown> & { id: string };

function useCollection(path: string, orderField: string, n = 50): Doc[] {
  const [docs, setDocs] = useState<Doc[]>([]);
  useEffect(() => {
    const q = query(collection(db, path), orderBy(orderField, 'desc'), limit(n));
    return onSnapshot(q, (snap) => {
      setDocs(snap.docs.map((d) => ({ id: d.id, ...d.data() })));
    });
  }, [path, orderField, n]);
  return docs;
}

function useDoc(path: string): Doc | null {
  const [data, setData] = useState<Doc | null>(null);
  useEffect(() => {
    return onSnapshot(doc(db, path), (snap) => {
      setData(snap.exists() ? ({ id: snap.id, ...snap.data() } as Doc) : null);
    });
  }, [path]);
  return data;
}

const ts = (v: unknown): string => {
  const d = (v as { toDate?: () => Date })?.toDate?.();
  return d ? d.toLocaleString('en-GB', { hour12: false }).slice(0, 17) : '';
};

const KIND_COLORS: Record<string, string> = {
  signal: 'text-sky-400 border-sky-800',
  decision: 'text-violet-400 border-violet-800',
  action: 'text-emerald-400 border-emerald-800',
  rejected: 'text-amber-400 border-amber-800',
  error: 'text-red-400 border-red-800',
  brief: 'text-zinc-300 border-zinc-700',
  outcome: 'text-teal-400 border-teal-800',
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
  if (!isOwner(user)) return <SignIn denied={!!user} />;

  return <Dashboard />;
}

function SignIn({ denied }: { denied: boolean }) {
  const [error, setError] = useState('');
  const buttonRef = (el: HTMLDivElement | null) => {
    if (el && !el.hasChildNodes()) gisSignIn(el, setError).catch((e) => setError(String(e)));
  };
  return (
    <Center>
      <div className="flex flex-col items-center gap-4">
        <div className="text-3xl">🎛️</div>
        <div className="text-zinc-400 text-sm uppercase tracking-widest">
          Stagenator Mission Control
        </div>
        {denied && <div className="text-red-400 text-xs">this account has no access</div>}
        {error && <div className="text-red-400 text-xs max-w-xs text-center">{error}</div>}
        <div ref={buttonRef} />
      </div>
    </Center>
  );
}

function Center({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex items-center justify-center text-zinc-300">{children}</div>
  );
}

function Dashboard() {
  const ledger = useCollection('stagenator_ledger', 'ts', 60);
  const tasks = useCollection('stagenator_tasks', 'updated', 40);
  const briefs = useCollection('stagenator_briefs', 'ts', 3);
  const playbook = useDoc('stagenator_playbook/current');
  const heartbeat = useDoc('stagenator_playbook/heartbeat');

  const taskBuckets = useMemo(() => {
    const b: Record<string, Doc[]> = { pending: [], running: [], done: [], dead: [] };
    tasks.forEach((t) => (b[String(t.status)] ?? (b[String(t.status)] = [])).push(t));
    return b;
  }, [tasks]);

  const lastHeartbeat = ledger[0] ? ts(ledger[0].ts) : '—';

  return (
    <div className="min-h-screen text-zinc-200 p-4 md:p-6 max-w-7xl mx-auto flex flex-col gap-5">
      <header className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-3">
          <span className="text-2xl">🎛️</span>
          <h1 className="font-bold tracking-widest uppercase text-sm">Stagenator · Mission Control</h1>
        </div>
        <div className="flex items-center gap-4 text-[11px] text-zinc-500">
          <span>
            last pulse {heartbeat ? `${ts(heartbeat.at)} · ${String(heartbeat.kind)}` : '—'}
          </span>
          <span>last event {lastHeartbeat}</span>
          <span className="flex items-center gap-1.5">
            <span
              className={`inline-block w-2 h-2 rounded-full ${
                taskBuckets.dead.length ? 'bg-red-500' : 'bg-emerald-500'
              } animate-pulse`}
            />
            {taskBuckets.dead.length ? `${taskBuckets.dead.length} dead-lettered` : 'healthy'}
          </span>
        </div>
      </header>

      <div className="grid md:grid-cols-3 gap-5">
        {/* Ledger feed */}
        <section className="md:col-span-2 flex flex-col gap-2">
          <SectionTitle>Decision ledger — live</SectionTitle>
          <div className="flex flex-col gap-1.5 max-h-[70vh] overflow-y-auto pr-1">
            {ledger.map((e) => (
              <div
                key={e.id}
                className={`border-l-2 pl-3 py-1.5 text-xs bg-zinc-900/60 rounded-r-lg ${
                  KIND_COLORS[String(e.kind)] ?? 'text-zinc-400 border-zinc-700'
                }`}
              >
                <div className="flex gap-2 items-baseline flex-wrap">
                  <span className="uppercase font-bold">{String(e.kind)}</span>
                  {e.game ? <span className="text-zinc-400">{String(e.game)}</span> : null}
                  <span className="text-zinc-600 ml-auto">{ts(e.ts)}</span>
                </div>
                <div className="text-zinc-400 mt-0.5 break-words">
                  {String(e.signal ?? e.action ?? e.reason ?? '')}
                  {e.status ? ` · ${String(e.status)}` : ''}
                  {e.detail ? ` · ${String(e.detail)}` : ''}
                  {e.reason && e.action ? ` — ${String(e.reason)}` : ''}
                </div>
              </div>
            ))}
            {ledger.length === 0 && <Empty>no events yet</Empty>}
          </div>
        </section>

        {/* Right column */}
        <div className="flex flex-col gap-5">
          <section>
            <SectionTitle>Tasks</SectionTitle>
            <div className="grid grid-cols-4 gap-2 text-center mb-2">
              {(['pending', 'running', 'done', 'dead'] as const).map((s) => (
                <div key={s} className="bg-zinc-900 rounded-lg py-2">
                  <div
                    className={`text-lg font-bold ${
                      s === 'dead' && taskBuckets[s].length ? 'text-red-400' : 'text-zinc-200'
                    }`}
                  >
                    {taskBuckets[s]?.length ?? 0}
                  </div>
                  <div className="text-[10px] text-zinc-500 uppercase">{s}</div>
                </div>
              ))}
            </div>
            <div className="flex flex-col gap-1 max-h-40 overflow-y-auto">
              {tasks.slice(0, 8).map((t) => (
                <div key={t.id} className="text-[11px] bg-zinc-900/60 rounded px-2 py-1 flex gap-2">
                  <span
                    className={
                      t.status === 'dead'
                        ? 'text-red-400'
                        : t.status === 'done'
                          ? 'text-emerald-400'
                          : 'text-amber-300'
                    }
                  >
                    ●
                  </span>
                  <span className="text-zinc-300">{String(t.type)}</span>
                  <span className="text-zinc-500">{String(t.game)}</span>
                  <span className="text-zinc-600 ml-auto">{String(t.attempts)}×</span>
                </div>
              ))}
            </div>
          </section>

          <LevelsOverview ledger={ledger} tasks={tasks} />

          <CodesOverview />

          <CostOverview />

          <section>
            <SectionTitle>
              Playbook{' '}
              <span className="text-zinc-600 normal-case">
                v{String(playbook?.version ?? '—')} · {ts(playbook?.updated)}
              </span>
            </SectionTitle>
            {playbook ? (
              <div className="text-[11px] bg-zinc-900/60 rounded-lg p-3 flex flex-col gap-2 max-h-64 overflow-y-auto">
                <p className="text-zinc-300 italic">“{String(playbook.philosophy)}”</p>
                <pre className="text-zinc-500 whitespace-pre-wrap">
                  {JSON.stringify(playbook.knobs, null, 1)}
                </pre>
                {(playbook.ceo_directives as string[] | undefined)?.map((d, i) => (
                  <div key={i} className="text-violet-300 border-l-2 border-violet-700 pl-2">
                    CEO: {typeof d === 'string' ? d : JSON.stringify(d)}
                  </div>
                ))}
              </div>
            ) : (
              <Empty>no playbook yet</Empty>
            )}
          </section>

          <Directives />

          <section>
            <SectionTitle>Daily briefs</SectionTitle>
            <div className="flex flex-col gap-2 max-h-56 overflow-y-auto">
              {briefs.map((b) => (
                <div key={b.id} className="text-[11px] bg-zinc-900/60 rounded-lg p-3">
                  <div className="text-zinc-600 mb-1">{ts(b.ts)}</div>
                  <div className="text-zinc-300 whitespace-pre-wrap">{String(b.brief)}</div>
                </div>
              ))}
              {briefs.length === 0 && <Empty>first brief arrives after tonight's run</Empty>}
            </div>
          </section>
        </div>
      </div>
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
      className="fixed inset-0 bg-black/80 z-50 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="bg-zinc-900 border border-zinc-700 rounded-2xl max-w-2xl w-full max-h-[85vh] overflow-y-auto p-5 flex flex-col gap-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-baseline gap-3">
          <h3 className="text-lg font-bold text-zinc-100">
            {String(r.movie ?? r.word ?? r.published ?? 'level')}
          </h3>
          <span className="text-[11px] text-zinc-500">{String(event.game)} · {ts(event.ts)}</span>
          <button onClick={onClose} className="ml-auto text-zinc-500 hover:text-zinc-200">✕</button>
        </div>

        {media.clip && (
          <video src={media.clip} controls autoPlay muted loop className="w-full rounded-xl" />
        )}
        <div className="grid grid-cols-2 gap-3">
          {media.puzzle && (
            <figure>
              <img src={media.puzzle} className="rounded-xl w-full" alt="puzzle" />
              <figcaption className="text-[10px] text-zinc-500 mt-1">puzzle (what players see)</figcaption>
            </figure>
          )}
          {(media.mask || media.solution_svg) && (
            <figure>
              <img src={media.solution_svg || media.mask} className="rounded-xl w-full bg-white" alt="solution" />
              <figcaption className="text-[10px] text-zinc-500 mt-1">solution</figcaption>
            </figure>
          )}
        </div>

        <div className="text-[12px] text-zinc-300 flex flex-col gap-1">
          {Object.entries(r)
            .filter(([k]) => !skip.has(k))
            .map(([k, v]) => (
              <div key={k} className="flex gap-2">
                <span className="text-zinc-500 w-28 shrink-0">{k}</span>
                <span className="break-words">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</span>
              </div>
            ))}
          {Object.entries(design).map(([k, v]) => (
            <div key={k} className="flex gap-2">
              <span className="text-violet-400/70 w-28 shrink-0">{k}</span>
              <span className="break-words text-zinc-400">{String(v)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function LevelsOverview({ ledger, tasks }: { ledger: Doc[]; tasks: Doc[] }) {
  const [selected, setSelected] = useState<Doc | null>(null);
  const GAMES = ['subliminal-words', 'ai-movie-quiz'];  // palindrome inactive (Future Phase)
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
    String(r.movie ?? r.word ?? r.published ?? r.would_publish ?? '?');

  return (
    <section>
      <SectionTitle>Levels created</SectionTitle>
      <div className="flex flex-col gap-3">
        {GAMES.map((g) => {
          const events = levelEvents.filter((e) => e.game === g);
          const pending = pendingByGame(g);
          return (
            <div key={g} className="bg-zinc-900/60 rounded-lg p-2.5">
              <div className="flex items-baseline gap-2 text-[11px] mb-1">
                <span className="text-zinc-200 font-bold">{g}</span>
                <span className="text-zinc-600 ml-auto">
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
                    className="text-[11px] flex gap-2 items-baseline py-0.5 cursor-pointer hover:bg-zinc-800/60 rounded px-1 -mx-1"
                  >
                    <span className={e.status === 'preview' ? 'text-amber-400' : 'text-emerald-400'}>
                      {e.status === 'preview' ? '◔ preview' : '✓ live'}
                    </span>
                    <span className="text-zinc-300 font-bold">{title(r)}</span>
                    {r.qa ? <span className="text-zinc-500">qa:{String(r.qa)}</span> : null}
                    {r.levelId ? <span className="text-zinc-500">#{String(r.levelId)}</span> : null}
                    {r.level ? <span className="text-zinc-500">#{String(r.level)}</span> : null}
                    <span className="text-zinc-600 ml-auto">{ts(e.ts)}</span>
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
    <section>
      <SectionTitle>Codes & claims <span className="text-zinc-600 normal-case">· {ts(summary?.updated)}</span></SectionTitle>
      <div className="flex flex-col gap-1.5">
        {Object.entries(games).map(([g, d]) => {
          const stock = (d.stock ?? {}) as Record<string, { available?: number }>;
          const claims = (d.claims ?? { links: 0, codes_backing: 0, teared: 0 }) as Record<string, number>;
          return (
            <div key={g} className="text-[11px] bg-zinc-900/60 rounded px-2 py-1.5 flex gap-3 items-baseline flex-wrap">
              <span className="text-zinc-200 font-bold">{g}</span>
              <span className="text-zinc-500">
                stock {Object.entries(stock).map(([p, s]) => `${p}:${s.available ?? '?'}`).join(' · ')}
              </span>
              <span className="text-emerald-400 ml-auto">
                {claims.teared ?? 0} claimed · {claims.links ?? 0} drop{(claims.links ?? 0) === 1 ? '' : 's'} live
              </span>
            </div>
          );
        })}
        {!summary && <Empty>updates on next pulse</Empty>}
      </div>
    </section>
  );
}

function CostOverview() {
  const cost = useDoc('stagenator_playbook/cost_summary');
  if (!cost) return null;
  const today = (cost.today ?? {}) as Record<string, number>;
  const month = (cost.month ?? {}) as Record<string, number>;
  const pct = Number(cost.budget_pct ?? 0);
  return (
    <section>
      <SectionTitle>
        Spend <span className="text-zinc-600 normal-case">· est · {ts(cost.updated)}</span>
      </SectionTitle>
      <div className="bg-zinc-900/60 rounded-lg p-3 flex flex-col gap-2 text-[11px]">
        <div className="flex justify-between items-baseline">
          <span className="text-zinc-400">today</span>
          <span className="text-emerald-400 font-bold text-sm">${Number(today.usd ?? 0).toFixed(2)}</span>
        </div>
        <div className="text-zinc-600">
          {today.veo_clips ?? 0} Veo · {today.runpod_puzzles ?? 0} puzzles · {today.gemini_calls ?? 0} Gemini
        </div>
        <div className="flex justify-between items-baseline pt-1">
          <span className="text-zinc-400">month · ${Number(month.usd ?? 0).toFixed(2)} / ${Number(cost.budget_usd ?? 0).toFixed(0)}</span>
          <span className="text-zinc-500">{pct.toFixed(1)}%</span>
        </div>
        <div className="h-1.5 bg-zinc-800 rounded-full overflow-hidden">
          <div
            className={`h-full ${pct > 90 ? 'bg-red-500' : pct > 50 ? 'bg-amber-500' : 'bg-emerald-500'}`}
            style={{ width: `${Math.min(100, pct)}%` }}
          />
        </div>
        <div className="text-zinc-600 pt-1">
          Runpod balance: {cost.runpod_balance == null ? 'n/a (endpoint-scoped key)' : `$${cost.runpod_balance}`}
        </div>
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
    <section>
      <SectionTitle>CEO channel</SectionTitle>
      <div className="flex gap-2">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
          placeholder="guide the agent… (picked up next run)"
          className="flex-1 bg-zinc-900 border border-zinc-700 rounded-lg px-3 py-2 text-xs text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:border-zinc-500"
        />
        <button
          onClick={send}
          className="border border-zinc-600 rounded-lg px-3 text-xs hover:bg-zinc-800"
        >
          {sent ? '✓' : 'send'}
        </button>
      </div>
      <div className="flex flex-col gap-1 mt-2">
        {directives.map((d) => (
          <div key={d.id} className="text-[11px] text-zinc-400 flex gap-2">
            <span className={d.status === 'new' ? 'text-amber-400' : 'text-emerald-500'}>
              {d.status === 'new' ? '◔' : '✓'}
            </span>
            <span>{String(d.text)}</span>
            {typeof d.response === 'string' && d.response && (
              <span className="text-zinc-600">→ {d.response.slice(0, 80)}</span>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="text-[11px] uppercase tracking-widest text-zinc-500 mb-2">{children}</h2>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="text-[11px] text-zinc-600 italic">{children}</div>;
}
