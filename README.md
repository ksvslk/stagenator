# Stagenator

**A fully automatic caretaker for three live mobile games.** No human presses any
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
- **Palindrome** — unscramble a phrase that reads the same both ways ·
  [App Store](https://apps.apple.com/app/hah-palindrome-puzzles/id1673006365) · [Google Play](https://play.google.com/store/apps/details?id=com.indest.hah)

## What it does on its own

- **Checks in every 5 minutes.** Most check-ins find nothing and cost nothing —
  doing nothing on purpose is a normal outcome. The AI is only woken when
  someone is playing.
- **Ships levels — multimodal, or provably-correct text.** Text becomes an image
  (a word hidden in a photo, via ControlNet) or an 8-second film with sound
  (Veo 3.1). Then the model switches roles and inspects its own output: Gemini
  **vision** judges the picture (the word must be subtle — not printed, not
  invisible), Gemini **video understanding** watches the clip (recognizable, no
  title text, no actor likeness). Only then is it saved, all-or-nothing, and
  re-verified. Palindrome is the opposite extreme — no media to inspect, so
  correctness is *proven in code* (normalize to letters, compare with its
  reverse); the model only proposes candidates, screens them for a kids'
  audience, and writes the localized hints, and every survivor is re-checked
  and deduped against the ~900 already in the game.
- **Gifts promo codes.** Apple codes are minted via the App Store Connect API;
  Google Play (which has no minting API) runs through an email loop the agent
  drives from its own inbox. Every code that leaves the shelf is bound to
  exactly one recipient. Delivery runs through **[proffer.codes](https://proffer.codes)** —
  our own claim site, wired into the same system — which doubles as
  cross-promotion: a player picking up one game's gift sees the other games'
  available codes right there.
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
away. One thing it deliberately cannot do: remove content — taking a bad
level down is a human decision.

**Why not a tool-calling agent, or more agents?** The action menu is small, fixed,
and high-stakes — minting codes with monetary value, pushing to strangers' phones,
publishing into store apps, unattended. For that profile, "the model fills in a form,
tested code executes behind hard limits" beats handing the model tools: the blast
radius is *which of five safe things, when* — never *whatever the model decided to
call*. This is ADK's own paved road, not a workaround: the workflow graph and
schema-locked `LlmAgent` output are first-class ADK features, and Google's
`ambient-expense-agent` sample uses the same pattern (business rules in code, the
model for judgment). Under the surface there are ~eleven specialist model roles —
strategist, reflector, three level designers, two visual inspectors, a content-safety
screener, a gift selector, an error diagnostician — orchestrated by code instead of a
manager-LLM, which is the component that drifts and gets prompt-injected. Three ADK idioms are deliberately
bypassed (tool callbacks, the tool system, session state) because the model holds no
tools; an independent conformance review confirmed each deviation is deliberate.
The boundary is explicit and grows on evidence, not vibes: when the graded eval says
a role is overloaded, it splits; when signals outgrow pre-gathered context, the
Strategist gains read-only lookups — eyes before hands.

Everything runs as **one ADK Workflow graph on Cloud Run** (asleep and free
when idle), woken every 5 minutes by Cloud Scheduler. Every action is a job on a
crash-proof to-do list: a crash mid-job is retried, a failure retries
three times then gives up loudly, and a failed attempt never burns the daily
budget. All state lives in Firestore and feeds the dashboard live.

## Status

- **Working:** levels generated and visible on-device in all three
  games (Palindrome ships text levels straight into the live player-level feed);
  Apple + Google promo codes minted and audited nightly for expiry; the claim
  pages tested end-to-end; the failure path exercised in production (a dead
  Runpod key was caught, alerted, and fixed the same evening).
- **Not yet claimable:** engagement/retention lift — the games have near-zero
  users so far. The measurement is built (per-code claim funnel, play events,
  nightly review); it proves itself as players arrive. The dashboard shows only measured numbers.
- **Tested:** 51 unit + 28 resilience tests (against the Firestore
  emulator) and a graded `agents-cli eval` suite — 4/4 scenarios at maximum
  scores.

## Built to grow

Everything except level-making is shared machinery keyed by a per-game config
entry — watching players, deciding, the hard limits, the job queue, code gifts
on proffer.codes, the A/B experiments, health checks, nightly learning. Adding
a game costs one config entry plus, at most, one content pipeline — Palindrome
was the proof: a third game brought online mid-project on one config entry and
one small text pipeline, sharing everything else unchanged. The next three
games in the same portfolio, in order of effort:

- **Trivia Player** — already has push notifications, so it plugs straight in:
  the agent schedules in-game events through Firebase Remote Config and
  announces them — one new tool, one new action type, same caps.
- **Penalty 2D** — promo codes only: on the agent side just a config entry,
  but the game needs one app update first to add push support (it has none
  today) so the codes can reach players.
- **Snackroach** — the honest hard case, twice over: needs the push update,
  and its levels are built in a game editor today, so the agent can take over
  level-making only once levels become data files the game reads.

## Stack

Cloud Run · Cloud Scheduler · Firestore · BigQuery (GA4 export) ·
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
  request deadline; games needing levels in the same moment are served a
  few minutes apart.

---

Questions about any part? Every section above is one picture on the
**[blueprints page](https://stagenator-mission.web.app/blueprints.html)**.
