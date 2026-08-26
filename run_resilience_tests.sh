#!/usr/bin/env bash
# Resilience proof: happy + unhappy queue paths against a real Firestore emulator.
set -euo pipefail
JAR=$(ls ~/.cache/firebase/emulators/cloud-firestore-emulator-*.jar 2>/dev/null | head -1)
[ -z "$JAR" ] && { echo "Install the Firebase Firestore emulator first: firebase setup:emulators:firestore"; exit 1; }
java -jar "$JAR" --host=localhost --port=8919 >/tmp/fsemu.log 2>&1 &
EMU=$!; trap "kill $EMU 2>/dev/null" EXIT
for i in $(seq 1 40); do curl -s localhost:8919 >/dev/null 2>&1 && break; sleep 1; done
FIRESTORE_EMULATOR_HOST=localhost:8919 GOOGLE_CLOUD_PROJECT=demo-test STAGENATOR_COLLECTION_PREFIX=restest \
  uv run pytest tests/integration/test_resilience.py -v
