#!/usr/bin/env bash
set -euo pipefail

# Hash-gated, rollback-preserving migration of the retained Richmond road core
# into an isolated CARLA UE5 project.  The default is an observational dry run.

SOURCE_ROOT="${V2X_RICHMOND_SOURCE_ROOT:-/mnt/v2x-ue5/projects/richmond-ue4-source}"
EVIDENCE_DIR="${V2X_RICHMOND_EVIDENCE_DIR:-/mnt/v2x-ue5/evidence/april-road-core-dependencies}"
DESTINATION_CONTENT="${V2X_CARLA_CONTENT_ROOT:-/mnt/v2x-ue5/src/carla-ue5/Unreal/CarlaUnreal/Content}"
EXPECTED_COUNT="${V2X_RICHMOND_EXPECTED_COUNT:-29}"
EXECUTE=false

usage() {
  cat <<'EOF'
Usage: migrate-richmond-road-core.sh [options]

Options:
  --source-root PATH          UE4 source checkout containing Content/Berkley
  --evidence-dir PATH         Directory containing hash-verification.tsv
  --destination-content PATH  Isolated CARLA Unreal Content directory
  --execute                   Copy after all preflight checks pass
  --help                      Show this help

Without --execute, the script validates every source object and reports the
planned destination without changing it.
EOF
}

while (( $# )); do
  case "$1" in
    --source-root)
      SOURCE_ROOT="${2:?missing value for --source-root}"
      shift 2
      ;;
    --evidence-dir)
      EVIDENCE_DIR="${2:?missing value for --evidence-dir}"
      shift 2
      ;;
    --destination-content)
      DESTINATION_CONTENT="${2:?missing value for --destination-content}"
      shift 2
      ;;
    --execute)
      EXECUTE=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! [[ "$EXPECTED_COUNT" =~ ^[1-9][0-9]*$ ]]; then
  echo "Expected object count must be a positive integer" >&2
  exit 2
fi

MANIFEST="$EVIDENCE_DIR/hash-verification.tsv"
[[ -d "$SOURCE_ROOT" ]] || { echo "Source root is missing: $SOURCE_ROOT" >&2; exit 1; }
[[ -f "$MANIFEST" ]] || { echo "Hash manifest is missing: $MANIFEST" >&2; exit 1; }

declare -a RELATIVE_PATHS=()
declare -a EXPECTED_HASHES=()
declare -A SEEN=()

while IFS=$'\t' read -r status expected actual relative extra; do
  [[ -n "$status$expected$actual$relative$extra" ]] || continue
  if [[ -n "${extra:-}" ]]; then
    echo "Manifest row has unexpected fields: $relative" >&2
    exit 1
  fi
  [[ "$status" == "OK" ]] || { echo "Manifest row is not verified: $relative" >&2; exit 1; }
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || { echo "Invalid expected hash: $relative" >&2; exit 1; }
  [[ "$actual" == "$expected" ]] || { echo "Manifest hash disagreement: $relative" >&2; exit 1; }
  case "$relative" in
    Content/Berkley/*) ;;
    *) echo "Refusing path outside Content/Berkley: $relative" >&2; exit 1 ;;
  esac
  if [[ "$relative" == *"/../"* || "$relative" == ../* || "$relative" == */.. ]]; then
    echo "Refusing path traversal: $relative" >&2
    exit 1
  fi
  [[ -z "${SEEN[$relative]:-}" ]] || { echo "Duplicate manifest path: $relative" >&2; exit 1; }
  SEEN[$relative]=1
  source_path="$SOURCE_ROOT/$relative"
  [[ -f "$source_path" ]] || { echo "Source object is missing: $source_path" >&2; exit 1; }
  observed="$(sha256sum "$source_path" | awk '{print $1}')"
  [[ "$observed" == "$expected" ]] || { echo "Source object hash mismatch: $relative" >&2; exit 1; }
  RELATIVE_PATHS+=("${relative#Content/}")
  EXPECTED_HASHES+=("$expected")
done <"$MANIFEST"

if (( ${#RELATIVE_PATHS[@]} != EXPECTED_COUNT )); then
  echo "Expected $EXPECTED_COUNT verified objects, found ${#RELATIVE_PATHS[@]}" >&2
  exit 1
fi

printf 'mode=%s source=%s destination=%s objects=%s\n' \
  "$([[ "$EXECUTE" == true ]] && printf execute || printf dry-run)" \
  "$SOURCE_ROOT" "$DESTINATION_CONTENT" "${#RELATIVE_PATHS[@]}"

if [[ "$EXECUTE" != true ]]; then
  printf 'preflight=pass mutation=none\n'
  exit 0
fi

mkdir -p "$DESTINATION_CONTENT"
DESTINATION_CONTENT="$(realpath "$DESTINATION_CONTENT")"
SOURCE_ROOT="$(realpath "$SOURCE_ROOT")"
if [[ "$DESTINATION_CONTENT" == "$SOURCE_ROOT"/Content || "$DESTINATION_CONTENT" == "$SOURCE_ROOT"/* ]]; then
  echo "Destination must not overlap the source checkout" >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$EVIDENCE_DIR/migration-$timestamp"
BACKUP_DIR="$RUN_DIR/rollback-content"
mkdir -p "$BACKUP_DIR"

report_rows="$(mktemp)"
trap 'rm -f "$report_rows"' EXIT

for index in "${!RELATIVE_PATHS[@]}"; do
  relative="${RELATIVE_PATHS[$index]}"
  expected="${EXPECTED_HASHES[$index]}"
  source_path="$SOURCE_ROOT/Content/$relative"
  destination_path="$DESTINATION_CONTENT/$relative"
  previous_hash=""
  action="created"
  if [[ -e "$destination_path" ]]; then
    [[ -f "$destination_path" ]] || { echo "Destination is not a regular file: $destination_path" >&2; exit 1; }
    previous_hash="$(sha256sum "$destination_path" | awk '{print $1}')"
    if [[ "$previous_hash" == "$expected" ]]; then
      action="unchanged"
    else
      action="replaced"
      backup_path="$BACKUP_DIR/$relative"
      mkdir -p "$(dirname "$backup_path")"
      cp --reflink=auto --preserve=mode,timestamps "$destination_path" "$backup_path"
    fi
  fi
  if [[ "$action" != "unchanged" ]]; then
    mkdir -p "$(dirname "$destination_path")"
    temporary="$destination_path.v2x-road-core-$timestamp.tmp"
    cp --reflink=auto --preserve=mode,timestamps "$source_path" "$temporary"
    observed="$(sha256sum "$temporary" | awk '{print $1}')"
    [[ "$observed" == "$expected" ]] || { rm -f "$temporary"; echo "Staged hash mismatch: $relative" >&2; exit 1; }
    mv -f "$temporary" "$destination_path"
  fi
  final_hash="$(sha256sum "$destination_path" | awk '{print $1}')"
  [[ "$final_hash" == "$expected" ]] || { echo "Post-copy hash mismatch: $relative" >&2; exit 1; }
  jq -nc \
    --arg path "$relative" \
    --arg expectedSha256 "$expected" \
    --arg previousSha256 "$previous_hash" \
    --arg finalSha256 "$final_hash" \
    --arg action "$action" \
    '{path:$path,expectedSha256:$expectedSha256,previousSha256:($previousSha256|if length>0 then . else null end),finalSha256:$finalSha256,action:$action}' \
    >>"$report_rows"
done

jq -s \
  --arg schema "v2x-richmond-road-core-migration/v1" \
  --arg createdAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg sourceRoot "$SOURCE_ROOT" \
  --arg sourceCommit "$(git -C "$SOURCE_ROOT" rev-parse HEAD)" \
  --arg destinationContent "$DESTINATION_CONTENT" \
  --arg manifestSha256 "$(sha256sum "$MANIFEST" | awk '{print $1}')" \
  '{schema:$schema,createdAt:$createdAt,sourceRoot:$sourceRoot,sourceCommit:$sourceCommit,destinationContent:$destinationContent,manifestSha256:$manifestSha256,objects:.}' \
  "$report_rows" >"$RUN_DIR/report.json"

(
  cd "$RUN_DIR"
  checksum_tmp="$(mktemp)"
  find . -type f ! -name evidence-sha256.txt -print0 \
    | sort -z \
    | xargs -0 sha256sum >"$checksum_tmp"
  mv "$checksum_tmp" evidence-sha256.txt
  sha256sum -c evidence-sha256.txt >/dev/null
)

printf 'migration=pass evidence=%s rollback=%s\n' "$RUN_DIR" "$BACKUP_DIR"
