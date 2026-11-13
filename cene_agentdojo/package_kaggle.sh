#!/usr/bin/env bash
# Build the Kaggle Dataset zip of this repo (the repo is private, so the notebook cannot clone it).
#   bash cene_agentdojo/package_kaggle.sh [POLICY_TAG] [RUN_TAG]   -> dist/cene-agentdojo-<run tag>.zip
# The files come from RUN_TAG. The script refuses to package if the policy files differ from POLICY_TAG.
# REVISION.json records both tags, their commits and the model the run setup serves.
set -euo pipefail
POLICY_TAG="${1:-agentdojo-policy-v1}"
RUN_TAG="${2:-agentdojo-run-r4-model2}"
cd "$(git rev-parse --show-toplevel)"
POLICY_COMMIT="$(git rev-list -n 1 "$POLICY_TAG")"
RUN_COMMIT="$(git rev-list -n 1 "$RUN_TAG")"
git merge-base --is-ancestor "$POLICY_COMMIT" "$RUN_COMMIT" || { echo "policy tag is not an ancestor of run tag"; exit 1; }
POLICY_FILES="cene cene_agentdojo/contracts cene_agentdojo/policy.py cene_agentdojo/adapter.py cene_agentdojo/hijack.py"
git diff --quiet "$POLICY_TAG" "$RUN_TAG" -- $POLICY_FILES || { echo "policy files changed since $POLICY_TAG"; exit 1; }
# The model this run setup serves, from run_config.yaml at RUN_TAG (model2.chosen, else the first serving option).
MODEL="$(git show "$RUN_TAG:cene_agentdojo/run_config.yaml" | python -c "import sys, yaml; c = yaml.safe_load(sys.stdin); o = c['serving']['order'][0]; print((c.get('model2') or {}).get('chosen') or o.get('name', o['folder']))")"
mkdir -p dist
OUT="dist/cene-agentdojo-${RUN_TAG#agentdojo-}.zip"
TMP="$(mktemp -d)"
git archive --format=tar "$RUN_TAG" | tar -x -C "$TMP"
printf '{"tag": "%s", "policy_tag": "%s", "policy_commit": "%s", "run_tag": "%s", "run_commit": "%s", "commit": "%s", "model": "%s", "policy_files_unchanged": true, "packaged": "%s"}\n' \
  "$POLICY_TAG" "$POLICY_TAG" "$POLICY_COMMIT" "$RUN_TAG" "$RUN_COMMIT" "$RUN_COMMIT" "$MODEL" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$TMP/REVISION.json"
rm -f "$OUT"
(cd "$TMP" && python -c "import shutil,sys; shutil.make_archive(sys.argv[1], 'zip', '.')" "$OLDPWD/${OUT%.zip}")
rm -rf "$TMP"
echo "$OUT  policy=$POLICY_TAG@$POLICY_COMMIT run=$RUN_TAG@$RUN_COMMIT model=$MODEL"
sha256sum "$OUT"
