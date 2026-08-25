# Stagenator

**An autonomous engagement & retention agent for live mobile games.** It watches
Google Analytics, decides what will grow engagement, and acts on its own —
shipping AI-generated levels, distributing promo/offer codes, keeping code
inventory alive, and learning day by day — with no human in the loop and full
observability. It runs two live games (Subliminal Words and AI Movie Quiz),
shipping real AI-generated content into apps that real players download.

> All Things Agentic Hackathon · **Taskmaster** track — *a complete workflow,
> not a chatbot.* Live Mission Control: https://stagenator-mission.web.app

---

## What it does, autonomously

- **Ships levels.** Designs and generates real game content — Subliminal Words
  puzzles (Runpod/ComfyUI ControlNet + a solution SVG that exactly matches the
  game's `word_level_svg` contract, mask and highlight pixel-aligned), AI Movie
  Quiz clips (Veo 3.1 Lite, with visual/ambience/dialogue difficulty strategies)
  — validates each (schema + Gemini vision QA + dedup), submits it via the
  game's exact write path, then **re-reads and self-validates the published
  artifact**, disabling and re-shipping anything structurally broken.
- **Delivers codes.** Google Play promo codes and Apple offer codes, distributed
  through **proffer.codes** with no-sign-in tokened claim links. Apple codes are
  **minted fully via the App Store Connect API** (both IAP and subscription
  offers); Google Play — which has *no* code-minting API (Console-only, verified
  in Google's own API discovery) — is a reply-to-restock email loop the agent
  drives from its own inbox.
- **Keeps inventory alive.** A daily audit knows each store's expiry rule (Apple
  28-day, Play promotion-end) and quarantines dead codes before a player ever
  hits one — the problem that had silently broken every campaign since January
  (91 of 92 legacy codes were dead).
- **Learns.** A nightly Reflector compares actions to outcomes and rewrites a
  playbook that steers the next day — with statistical humility at low samples.
- **Reports & takes direction.** Daily brief, CRITICAL alerts, and CEO
  directives from the dashboard — async, never blocking autonomy.

## Architecture — and why

One ADK **Workflow graph** on Cloud Run, fired by Cloud Scheduler (pulse every
5 min + nightly + replenish) and Eventarc. See [`docs/architecture.mmd`](docs/architecture.mmd) (simple) and [`docs/architecture-detailed.mmd`](docs/architecture-detailed.mmd) (with the real tech).

**The core decision: the *code* holds the tools; the *model* holds the
judgment.** A conventional agent hands the LLM a set of tools and lets it call
them. We inverted that: `rules.py` gathers signals deterministically, the
**Strategist** (Gemini) emits a *structured decision* (one of a fixed set of
capped action types), `guardrails.py` validates it against hard limits in code,
and executor pipelines act — the LLM never touches a tool it could misuse.

Why this over a tool-calling agent:
- **Safety by construction** — the blast radius is "which of N capped actions,
  for which game," not "whatever the model decided to call." Money-adjacent and
  irreversible operations (minting, transactions, expiry math) are deterministic
  code with unit tests; the LLM only chooses *which safe thing, when*.
- **Cost & determinism** — no tool-call loops; a pulse with no signal spends
  zero tokens.
- **Testability** — decisions are gradeable in isolation (see eval), which a
  free-roaming tool-caller makes hard.

This is Google's own [`ambient-expense-agent`](https://github.com/google/adk-samples)
pattern (business rules in code, LLM for judgment only) — and it is literally
the Taskmaster brief: a workflow, not a chatbot. Trade-off we accept: the
Strategist can't *investigate* ad hoc (fetch extra data mid-decision). At this
data scale that's unnecessary; read-only decision tools are a documented Future
Phase.

Everything flows through a **Firestore ledger** — system of record,
fault-recovery substrate (idempotent leased tasks, retry, dead-letter), and the
live feed behind Mission Control. The executor is time-bounded so a long Veo/
Runpod job can't exceed the trigger deadline; remaining work self-paces across
pulses.

## Is it working? Measurable & validated

Two honest levels:

- **Functional (measured, verified):** live Impact numbers on the dashboard —
  autonomous actions executed, decisions, guardrail blocks, codes minted &
  claimed, dead codes quarantined. Levels verified **on-device** in both games.
  100 codes minted via API; claim funnel verified end-to-end.
- **Outcome (engagement/retention lift):** *not yet measurable* — near-zero
  users, GA→BigQuery data just started. Not fabricated. The measurement is
  **instrumented and ready** (per-code sent→claimed→redeemed funnel, GA
  `level_finish`/`level_skipped`, Reflector evidence tags); it proves itself as
  users scale.

**Validation:** unit tests (expiry rules, caps, SVG contract, memory discipline) ·
`agents-cli eval` behavioral suite (decision quality, channel compliance, idle
efficiency, reflector restraint) · resilience proven live (retry → dead-letter)
against the deployed
agent (dead-letter, retry-recovery, idempotency, guardrail rejection).

## Google Cloud stack

Cloud Run · Cloud Scheduler · Eventarc · Firestore · BigQuery (GA export +
billing export) · Vertex AI (Gemini + Veo) · Firebase Hosting/Auth/FCM · Secret
Manager · Cloud Monitoring. Framework: **ADK**. Model: **Gemini 3.7 Flash**.

See [SETUP.md](SETUP.md) for full spin-up.

## Run it

```bash
uv sync
agents-cli run "pulse"      # one decision cycle, locally (DRY_RUN)
agents-cli eval run         # graded behavior
uv run pytest tests/unit    # deterministic core (24 tests)
```

Deploy: `agents-cli deploy` (Cloud Run) + three Cloud Scheduler triggers +
Eventarc. Secrets in Secret Manager; `.env` carries only non-secret config.

## Repo layout

```
agent/
  agent.py          workflow graph — the decision spine (time-bounded executor)
  rules.py          signal detection + summaries (code)
  strategist.py     decision LLM (structured output)
  reflector.py      nightly learning LLM
  guardrails.py     hard caps (code)
  state.py          Firestore: ledger · leased queue · playbook · directives
  pipelines/        executors: levels (subliminal/moviequiz) · codes · replenish
  tools/            GA · BigQuery · Runpod · Veo · Firestore · FCM · ASC · mailbox
dashboard/          Mission Control (Vite + Firestore listeners)
tests/              unit + eval
docs/               architecture diagram
```

## Honest limitations (Future Phases)

- **Outcome KPIs** await a real user base to move (measurement is built).
- **Google Play minting** is Console-only (no API, by Google's design) — handled
  via the agent-driven reply-to-restock email loop, not full headless
  automation.
- **No CI/CD** yet (scaffolded with `skip`); long-running generation is bounded
  but not yet async (Cloud Tasks) — both are clean next steps.
