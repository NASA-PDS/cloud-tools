#!/bin/bash
# Download CloudFront access logs from a source S3 bucket and upload them to a
# destination S3 bucket, using different IAM profiles for each side.
#
# Useful when the two buckets require different IAM roles, making direct
# server-side copy impossible.
#
# Usage:
#   scripts/sync_cloudfront_logs.sh <year> <month> [options]
#
# Options:
#   --src-bucket   <bucket>   Source bucket (default: pds-logs-prod)
#   --dst-bucket   <bucket>   Destination bucket (default: pds-logs-dev)
#   --src-profile  <profile>  AWS profile for source (default: prod-en-platform-engineer)
#   --dst-profile  <profile>  AWS profile for destination (default: dev-power)
#   --prefix       <prefix>   Key prefix up to year= segment
#                             (default: pdc-cds-infra/cloudfront/access/json/pds-main)
#   --scratch-dir  <dir>      Local staging directory (default: /tmp/cloudfront-sync-<year>-<month>)
#   --keep-scratch            Keep the local scratch directory after upload (default: delete)
#
# Examples:
#   # Sync August 2026 with defaults
#   scripts/sync_cloudfront_logs.sh 2026 08
#
#   # Sync July 2026, keep scratch
#   scripts/sync_cloudfront_logs.sh 2026 07 --keep-scratch
#
#   # Override buckets and profiles
#   scripts/sync_cloudfront_logs.sh 2026 06 \
#     --src-bucket pds-logs-prod --src-profile <src-profile> \
#     --dst-bucket pds-logs-dev  --dst-profile <dst-profile>

set -euo pipefail

# ── defaults ──────────────────────────────────────────────────────────────────
SRC_BUCKET="pds-logs-prod"
DST_BUCKET="pds-logs-dev"
SRC_PROFILE="prod-en-platform-engineer"
DST_PROFILE="dev-power"
KEY_PREFIX="pdc-cds-infra/cloudfront/access/json/pds-main"
SCRATCH_DIR=""
KEEP_SCRATCH=false

# ── parse positional args ─────────────────────────────────────────────────────
if [ $# -lt 2 ]; then
    echo "Usage: $0 <year> <month> [options]" >&2
    exit 1
fi
YEAR="$1"; shift
MONTH="$1"; shift

# ── parse flags ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --src-bucket)   SRC_BUCKET="$2";   shift 2 ;;
        --dst-bucket)   DST_BUCKET="$2";   shift 2 ;;
        --src-profile)  SRC_PROFILE="$2";  shift 2 ;;
        --dst-profile)  DST_PROFILE="$2";  shift 2 ;;
        --prefix)       KEY_PREFIX="$2";   shift 2 ;;
        --scratch-dir)  SCRATCH_DIR="$2";  shift 2 ;;
        --keep-scratch) KEEP_SCRATCH=true; shift   ;;
        *) echo "Unknown option: $1" >&2; exit 1   ;;
    esac
done

REMOTE_PREFIX="${KEY_PREFIX}/year=${YEAR}/month=${MONTH}/"
if [ -z "$SCRATCH_DIR" ]; then
    SCRATCH_DIR="/tmp/cloudfront-sync-${YEAR}-${MONTH}"
fi

echo "==> Syncing s3://${SRC_BUCKET}/${REMOTE_PREFIX}"
echo "    -> s3://${DST_BUCKET}/${REMOTE_PREFIX}"
echo "    scratch: ${SCRATCH_DIR}"

mkdir -p "$SCRATCH_DIR"

# ── download ──────────────────────────────────────────────────────────────────
echo ""
echo "==> [1/3] Downloading from source..."
aws s3 sync \
    "s3://${SRC_BUCKET}/${REMOTE_PREFIX}" \
    "${SCRATCH_DIR}/" \
    --profile "$SRC_PROFILE" \
    --only-show-errors

downloaded=$(find "$SCRATCH_DIR" -type f | wc -l | tr -d ' ')
echo "    Downloaded ${downloaded} files."

# ── upload ────────────────────────────────────────────────────────────────────
echo ""
echo "==> [2/3] Uploading to destination..."
aws s3 sync \
    "${SCRATCH_DIR}/" \
    "s3://${DST_BUCKET}/${REMOTE_PREFIX}" \
    --profile "$DST_PROFILE" \
    --only-show-errors

# ── verify ────────────────────────────────────────────────────────────────────
echo ""
echo "==> [3/3] Verifying..."

TMP_LOCAL=$(mktemp)
TMP_REMOTE=$(mktemp)
trap 'rm -f "$TMP_LOCAL" "$TMP_REMOTE"' EXIT

# Keys present locally (relative to scratch dir)
find "$SCRATCH_DIR" -type f \
    | sed "s|${SCRATCH_DIR}/||" \
    | sort > "$TMP_LOCAL"

# Keys present in destination bucket under the same prefix
aws s3api list-objects-v2 \
    --bucket "$DST_BUCKET" \
    --profile "$DST_PROFILE" \
    --prefix "$REMOTE_PREFIX" \
    --query 'Contents[].Key' \
    --output text \
    | tr '\t' '\n' \
    | sed "s|${REMOTE_PREFIX}||" \
    | sort > "$TMP_REMOTE"

missing=$(comm -23 "$TMP_LOCAL" "$TMP_REMOTE" | wc -l | tr -d ' ')

if [ "$missing" -gt 0 ]; then
    echo "ERROR: ${missing} local file(s) not found in destination:" >&2
    comm -23 "$TMP_LOCAL" "$TMP_REMOTE" >&2
    exit 1
fi

echo "    Verified: all ${downloaded} files present in destination."

# ── cleanup ───────────────────────────────────────────────────────────────────
if [ "$KEEP_SCRATCH" = false ]; then
    echo ""
    echo "==> Cleaning up scratch directory..."
    rm -rf "$SCRATCH_DIR"
fi

echo ""
echo "Done. year=${YEAR}/month=${MONTH} synced successfully."
