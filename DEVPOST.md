# Stagenator — Devpost Submission

## Inspiration
In 2022, I started making apps solo and released a game (**Palindrome**), which was featured multiple times in the Google Play Games app. In 2023, I released **Subliminal Words**, featured under "New Games We Love". I released more games (like **AI Movie Quiz**), but once the promotional traffic dried up, daily active users dropped near zero.

The hard truth of indie app development: without continuous store promotion or a big marketing budget, your apps stagnate. Keeping games fresh with new content takes full-time effort, but I can't put my days into these games anymore. 

An autonomous agent can. 

Enter **Stagenator** — a 24/7 AI caretaker that brings my live mobile game portfolio out of stagnation.

## What it does
Stagenator autonomously manages two live store games — **Subliminal Words** and **AI Movie Quiz** — on iOS and Android with zero human intervention.

- **Instant Player Reaction & 5-Minute Heartbeat:** When traffic is near zero, every single player visit is precious. If a user opens the app and leaves 2 minutes later, a standard cron job is too late. Stagenator uses **Eventarc** to wake up *instantly* the second a player arrives, backed by a 5-minute Cloud Scheduler heartbeat. It immediately generates custom content or delivers a gift code while the player is still active on-screen.
- **Multimodal Content Generation:** Creates new levels on the fly (ControlNet images with hidden words for Subliminal Words, or 8-second Veo 3.1 AI video clips with audio for AI Movie Quiz).
- **Closed-Loop Quality Control:** Before publishing, Gemini 3.7 Vision & Video Understanding inspects the generated media to enforce quality (e.g., verifying word subtlety, ensuring no actor likeness or overlay text).
- **Player Engagement & Gifting:** Gifts mintable Apple App Store promo codes or Google Play gifts delivered via personal claim links on proffer.codes.
- **A/B Testing & Self-Reflecting Playbook:** Drafts push notifications in dual variants, measures claim funnel conversions, and runs a nightly reflection step to update its operational playbook for the next day.
- **Self-Healing & Diagnostics:** If an external service breaks, it emails me within minutes with an auto-diagnosed root cause.

👉 **Live Mission Control Dashboard:** https://stagenator-mission.web.app (view live decisions, levels, and operational telemetry).

## How we built it
Built on **Google ADK (Agent Development Kit)** and deployed natively to **Google Cloud**:

- **Orchestration:** Built as an **ADK 2.0 Workflow Graph** running on **Cloud Run**, woken up serverlessly by **Cloud Scheduler** and **Eventarc**.
- **Model Roles (~8 Specialist Prompts):** Rather than a single monolithic prompt, we split intelligence into specialized roles orchestrated by code: Strategist, Reflector, Level Designers, Visual Inspectors, Gift Selectors, and Error Diagnosticians.
- **Generative & Vision Stack:** Vertex AI (Gemini 3.7 Flash + Veo 3.1), Runpod/ComfyUI (ControlNet), Firebase (Hosting, Auth, FCM, Firestore), BigQuery (GA4 export), and App Store Connect API.
- **Safety & Guardrails:** Code does the doing; AI does the thinking. The LLM returns structured schema-locked outputs (LlmAgent). Hard code-level guardrails enforce strict daily budgets (e.g., max 1 level and 1 gift per game per day) to prevent runaway costs or store spam.
- **Development & Testing:** Developed using Google ADK skills (agents-cli-workflow, adk-code, deploy, eval, scaffold). Verified with 19 unit tests, 24 Firestore emulator resilience tests, and a graded agents-cli eval benchmark (passing 4/4 test scenarios).

## Challenges we ran into
1. **Architectural Choice — Why Tool-Calling Agents are Overkill:**
   Standard ADK tutorials showcase multi-agent networks where models call tools dynamically. In our unattended 24/7 production scenario, tool-calling agents would be complete overkill and counterproductive:
   - **Cost & Token Waste:** With near-zero daily active traffic, 99% of 5-minute check-ins find zero active players. A tool-calling agent would burn ~7,000 tokens per check-in across 8,640 monthly calls (~60 Million tokens/month) just to evaluate tool schemas and decide to do nothing.
   - **Uncontrolled Blast Radius:** Giving an LLM raw tool access (minting promo codes, pushing store notifications) creates real risks of hallucinated loops or prompt-drift on live apps.
   
   **Solution:** We chose an **ADK 2.0 Workflow Graph** on Cloud Run. Deterministic Python code handles schedule triggers, player telemetry checks ($0.00 idle cost), and hard daily caps. Gemini is invoked *only* when active players are detected, returning bounded, schema-locked decisions (`LlmAgent`). Code does the doing; AI does the thinking.
2. **Asynchronous Generation within Cloud Run Limits:**
   Generating 8-second Veo video clips takes time. To keep Cloud Run requests fast and cost-effective, we built a crash-proof Firestore job queue. Long AI jobs self-pace across 5-minute check-ins, allowing jobs to retry up to 3 times without burning daily content budgets.
3. **Store API Limitations (Google Play vs. Apple App Store):**
   Apple offers an official App Store Connect REST API to mint promo codes programmatically. Google Play, however, provides no public code-minting API (Console web UI only). We evaluated using Chrome DevTools MCP / headless browser automation to navigate Google Play Console and generate code batches automatically. However, Google account 2FA/MFA security challenges and session persistence in serverless Cloud Run made browser automation fragile. Instead, we built a reliable human-in-the-loop email pipeline: Stagenator monitors stock, sends a pre-formatted restock request when low, parses the developer's CSV email reply, and automatically ingests the codes into `proffer.codes`.
4. **Deep-Linking Limitations & Playbook Steering:**
   Push notifications sent via FCM carry custom `claimUrl` data payloads pointing to gift claim links on `proffer.codes`. However, existing live game binaries in the store lacked native deep-link tap handlers to directly open an external browser on tap. When we observed this, instead of hot-patching backend code or turning off servers, we simply issued a human directive to the agent's Playbook: *"do no code drops until further notice"*. Stagenator's Strategist immediately read the directive and paused all code-drop actions gracefully without redeploying code.

## Accomplishments that we're proud of
- **Built in Just 8 Days (Aug 20–28, 2026):** Went from initial concept and spec writing to a fully deployed, autonomous production agent managing two live store apps — complete with multimodal pipelines, unit/resilience tests, and eval suites — in just 8 days.
- **100% Live & Operational:** The agent is shipping real levels into active iOS and Android apps right now.
- **Full Mission Control Observability:** Built a real-time web dashboard ([stagenator-mission.web.app](https://stagenator-mission.web.app)) that streams every decision ledger entry, generated level preview, rejected action, and playbook directive live.
- **Keeping My Apps Alive Autonomously:** As a solo developer, it is deeply satisfying to watch brand-new levels, AI videos, and promo gifts automatically ship into production store apps without any daily human involvement. My game portfolio is alive again.
- **Real Production Self-Healing & Steering:** During testing, an external API key failed and was auto-diagnosed via email; when deep-linking needed adjustment, a simple plain-text directive steered the agent's behavior seamlessly.
- **Clean Architecture:** Achieved a perfect 4/4 on agents-cli eval behavioral benchmarks while keeping idle infrastructure cost near zero.

## What we learned
- **Conquering the Fear of Level Quality:** My biggest fear with an autonomous agent was that automated content quality wouldn't meet store standards. When I created levels manually with AI in the past, it was exhausting and time-consuming — I would overthink every prompt and setting searching for the "perfect" level. Watching Stagenator run autonomously — backed by Gemini Vision & Video understanding self-inspection — I realized the agent's levels are just as high quality as my hand-crafted ones. Automated quality control eliminated both my fear of bad content reaching players and the frustration of spending hours creating levels manually.
- **Cloud agents don't have to be expensive:** My biggest initial fear was that running an autonomous agent 24/7 in the cloud would burn through a high infrastructure budget. By combining serverless Cloud Run (which scales to 0 and costs $0.00 when idle) with event-driven ADK Workflow Graphs and Gemini Flash, 99% of idle check-ins cost nothing — making 24/7 production app caretaking cost pennies per day.
- **The Autonomy & Consistency Tradeoff:** Technically, I could generate levels locally on my MacBook Pro for $0 in cloud API fees. But manual creation requires constant human effort, and manually updating low-traffic apps without seeing immediate user spikes becomes demotivating over time. Autonomous agents do not suffer from demotivation — they operate with 100% relentless, continuous consistency 24/7. Spending a few cents on cloud APIs (Runpod + Vertex AI Veo) buys complete hands-off autonomy and relentless operational consistency that no human can maintain alone.
- **Code for Execution, LLMs for Reasoning:** Designing around schema-locked structured outputs (`LlmAgent`) rather than raw LLM tool execution provides vastly superior reliability for automated app management. I learned (again!) that there is a correct tool for every job — deterministic code for state machine control, schedules, and guardrails, and LLMs for bounded, structured reasoning.
- **The power of spec-driven AI development:** Writing a rigorous architecture specification upfront saved days of patching downstream logic.

## What's next for Stagenator
Stagenator's shared engine is built to scale across my entire app portfolio. Adding a new game only requires a single config entry and a content pipeline. Next up: integrating **Trivia Player**, **Penalty 2D**, **Palindrome**, and **Snackroach**!
