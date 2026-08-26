# Stagenator

**A fully automatic caretaker for two live mobile games.** No human presses any
buttons: it watches who is playing right now, decides what would help, creates
brand-new game levels with AI and ships them straight into the games, can gift
each player their own promo code, and learns from results every night.

> All Things Agentic Hackathon · **Taskmaster** track — *a complete workflow, not a chatbot.*

### 👉 Start here: **[How it works — 10 diagrams, plain language](https://stagenator-mission.web.app/blueprints.html)**

The whole system in ten pictures, simplest first — five minutes to understand
everything below. Then watch it live on the
**[dashboard](https://stagenator-mission.web.app)**: every decision, action,
and mistake, as it happens.

These are **live games you can install right now** — the agent ships into apps
with players on both stores:

- **Subliminal Words** — find the word hidden inside a photo ·
  [App Store](https://apps.apple.com/app/subliminal-words/id6468366578) · [Google Play](https://play.google.com/store/apps/details?id=com.indest.subliminalwords)
- **AI Movie Quiz** — guess the film from an 8-second AI clip ·
  [App Store](https://apps.apple.com/app/ai-movie-quiz/id6752119990) · [Google Play](https://play.google.com/store/apps/details?id=com.indest.aimoviequiz)

## What it does on its own

- **Checks in every 5 minutes.** Most check-ins find nothing and cost nothing —
  doing nothing on purpose is a normal outcome. The AI is only woken when
  someone is playing.
- **Ships levels — multimodal end to end.** Text becomes an image (a word
  hidden in a photo, via ControlNet) or an 8-second film with sound (Veo 3.1).
  Then the model switches roles and inspects its own output: Gemini **vision**
  judges the picture (the word must be subtle — not printed, not invisible),
  Gemini **video understanding** watches the clip (recognizable, no title text,
  no actor likeness). Only then is it saved, all-or-nothing, and re-verified.
- **Gifts promo codes.** Apple codes are minted via the App Store Connect API;
  Google Play (which has no minting API) runs through an email loop the agent
  drives from its own inbox. Every code that leaves the shelf is bound to
  exactly one recipient.
- **Learns overnight.** A nightly review compares actions to outcomes and
  rewrites a size-capped playbook that steers the next day. With too little
  data it deliberately changes nothing.
- **Tells the owner when something breaks.** A failure emails within minutes —
  with the agent's own guess at the cause — and shows as a red row in the live
  feed. A daily health check calls every service it depends on.

## How it's built

**Code does the doing, AI does the thinking — and the creating.** The LLM is
consulted for exactly two decisions (what to do now; what to learn tonight) and
answers a fixed form. Everything that moves money or changes the games is
plain, tested code behind hard limits — at most 1 level and 1 code gift per
game per day. When the AI creates (level designs, images, video, quality
checks), it does so inside pipelines that code starts, checks, and can throw
away.

Everything runs as **one ADK Workflow graph on Cloud Run** (asleep and free
when idle), woken by Cloud Scheduler and Eventarc. Every action is a job on a
crash-proof to-do list: a crash mid-job is retried, a failure retries
three times then gives up loudly, and a failed attempt never burns the daily
budget. All state lives in Firestore and feeds the dashboard live.

## Status

- **Working:** levels generated and visible on-device in both
  games; 100 Apple codes minted via API; the claim pages tested end-to-end;
  the failure path exercised in production (a dead Runpod key was caught,
  alerted, and fixed the same evening).
- **Not yet claimable:** engagement/retention lift — the games have near-zero
  users so far. The measurement is built (per-code claim funnel, play events,
  nightly review); it proves itself as players arrive. The dashboard shows only measured numbers.
- **Tested:** 19 unit + 24 resilience tests (against the Firestore
  emulator) and a graded `agents-cli eval` suite — 4/4 scenarios at maximum
  scores.

## Stack

Cloud Run · Cloud Scheduler · Eventarc · Firestore · BigQuery (GA4 export) ·
Vertex AI (Gemini 3.7 Flash + Veo) · Firebase Hosting/Auth/FCM · Secret
Manager · Runpod/ComfyUI. Framework: **ADK**. Full spin-up: [SETUP.md](SETUP.md).

## Run it

```bash
uv sync
agents-cli run "pulse"      # one decision cycle, locally (DRY_RUN)
agents-cli eval run         # graded behavior
uv run pytest tests/unit    # deterministic core
```

## Repo layout

```
agent/
  agent.py       the workflow graph
  strategist.py  decision AI (fixed-form output)
  reflector.py   nightly learning AI
  guardrails.py  hard limits, in code
  rules.py       signal detection
  state.py       Firestore: diary · job queue · playbook
  pipelines/     level generation · code gifts · restock · housekeeping
  tools/         analytics · Runpod · Veo · push · App Store · Gmail
dashboard/       Mission Control + /blueprints.html
tests/           unit · resilience (emulator) · eval
```

## Known limits

- Google Play code minting is Console-only by Google's design — hence the
  email loop, the one human touchpoint.
- Long AI generations self-pace across check-ins to stay inside Cloud Run's
  request deadline; two games needing levels in the same moment are served a
  few minutes apart.

---

Questions about any part? Every section above is one picture on the
**[blueprints page](https://stagenator-mission.web.app/blueprints.html)**.
