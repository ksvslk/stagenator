#!/usr/bin/env bash
# Production deploy for Stagenator — ALWAYS pins prod runtime env.
#
# Why this exists: `agents-cli deploy` bundles the local .env (DRY_RUN=true,
# STAGENATOR_COLLECTION_PREFIX=stagenator_eval) into the image. Without the
# override below, prod would boot in dry-run (agent does nothing) or on the eval
# collection prefix (dashboard reads empty). This script makes forgetting that
# impossible.
set -euo pipefail

PROJECT=operation-sunrise
REGION=us-central1
SA=stagenator@operation-sunrise.iam.gserviceaccount.com
# RUNPOD_API_KEY (latest=v3): run-capable, endpoint-scoped. RUNPOD_BALANCE_KEY (v2):
# account-read key used ONLY for balance/cost tracking — the two scopes need two keys.
SECRETS="ASC_KEY_CONTENT=stagenator-asc-key:latest,GMAIL_APP_PASSWORD=stagenator-gmail-app-password:latest,RUNPOD_API_KEY=stagenator-runpod-key:latest,RUNPOD_BALANCE_KEY=stagenator-runpod-key:2"

echo "→ deploying agent…"
agents-cli deploy --project "$PROJECT" --region "$REGION" \
  --service-account "$SA" --secrets "$SECRETS"

echo "→ pinning prod runtime env (DRY_RUN=false, no eval prefix)…"
gcloud run services update stagenator --project "$PROJECT" --region "$REGION" \
  --remove-env-vars STAGENATOR_COLLECTION_PREFIX \
  --update-env-vars "DRY_RUN=false,OWNER_EMAIL=indrekl@gmail.com,MODEL_NAME=gemini-3.7-flash" \
  --cpu-throttling --max-instances=4 --memory=2Gi

echo "✓ deployed. Verifying prod env (HARD gate — deploy fails if wrong)…"
ENV=$(gcloud run services describe stagenator --project "$PROJECT" --region "$REGION" \
  --format="value(spec.template.spec.containers[0].env)")
echo "$ENV" | grep -q "'DRY_RUN', 'value': 'false'" || { echo "✗ DRY_RUN is not false — ABORT"; exit 1; }
echo "$ENV" | grep -q "STAGENATOR_COLLECTION_PREFIX" && { echo "✗ eval prefix still set — ABORT"; exit 1; }
echo "  env OK: DRY_RUN=false, no eval prefix"

echo "→ running deployment health check…"
URL=$(gcloud run services describe stagenator --project "$PROJECT" --region "$REGION" --format="value(status.url)")
TOKEN=$(gcloud auth print-identity-token)
curl -s -o /dev/null -w "  health trigger: HTTP %{http_code}\n" -X POST "$URL/triggers/health" \
  -H "Authorization: Bearer $TOKEN" -d "health:deploy" || true
echo "  → see stagenator_playbook/health (Mission Control 'Health' panel) for the result"
