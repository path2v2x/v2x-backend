#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SOURCE="$TMP/source"
EVIDENCE="$TMP/evidence"
DESTINATION="$TMP/destination"
mkdir -p "$SOURCE/Content/Berkley/ImportedMesh" "$EVIDENCE" "$DESTINATION/Berkley/ImportedMesh"
git -C "$SOURCE" init -q
git -C "$SOURCE" config user.name test
git -C "$SOURCE" config user.email test@example.invalid

for index in $(seq 1 29); do
  path="Content/Berkley/ImportedMesh/object-$index.uasset"
  printf 'verified-object-%s\n' "$index" >"$SOURCE/$path"
  hash="$(sha256sum "$SOURCE/$path" | awk '{print $1}')"
  printf 'OK\t%s\t%s\t%s\n' "$hash" "$hash" "$path" >>"$EVIDENCE/hash-verification.tsv"
done
git -C "$SOURCE" add Content
git -C "$SOURCE" commit -qm fixture

printf 'old-object\n' >"$DESTINATION/Berkley/ImportedMesh/object-1.uasset"
old_hash="$(sha256sum "$DESTINATION/Berkley/ImportedMesh/object-1.uasset" | awk '{print $1}')"

V2X_RICHMOND_EXPECTED_COUNT=29 "$ROOT/scripts/migrate-richmond-road-core.sh" \
  --source-root "$SOURCE" --evidence-dir "$EVIDENCE" \
  --destination-content "$DESTINATION" >"$TMP/dry-run.log"
grep -Fq 'preflight=pass mutation=none' "$TMP/dry-run.log"
[[ "$(sha256sum "$DESTINATION/Berkley/ImportedMesh/object-1.uasset" | awk '{print $1}')" == "$old_hash" ]]

V2X_RICHMOND_EXPECTED_COUNT=29 "$ROOT/scripts/migrate-richmond-road-core.sh" \
  --source-root "$SOURCE" --evidence-dir "$EVIDENCE" \
  --destination-content "$DESTINATION" --execute >"$TMP/execute.log"
run_dir="$(sed -n 's/^migration=pass evidence=\([^ ]*\) rollback=.*/\1/p' "$TMP/execute.log")"
[[ -n "$run_dir" && -f "$run_dir/report.json" ]]
jq -e '
  .schema == "v2x-richmond-road-core-migration/v1"
  and (.objects | length) == 29
  and ([.objects[].action] | index("replaced") != null)
' "$run_dir/report.json" >/dev/null
[[ "$(sha256sum "$run_dir/rollback-content/Berkley/ImportedMesh/object-1.uasset" | awk '{print $1}')" == "$old_hash" ]]
(cd "$run_dir" && sha256sum -c evidence-sha256.txt >/dev/null)

printf 'tampered\n' >"$SOURCE/Content/Berkley/ImportedMesh/object-2.uasset"
if V2X_RICHMOND_EXPECTED_COUNT=29 "$ROOT/scripts/migrate-richmond-road-core.sh" \
  --source-root "$SOURCE" --evidence-dir "$EVIDENCE" \
  --destination-content "$DESTINATION" >"$TMP/tampered.log" 2>&1; then
  echo "tampered source unexpectedly passed" >&2
  exit 1
fi
grep -Fq 'Source object hash mismatch' "$TMP/tampered.log"

echo "Richmond road-core migration tests passed"
