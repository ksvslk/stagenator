# Setup & Spin-up

Reproducible from a clean clone. Two paths: **run locally** (safe, `DRY_RUN`) and
**deploy to Google Cloud**.

## Prerequisites
- [uv](https://docs.astral.sh/uv/) · Google Cloud project with billing · `gcloud` authed
- A service account with: Datastore User, Storage Object Admin, FCM Admin,
  Vertex AI User, Secret Manager Secret Accessor
- GA4 properties shared (Viewer) with that service account

## 1. Install & smoke-test locally (no side effects)
```bash
uv sync
cp .env.example .env            # fill GOOGLE_CLOUD_PROJECT etc.; keep DRY_RUN=true
uv run pytest tests/unit        # 19 unit + 24 resilience tests
uv run --with ruff ruff check agent/
uv run python -c "from agent.agent import root_agent; print(root_agent.name)"
agents-cli run "pulse"          # one decision cycle, effects stubbed
agents-cli eval run             # graded behaviour
```

## 2. Secrets (never in code)
```bash
printf '%s' "$RUNPOD_KEY"   | gcloud secrets create stagenator-runpod-key --data-file=-
printf '%s' "$RUNPOD_EP"    | gcloud secrets create stagenator-runpod-endpoint --data-file=-
gcloud secrets create stagenator-asc-key  --data-file=AuthKey_XXX.p8   # App Store Connect
printf '%s' "$GMAIL_APP_PW" | gcloud secrets create stagenator-gmail-app-password --data-file=-
# grant the runtime SA secretAccessor on each
```

## 3. Deploy to Cloud Run + triggers
```bash
agents-cli deploy \
  --service-account SA_EMAIL \
  --secrets "ASC_KEY_CONTENT=stagenator-asc-key:latest,GMAIL_APP_PASSWORD=stagenator-gmail-app-password:latest,\
RUNPOD_API_KEY=stagenator-runpod-key:latest,RUNPOD_BALANCE_KEY=stagenator-runpod-key:2,RUNPOD_ENDPOINT_ID=stagenator-runpod-endpoint:latest"
# NOTE: prod actually deploys via ./deploy.sh, which pins these (run key = latest, balance key = v2).

# three Cloud Scheduler jobs (OIDC-authed to the private service)
for j in "pulse|*/5 * * * *" "nightly|10 3 * * *" "replenish|0 4 * * *"; do
  name=${j%%|*}; cron=${j##*|}
  gcloud scheduler jobs create http stagenator-$name \
    --schedule="$cron" --uri="$SERVICE_URL/triggers/$name" --http-method=POST \
    --oidc-service-account-email=SA_EMAIL --attempt-deadline=540s
done
```
Flip `DRY_RUN=false` only when you're ready for real actions. Optional data
sources: GA4→BigQuery export and Billing→BigQuery export (both enable the
BigQuery-backed panels).

## 4. Observe
Mission Control (`dashboard/`, Firebase Hosting, owner-locked) reads the live
Firestore ledger. Cloud Run logs stream the reasoning; `severity=CRITICAL` logs
drive the Cloud Monitoring email alert policy.
