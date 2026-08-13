#!/bin/bash
# Move all volume prefixes under s3://<bucket>/<src-prefix>/ to
# s3://<bucket>/<dst-prefix>/, one volume at a time, with resumability.
#
# Usage:
#   scripts/move_volumes.sh <bucket> <profile> [src-prefix] [dst-prefix]
#
# Progress is tracked in move_volumes.done (one moved volume name per line)
# in the current directory — safe to re-run; already-done volumes are skipped.
set -e

BUCKET="$1"
PROFILE="$2"
SRC="${3:-new}"
DST="${4:-pds3}"
DONE_FILE="move_volumes.done"

touch "$DONE_FILE"

aws s3api list-objects-v2 --bucket "$BUCKET" --profile "$PROFILE" \
    --prefix "${SRC}/" --delimiter / \
    --query 'CommonPrefixes[].Prefix' --output text | tr '\t' '\n' \
    > move_volumes.list

total=$(wc -l < move_volumes.list)
n=0

while IFS= read -r prefix; do
    n=$((n + 1))
    vol="${prefix#${SRC}/}"
    vol="${vol%/}"
    [ -z "$vol" ] && continue

    if grep -qxF "$vol" "$DONE_FILE"; then
        echo "[$n/$total] skip (already done): $vol"
        continue
    fi

    echo "[$n/$total] moving: $vol"

    src_count=$(aws s3 ls "s3://${BUCKET}/${SRC}/${vol}/" --profile "$PROFILE" \
        --recursive --summarize | awk '/Total Objects/{print $3}')

    aws s3 mv "s3://${BUCKET}/${SRC}/${vol}/" "s3://${BUCKET}/${DST}/${vol}/" \
        --profile "$PROFILE" --recursive --only-show-errors

    dst_count=$(aws s3 ls "s3://${BUCKET}/${DST}/${vol}/" --profile "$PROFILE" \
        --recursive --summarize | awk '/Total Objects/{print $3}')

    if [ "$src_count" = "$dst_count" ]; then
        echo "$vol" >> "$DONE_FILE"
        echo "[$n/$total] OK: $vol ($dst_count objects)"
    else
        echo "[$n/$total] MISMATCH: $vol  src=$src_count dst=$dst_count -- not marked done, will retry next run" >&2
    fi
done < move_volumes.list
