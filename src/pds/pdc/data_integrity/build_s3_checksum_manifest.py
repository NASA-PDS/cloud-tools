#!/usr/bin/env python3
"""Build a per-object checksum manifest for an S3 bucket.

Checksum resolution order (first match wins):
  1. Native S3 checksum from GetObjectAttributes (CRC64NVME preferred)
  2. MD5 from x-amz-meta-s3cmd-attrs user metadata (HeadObject fallback)
  3. No checksum recorded — object will appear as unverifiable in comparison

Output CSV columns:
  bucket, key, size, checksum_algorithm, checksum_type, checksum_value, etag

A progress DB is always maintained at <output>.db alongside the CSV.  On the
next run the DB is loaded automatically — already-processed keys are skipped
without re-reading the CSV.  If the DB does not exist yet, pass --resume-from
<manifest.csv> to seed it from an existing CSV on the first resume.

Usage:
  pdc-build-checksum-manifest --bucket my-bucket --output manifest.csv

Optional:
  --prefix PDS4/
  --profile my-aws-profile
  --region us-west-2
  --resume-from manifest.csv   # seed the DB from a CSV (only needed when no .db exists yet)
  --keys-from weak_checksum_rows.csv  # re-fetch only the keys listed in this CSV
  --max-objects 1000           # stop after N objects (dry run / smoke test)
  --workers 32                 # parallel threads for checksum fetching (default: 32)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import os
import sqlite3
import sys
import threading
import time
from typing import Dict
from typing import Iterable
from typing import List
from typing import Optional
from typing import Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


CHECKSUM_FIELDS = [
    ("CRC64NVME", "ChecksumCRC64NVME"),
    ("CRC32C", "ChecksumCRC32C"),
    ("CRC32", "ChecksumCRC32"),
    ("SHA256", "ChecksumSHA256"),
    ("SHA1", "ChecksumSHA1"),
]

CSV_HEADERS = [
    "bucket",
    "key",
    "size",
    "checksum_algorithm",
    "checksum_type",
    "checksum_value",
    "etag",
]

_thread_local = threading.local()


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument("--output", required=True, help="Output CSV path")
    parser.add_argument("--prefix", default="", help="Optional prefix filter")
    parser.add_argument("--profile", default=None, help="AWS profile")
    parser.add_argument("--region", default=None, help="AWS region")
    parser.add_argument(
        "--resume-from",
        default=None,
        help="Existing manifest CSV to skip already-processed keys",
    )
    parser.add_argument(
        "--max-objects",
        type=int,
        default=None,
        help="Stop after processing this many objects (useful for dry runs)",
    )
    parser.add_argument(
        "--keys-from",
        default=None,
        help="CSV file containing a 'key' column; process only those keys instead of listing the bucket",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=32,
        help="Number of parallel threads for checksum fetching (default: 32)",
    )
    return parser.parse_args()


def build_session(profile: Optional[str], region: Optional[str]) -> boto3.Session:
    """Create a boto3 Session with optional profile and region."""
    if profile and region:
        return boto3.Session(profile_name=profile, region_name=region)
    if profile:
        return boto3.Session(profile_name=profile)
    if region:
        return boto3.Session(region_name=region)
    return boto3.Session()


def get_thread_s3_client(session: boto3.Session):
    """Return a thread-local S3 client, creating one on first access per thread."""
    if not hasattr(_thread_local, "s3_client"):
        _thread_local.s3_client = session.client(
            "s3",
            config=Config(retries={"max_attempts": 10, "mode": "standard"}),
        )
    return _thread_local.s3_client


def open_or_create_db(db_path: str) -> Tuple[sqlite3.Connection, int]:
    """Open the persistent progress DB, creating it if absent.

    Returns (connection, existing_key_count).
    """
    already_existed = os.path.exists(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE IF NOT EXISTS processed_keys (key TEXT NOT NULL PRIMARY KEY)")
    conn.commit()
    count = 0
    if already_existed:
        count = conn.execute("SELECT COUNT(*) FROM processed_keys").fetchone()[0]
    return conn, count


def seed_db_from_csv(conn: sqlite3.Connection, csv_path: str) -> int:
    """Insert keys from an existing manifest CSV into the progress DB.

    Skips keys already present.  Returns the number of newly inserted rows.
    Commits each batch so progress is preserved on KeyboardInterrupt.
    """
    batch: List[Tuple[str, ...]] = []
    batch_size = 500_000
    rows_read = 0
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            batch.append((row["key"],))
            rows_read += 1
            if len(batch) >= batch_size:
                conn.executemany("INSERT OR IGNORE INTO processed_keys VALUES (?)", batch)
                conn.commit()
                batch.clear()
                print(f"  Seeded {rows_read:,} rows from CSV...", file=sys.stderr)
    if batch:
        conn.executemany("INSERT OR IGNORE INTO processed_keys VALUES (?)", batch)
        conn.commit()
    return rows_read


def key_is_processed(conn: sqlite3.Connection, key: str) -> bool:
    """Return True if key exists in the SQLite processed-keys table."""
    return conn.execute("SELECT 1 FROM processed_keys WHERE key = ?", (key,)).fetchone() is not None


def mark_key_processed(conn: sqlite3.Connection, key: str, count: int) -> None:
    """Insert a key into the progress DB; commit every 1000 rows."""
    conn.execute("INSERT OR IGNORE INTO processed_keys VALUES (?)", (key,))
    if count % 1_000 == 0:
        conn.commit()


def choose_checksum(resp: Dict) -> Tuple[str, str, str]:
    """Prefer CRC64NVME if present, otherwise take first available checksum.

    Returns: (algorithm, checksum_type, checksum_value)
    """
    checksum = resp.get("Checksum", {}) or {}
    checksum_type = checksum.get("ChecksumType", "")

    for algo_name, field_name in CHECKSUM_FIELDS:
        value = checksum.get(field_name)
        if value:
            return algo_name, checksum_type, value

    return "", checksum_type, ""


def iter_keys_from_csv(csv_path: str) -> Iterable[Dict]:
    """Yield minimal object dicts (key, size) from a manifest-style CSV.

    The CSV must have a 'key' column; 'size' is used if present.
    """
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row.get("key") or "").strip()
            if not key:
                continue
            size = (row.get("size") or "").strip()
            yield {"Key": key, "Size": size}


def list_objects(s3_client, bucket: str, prefix: str) -> Iterable[Dict]:
    """Paginate over all objects in a bucket matching the given prefix."""
    paginator = s3_client.get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix

    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            yield obj


def get_object_attrs_with_retry(s3_client, bucket: str, key: str, retries: int = 5) -> Dict:
    """Call GetObjectAttributes with exponential-backoff retry on throttle errors."""
    delay = 1.0
    for attempt in range(retries):
        try:
            return s3_client.get_object_attributes(
                Bucket=bucket,
                Key=key,
                ObjectAttributes=["Checksum", "ObjectSize", "ETag"],
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in {"SlowDown", "Throttling", "RequestTimeout", "InternalError"} and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError(f"Exhausted {retries} retries for GetObjectAttributes on {key}")


def head_object_with_retry(s3_client, bucket: str, key: str, retries: int = 5) -> Dict:
    """Call HeadObject with exponential-backoff retry on throttle errors."""
    delay = 1.0
    for attempt in range(retries):
        try:
            return s3_client.head_object(Bucket=bucket, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in {"SlowDown", "Throttling", "RequestTimeout", "InternalError"} and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError(f"Exhausted {retries} retries for HeadObject on {key}")


def parse_s3cmd_md5(attrs_value: str) -> str:
    """Extract md5 from an x-amz-meta-s3cmd-attrs metadata string.

    Example value:
        atime:1733437044/ctime:1733164973/gid:4000/gname:pds/md5:c3de3759.../mode:33188/...

    Returns the md5 hex string, or empty string if not found.
    """
    for part in attrs_value.split("/"):
        if part.startswith("md5:"):
            return part[4:].strip()
    return ""


def process_object(session: boto3.Session, bucket: str, key: str, obj_size) -> List:
    """Fetch checksum info for one S3 object using a thread-local client.

    Always returns a CSV row list — ClientErrors are captured as an error value.
    """
    s3 = get_thread_s3_client(session)
    try:
        attrs = get_object_attrs_with_retry(s3, bucket, key)
        algo, checksum_type, checksum_value = choose_checksum(attrs)
        size = attrs.get("ObjectSize", obj_size)
        etag = (attrs.get("ETag") or "").strip('"')

        if not checksum_value:
            # No native S3 checksum — try x-amz-meta-s3cmd-attrs metadata.
            head = head_object_with_retry(s3, bucket, key)
            s3cmd_attrs = (head.get("Metadata") or {}).get("s3cmd-attrs", "")
            if s3cmd_attrs:
                md5 = parse_s3cmd_md5(s3cmd_attrs)
                if md5:
                    algo = "S3CMD-MD5"
                    checksum_type = "MD5"
                    checksum_value = md5

    except ClientError as e:
        # Record a row with blanks so the comparison script can flag it.
        err_code = e.response.get("Error", {}).get("Code", "UNKNOWN")
        print(f"  WARNING: ClientError for key={key}: {err_code}", file=sys.stderr)
        size = obj_size
        etag = ""
        algo = ""
        checksum_type = ""
        checksum_value = f"ERROR:{err_code}"

    return [bucket, key, size, algo, checksum_type, checksum_value, etag]


def main() -> int:
    """Build a checksum manifest CSV for all objects in an S3 bucket."""
    args = parse_args()

    profile_str = args.profile or "default"
    region_str = args.region or "default"
    print(
        f"Building session (profile={profile_str}, region={region_str}, workers={args.workers})...",
        file=sys.stderr,
    )
    session = build_session(args.profile, args.region)

    # Main-thread client used only for list_objects pagination.
    list_client = session.client(
        "s3",
        config=Config(retries={"max_attempts": 10, "mode": "standard"}),
    )

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    db_path = args.output + ".db"
    resume_conn, existing_count = open_or_create_db(db_path)

    write_header = True
    mode = "w"

    if existing_count > 0:
        print(
            f"WARNING: Progress DB found at {db_path} with {existing_count:,} already-processed keys. "
            "Continuing from DB — pass a fresh --output path to start over.",
            file=sys.stderr,
        )
        mode = "a"
        write_header = False

    prefix_str = args.prefix or "(none)"

    # At most workers*4 futures in-flight to bound memory for very large buckets.
    max_pending = args.workers * 4

    interrupted = False
    try:
        if existing_count == 0 and args.resume_from:
            print(f"Seeding progress DB from {args.resume_from}...", file=sys.stderr)
            seeded = seed_db_from_csv(resume_conn, args.resume_from)
            print(f"Seeded {seeded:,} rows into {db_path}. Will skip already-processed keys.", file=sys.stderr)
            mode = "a"
            write_header = False

        if args.keys_from:
            print(f"Reading keys from {args.keys_from} (skipping bucket listing)...", file=sys.stderr)
            object_source = iter_keys_from_csv(args.keys_from)
        else:
            print(f"Listing objects in s3://{args.bucket} prefix={prefix_str} ...", file=sys.stderr)
            object_source = list_objects(list_client, args.bucket, args.prefix)

        with open(args.output, mode, newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(CSV_HEADERS)

            count = 0
            skipped = 0
            submitted = 0
            skipping_done = False

            def log_progress() -> None:
                nonlocal skipping_done
                # One-time notice when we transition from skipping to processing new objects.
                if not skipping_done and skipped > 0 and count > 0:
                    print(
                        f"Finished skipping {skipped} already-processed keys. Now processing new objects...",
                        file=sys.stderr,
                    )
                    skipping_done = True
                if count in {1, 10, 100} or count % 1000 == 0:
                    scanned = count + skipped
                    scanned_str = f" ({scanned} total scanned)" if skipped else ""
                    print(f"Processed {count} new objects{scanned_str}...", file=sys.stderr)

            def write_row(fut: concurrent.futures.Future) -> None:
                """Write a completed future's result to CSV and record key in DB."""
                nonlocal count
                row = fut.result()
                writer.writerow(row)
                count += 1
                mark_key_processed(resume_conn, row[1], count)  # row[1] == key
                log_progress()
                # Flush CSV periodically so the output stays recoverable on interruption.
                if count % 10_000 == 0:
                    f.flush()

            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
                pending: Dict[concurrent.futures.Future, None] = {}

                try:
                    for obj in object_source:
                        key = obj["Key"]
                        if resume_conn and key_is_processed(resume_conn, key):
                            skipped += 1
                            continue

                        if args.max_objects and submitted >= args.max_objects:
                            break

                        # Block until the queue drains below the cap.
                        while len(pending) >= max_pending:
                            done, _ = concurrent.futures.wait(
                                pending.keys(), return_when=concurrent.futures.FIRST_COMPLETED
                            )
                            for fut in done:
                                write_row(fut)
                                del pending[fut]

                        # Opportunistically drain any already-finished futures.
                        for fut in [f for f in pending if f.done()]:
                            write_row(fut)
                            del pending[fut]

                        pending[
                            executor.submit(process_object, session, args.bucket, key, obj.get("Size", ""))
                        ] = None
                        submitted += 1

                        if submitted == 1:
                            print("First object submitted, fetching checksums...", file=sys.stderr)

                    # Drain all remaining in-flight futures.
                    for fut in concurrent.futures.as_completed(list(pending.keys())):
                        write_row(fut)

                except KeyboardInterrupt:
                    interrupted = True
                    print(
                        "\nInterrupted. Cancelling queued work and draining in-flight requests...",
                        file=sys.stderr,
                    )
                    # Cancel futures that haven't started yet.
                    for fut in list(pending.keys()):
                        fut.cancel()
                    # Drain futures that are already running so the output stays valid.
                    for fut in concurrent.futures.as_completed(list(pending.keys())):
                        if not fut.cancelled():
                            write_row(fut)

            f.flush()

        if interrupted:
            print(
                f"Interrupted after {count} new objects (output preserved at {args.output}). "
                f"Progress DB saved to {db_path} — re-run with the same --output to continue.",
                file=sys.stderr,
            )
        else:
            skipped_str = f", skipped {skipped} already-processed" if skipped else ""
            print(
                f"Done. Processed {count} new objects{skipped_str} ({count + skipped} total scanned). "
                f"Manifest written to {args.output}",
                file=sys.stderr,
            )
    except KeyboardInterrupt:
        interrupted = True
        print(
            f"\nInterrupted during seeding. Progress DB saved to {db_path} — "
            "re-run with the same arguments to continue seeding.",
            file=sys.stderr,
        )
    finally:
        resume_conn.commit()
        resume_conn.close()

    return 1 if interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
