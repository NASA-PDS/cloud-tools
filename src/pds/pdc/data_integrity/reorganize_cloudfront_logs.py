#!/usr/bin/env python3
"""Reorganize CloudFront log files in S3 from flat to date-partitioned layout.

Moves objects matching the CloudFront log naming pattern:

  <distribution-id>.<YYYY>-<MM>-<DD>-<HH>.<unique-id>.gz

from the top-level of a bucket into:

  cloudfront/access/w3c/pds-unknown/year=YYYY/month=MM/day=DD/<filename>

The move is a copy followed by a delete — S3 has no native rename.  The script
is idempotent: objects already at the destination are not re-copied, and objects
that have already been deleted from the source are skipped.

Usage
-----
  pdc-reorganize-cloudfront-logs \\
    --bucket pds-logs-prod \\
    --profile my-aws-profile

Optional
--------
  --dest-prefix PREFIX   Base prefix (default: cloudfront/access/w3c/pds-unknown)
  --region REGION        AWS region (default: us-east-1)
  --source-prefix PREFIX Restrict to keys under this prefix (default: scan top level)
  --workers N            Parallel move threads (default: 16)
  --max-objects N        Stop after N objects (smoke test / dry run)
  --dry-run              Print what would happen without making changes
  --overwrite            Overwrite destination if it already exists (default: skip)
  --log-level LEVEL      DEBUG | INFO | WARNING | ERROR (default: INFO)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import logging
import re
import sys
import threading
from typing import Iterator
from typing import List
from typing import Optional
from typing import Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

_BOTO_CONFIG = Config(retries={"max_attempts": 10, "mode": "standard"})

# Matches: <dist-id>.<YYYY>-<MM>-<DD>-<HH>.<unique>.gz
# Captures year, month, day from the date portion of the key.
_LOG_RE = re.compile(
    r"^(?:.+/)?(?P<filename>[^/]+\.(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})-\d{2}\.[0-9a-f]+\.gz)$"
)

_thread_local = threading.local()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(
        description="Reorganize CloudFront log files from flat layout to date-partitioned prefixes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--bucket", required=True, help="S3 bucket containing the log files")
    p.add_argument("--profile", default=None, help="AWS profile name")
    p.add_argument("--region", default="us-west-2", help="AWS region (default: us-west-2")
    p.add_argument(
        "--dest-prefix",
        default="cloudfront/access/w3c/portal-legacy",
        help="Base destination prefix (default: cloudfront/access/w3c/portal-legacy)",
    )
    p.add_argument(
        "--source-prefix",
        default="",
        help="Restrict listing to keys under this prefix (default: scan entire bucket)",
    )
    p.add_argument("--workers", type=int, default=16, help="Parallel move threads (default: 16)")
    p.add_argument("--max-objects", type=int, default=None, help="Stop after N objects (smoke test)")
    p.add_argument("--dry-run", action="store_true", help="Print actions without making any changes")
    p.add_argument("--overwrite", action="store_true", help="Overwrite destination if it already exists (default: skip)")
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _s3_client(session: boto3.Session):
    """Return a thread-local S3 client."""
    if not hasattr(_thread_local, "s3"):
        _thread_local.s3 = session.client("s3", config=_BOTO_CONFIG)
    return _thread_local.s3


def _list_objects(session: boto3.Session, bucket: str, prefix: str) -> Iterator[dict]:
    """Yield all object metadata dicts under *prefix* via paginated ListObjectsV2."""
    s3 = session.client("s3", config=_BOTO_CONFIG)
    paginator = s3.get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix
    for page in paginator.paginate(**kwargs):
        yield from page.get("Contents", [])


def _dest_key(dest_prefix: str, year: str, month: str, day: str, filename: str) -> str:
    """Build the destination key from parsed date components."""
    return f"{dest_prefix.rstrip('/')}/year={year}/month={month}/day={day}/{filename}"


def _dest_exists(session: boto3.Session, bucket: str, key: str) -> bool:
    """Return True if the object already exists at *key*."""
    s3 = _s3_client(session)
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def _move_object(
    session: boto3.Session,
    bucket: str,
    src_key: str,
    dst_key: str,
    dry_run: bool,
) -> None:
    """Copy *src_key* to *dst_key* then delete the source."""
    if dry_run:
        log.info("[DRY RUN] s3://%s/%s  ->  s3://%s/%s", bucket, src_key, bucket, dst_key)
        return

    s3 = _s3_client(session)

    # Copy
    s3.copy_object(
        Bucket=bucket,
        Key=dst_key,
        CopySource={"Bucket": bucket, "Key": src_key},
        MetadataDirective="COPY",
        TaggingDirective="COPY",
    )
    log.debug("Copied  s3://%s/%s  ->  s3://%s/%s", bucket, src_key, bucket, dst_key)

    # Delete source only after successful copy
    s3.delete_object(Bucket=bucket, Key=src_key)
    log.debug("Deleted s3://%s/%s", bucket, src_key)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )
    if level != "DEBUG":
        for name in ("boto3", "botocore", "urllib3", "s3transfer"):
            logging.getLogger(name).setLevel(logging.WARNING)


def main() -> int:
    """Reorganize CloudFront log files from flat layout to date-partitioned prefixes."""
    args = parse_args()
    _configure_logging(args.log_level)

    session = boto3.Session(
        profile_name=args.profile,
        region_name=args.region,
    )

    log.info("Bucket:       s3://%s", args.bucket)
    log.info("Source prefix: '%s'  (empty = entire bucket)", args.source_prefix)
    log.info("Dest prefix:  %s", args.dest_prefix)
    log.info("Workers:      %d", args.workers)
    if args.dry_run:
        log.info("DRY RUN — no changes will be made.")
    if args.overwrite:
        log.info("OVERWRITE — existing destination objects will be replaced.")

    # Collect candidate objects
    candidates: List[Tuple[str, str, str, str, str]] = []  # (src_key, year, month, day, filename)
    skipped_no_match = 0

    log.info("Listing objects...")
    for obj in _list_objects(session, args.bucket, args.source_prefix):
        key = obj["Key"]
        m = _LOG_RE.match(key)
        if not m:
            skipped_no_match += 1
            log.debug("Skipping (no match): %s", key)
            continue

        candidates.append((key, m.group("year"), m.group("month"), m.group("day"), m.group("filename")))
        if args.max_objects and len(candidates) >= args.max_objects:
            log.info("--max-objects %d reached, stopping list.", args.max_objects)
            break

    log.info("Found %d matching objects (%d skipped — no pattern match).", len(candidates), skipped_no_match)

    if not candidates:
        log.info("Nothing to do.")
        return 0

    # Move in parallel
    moved = 0
    skipped_exists = 0
    errors = 0
    lock = threading.Lock()

    def _process(item: Tuple[str, str, str, str, str]) -> None:
        nonlocal moved, skipped_exists, errors
        src_key, year, month, day, filename = item
        dst_key = _dest_key(args.dest_prefix, year, month, day, filename)

        try:
            if not args.dry_run and not args.overwrite and _dest_exists(session, args.bucket, dst_key):
                log.debug("Destination already exists, skipping: %s", dst_key)
                with lock:
                    skipped_exists += 1
                return

            _move_object(session, args.bucket, src_key, dst_key, args.dry_run)

            with lock:
                moved += 1
                if moved in {1, 10, 100} or moved % 500 == 0:
                    log.info("Progress: %d moved, %d skipped (dest exists), %d errors", moved, skipped_exists, errors)

        except ClientError as e:
            log.error("Failed to move s3://%s/%s: %s", args.bucket, src_key, e)
            with lock:
                errors += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(_process, candidates))

    log.info("=== Summary ===")
    log.info("Moved:   %d", moved)
    log.info("Skipped (destination already exists): %d", skipped_exists)
    log.info("Errors:  %d", errors)

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
