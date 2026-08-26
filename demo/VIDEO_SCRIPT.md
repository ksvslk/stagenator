# Stagenator — 4-minute demo video script (one unedited take)

Target: ~4:00 · screen recording + phone camera insert · plain narration, no hype.
Judging beats to hit: autonomous action (40%) · architecture (30%) · live proof on GCP (30%).

## Prep (15 min before recording — do NOT skip)

- [ ] Dashboard open (light mode), phone with **Subliminal Words** installed next to you
- [ ] Second browser tab: Google Cloud console → Cloud Run service page (stagenator)
- [ ] Third tab: Cloud Scheduler jobs list · Fourth tab: the repo README
- [ ] Gmail open on the **CRITICAL diagnosis email** from the Runpod incident (search "Stagenator CRITICAL")
- [ ] Confirm today's level budget is FREE for subliminal-words (no level shipped today)
- [ ] Confirm Runpod balance > $1 (dashboard header dot green)
- [ ] **Start playing Subliminal Words on the phone ~3 minutes before you hit record**
      (GA realtime needs a minute or two to see you; the next 5-min pulse will fire the signal)
- [ ] Have a claim link ready as backup: the /drop/ page for AI Movie Quiz

## Timeline

**0:00 – 0:25 · The problem, over the dashboard header**
> "I have two small mobile games on the app stores. They were dying quietly —
> no new content, no reason to come back. I don't have time to run them.
> So this is Stagenator: it runs them for me. Completely. No buttons."
Show: dashboard header — name, green dot, "last run" ticking.

**0:25 – 0:55 · The live moment: it sees the player (you)**
> "That player it just noticed on iOS — that's my phone, I'm playing right now.
> Every five minutes a timer wakes it on Cloud Run. It saw a player, and decided."
Show: Activity feed — the SIGNAL row (user_active · count · breakdown), then the
DECISION row. **Read the why-not out loud** if present:
> "Notice it also says what it chose NOT to do, and why. Every decision explains itself."

**0:55 – 1:20 · The action starts (the slow-cooker)**
> "It decided to ship this player a brand-new level. Generation is running now —
> AI designs the word and the scene, paints the picture, then inspects its own
> output before anything is saved. We'll come back when it's done."
Show: ACTION row (level_pipeline · enqueued), Tasks card flips to 1 RUNNING.

**1:20 – 1:50 · Proof it's on Google Cloud (while it cooks)**
> "Under the hood: one ADK workflow graph on Cloud Run — asleep and free when
> idle. Cloud Scheduler wakes it. Firestore is its memory and its diary."
Show: Cloud Run service + revisions tab (real deploys), Scheduler jobs (pulse */5),
back to dashboard.

**1:50 – 2:25 · Architecture in one breath (blueprints page)**
> "The design rule: code does the doing, AI does the thinking — and the creating.
> The model answers a fixed form; hard limits in code have the last word:
> one level and one code gift per game per day, at most."
Show: /blueprints.html — scroll diagram 2 (the check-in) and 3 (code vs AI). Ten seconds each.

**2:25 – 2:55 · When it breaks, it tells on itself**
> "Two nights ago a credential silently lost permission. The agent tried three
> times, gave up, marked the failure in the feed — and emailed me within minutes,
> with its own guess at the cause. That guess was correct."
Show: the red ERROR row in the feed history (or the dead task record), then the
Gmail CRITICAL email with the "likely cause / try" lines. This is the beat no
slide can fake.

**2:55 – 3:20 · Codes: a gift bound to one person**
> "It can also gift promo codes — minted through the App Store Connect API.
> Each code is reserved for exactly one person."
Show: open the claim/drop page, tear a code on camera, show the code appear.

**3:20 – 3:50 · The payoff: the level lands in the game**
> "And the level it started making three minutes ago —"
Show: Stages created card → the new level with its puzzle image · then the
**phone**: open Subliminal Words, show the new level playable.
> "— is on my phone. In the store app. Nobody touched anything."

**3:50 – 4:00 · Close**
> "It watches, decides, creates, ships, learns overnight, and emails me when
> something breaks. Repo, setup, and the ten-diagram explanation are linked.
> Stagenator — pulling my app portfolio out of stagnation."
Show: README top (Start here → blueprints), dashboard one last time.

## Contingencies (rehearse these)

- **Signal doesn't fire in time** → fall back to showing yesterday's real
  signal→decision→action rows in the feed history; narrate identically ("here is
  the exact moment it saw a player").
- **Generation slower than 3 min** → swap beats: do the phone-payoff LAST even
  past 4:00 slightly, or show the most recent shipped level on-device instead
  ("this one shipped itself yesterday").
- **Generation fails on camera** → gold, not disaster: point at the red row +
  the email arriving. "This is what failure looks like — loud, explained, and
  budget-free to retry."
- **Cap already used today** → narrate the rejection: the gate refusing IS the
  demo of hard limits.

## Rules for the take

- One take, no cuts (the brief says unedited). Rehearse twice.
- Never say "honest/real/genuinely" — just show.
- If something unexpected appears in the feed, read it — unscripted reality is
  the strongest material this system has.
