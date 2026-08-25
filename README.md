# Stagenator

**An autonomous engagement & retention agent for three live mobile games** —
Subliminal Words, AI Movie Quiz, and Palindrome. It watches Google Analytics,
decides what will grow engagement, and acts on its own: shipping new levels,
distributing promo/offer codes, keeping code inventory alive, and learning day
by day — with no human in the loop and full observability.

> All Things Agentic Hackathon · **Taskmaster** track — *a complete workflow,
> not a chatbot.*

**Live:** Mission Control → https://stagenator-mission.web.app (owner-only)

---

## What it does, autonomously

- **Ships levels.** Designs and generates real game content — Subliminal Words
  puzzles (Runpod/ComfyUI + a self-drawn solution SVG), AI Movie Quiz clips
  (Veo 3.1 Lite, with visual/ambience/dialogue difficulty strategies),
  Palindrome curation (Gemini, 18-locale hints) — validates it (schema +
  vision QA + dedup vs. bundled content), and submits it via each game's exact
  write path. New levels trigger the games' push notifications.
- **Delivers codes.** Distributes Google Play promo codes and Apple offer codes
  through **proffer.codes** with no-sign-in tokened claim links. Apple codes are
  **minted fully via the App Store Connect API**; Google Play (which has no
  minting API) is a reply-to-restock email loop the agent drives itself.
- **Keeps inventory alive.** A daily audit knows every store's expiry rule
  (Apple 28-day, Play promotion-end) and quarantines dead codes before a player
  ever hits one — the problem that had silently broken every campaign since
  January.
- **Learns.** A nightly Reflector compares yesterday's actions to outcomes and
  rewrites a playbook that steers tomorrow's decisions — with statistical
  humility at low sample sizes.
- **Reports & takes direction.** Emails a daily brief, alerts on anything
  critical, and accepts CEO directives from the dashboard — async, never
  blocking autonomy.

## Architecture

See [`docs/architecture.mmd`](docs/architecture.mmd). One ADK **Workflow graph**
on Cloud Run, fired by Cloud Scheduler (every 5 min + nightly) and Eventarc.

**The key design choice:** the *code* has the tools; the *model* has the
judgment. Signals are gathered deterministically, the **Strategist** (Gemini)
emits a structured decision, guardrails validate it against hard caps in code,
and executor pipelines act — the LLM never holds a tool it could misuse. This is
Google's `ambient-expense-agent` pattern (business rules in code, LLM only for
judgment), and it makes the system safe, cheap (idle ticks cost zero tokens),
auditable, and testable.

Everything flows through a **Firestore ledger** — the system of record, the
fault-recovery substrate (idempotent tasks, retry, dead-letter), and the live
feed behind Mission Control.

## Google Cloud stack

Cloud Run · Cloud Scheduler · Eventarc · Firestore · BigQuery · Vertex AI
(Gemini + Veo) · Firebase Hosting/Auth/FCM · Secret Manager · Cloud Monitoring.
Agent framework: **ADK**. Model: **Gemini** (3.7-flash for production judgment).

## Verification

- `uv run pytest tests/unit` — deterministic core (expiry rules, caps,
  palindrome/SVG contracts): **23 tests**.
- `agents-cli eval run` — LLM-judged decision quality, channel compliance,
  idle efficiency, reflector restraint: **all pass**.
- **Live fault drills** — dead-letter, retry-recovery, idempotency, and
  guardrail rejection, verified against the deployed agent.

## Run it

```bash
uv sync
agents-cli run "pulse"            # one decision cycle, locally
agents-cli eval run              # graded behavior
uv run pytest tests/unit         # deterministic core
```

Deploy: `agents-cli deploy` (Cloud Run) + the three Cloud Scheduler triggers.
Credentials live in Secret Manager; `.env` carries only non-secret config.

## Repo layout

```
agent/
  agent.py          workflow graph — the decision spine
  rules.py          signal detection (code)
  strategist.py     decision LLM (structured output)
  reflector.py      nightly learning LLM
  guardrails.py     hard caps (code)
  state.py          Firestore: ledger · queue · playbook · directives
  pipelines/        executors: levels · codes · replenish
  tools/            GA · Runpod · Veo · Firestore · FCM · ASC · Play · mailbox
dashboard/          Mission Control (Vite + Firestore listeners)
tests/              unit + eval
docs/               architecture diagram
```
