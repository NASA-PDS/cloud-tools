#!/usr/bin/env python3
"""Copy S3 objects from a manifest CSV to a destination bucket.

Handles Glacier restoration and multipart copy for large files.  The script is
fully resumable: all per-object state is tracked in a SQLite database so a
crashed or interrupted run can be re-launched with the same arguments and it
will pick up where it left off.

Two-phase operation
-------------------
Phase 1 — Restore
    Submit RestoreObject requests for every GLACIER / DEEP_ARCHIVE object.
    Objects already in a non-Glacier storage class are marked ready immediately.
    Glacier Instant Retrieval (GLACIER_IR) objects are also immediately copyable
    and skip the restore phase.

Phase 2 — Poll + Copy
    Periodically poll the restore status of each object.  Once restored (or
    never needing restore), the object is dispatched to a thread-pool worker that
    performs either a single-operation CopyObject (≤ --multipart-threshold) or a
    multipart UploadPartCopy sequence (> --multipart-threshold).  In-progress
    multipart uploads are aborted on any failure so the destination bucket is
    never left with dangling incomplete uploads.

Usage
-----
  pdc-copy-objects \\
    --manifest missing_in_new.csv \\
    --dest-bucket my-dest-bucket \\
    --dest-region us-west-2 \\
    --source-profile jpl_aws_atm

Optional
--------
  --dest-profile PROFILE       AWS profile for the destination (default: same as source)
  --source-region REGION        Source bucket region (default: us-east-1)
  --dest-prefix PREFIX          Prepend PREFIX to every destination key (default: none)
  --restore-tier TIER           Expedited | Standard | Bulk  (default: Standard)
  --restore-days N              Days restored copy stays available (default: 3)
  --storage-class CLASS         Destination storage class (default: STANDARD)
  --multipart-threshold BYTES   Use multipart above this size (default: 524288000 = 500 MiB)
  --part-size BYTES             Part size for multipart copy (default: 268435456 = 256 MiB)
  --workers N                   Parallel copy threads (default: 16)
  --poll-interval SECONDS       Seconds between restore-status sweeps (default: 300)
  --state-db PATH               SQLite state file for resume (default: copy_state.db)
  --source-bucket BUCKET        Override the bucket column in every manifest row
                                (required when combining missing_in_new.csv,
                                unverifiable.csv, and weak_checksum_rows.csv
                                since weak rows carry the destination bucket name)
  --restore-only                Submit restore requests and exit; run again with
                                --copy-only tomorrow to execute the copy phase
  --copy-only                   Skip restore submission and go straight to polling
                                + copy; use after --restore-only has completed
  --max-objects N               Stop after N objects (smoke test)
  --dry-run                     Print what would happen without making changes
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import logging
import sqlite3
import sys
import threading
import time
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple

import resource

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)


_GLACIER_RESTORE_CLASSES = {"GLACIER", "DEEP_ARCHIVE"}
_GLACIER_ALL_CLASSES = {"GLACIER", "DEEP_ARCHIVE", "GLACIER_IR"}
_DEFAULT_MULTIPART_THRESHOLD = 500 * 1024 * 1024   # 500 MiB
_DEFAULT_PART_SIZE = 256 * 1024 * 1024              # 256 MiB
_BOTO_CONFIG = Config(retries={"max_attempts": 10, "mode": "standard"})

_thread_local = threading.local()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(
        description="Copy S3 objects from a manifest CSV to a destination bucket.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--manifest", required=True, nargs="+", help="Input CSV(s) (missing_in_new.csv format; accepts multiple files)")
    p.add_argument("--dest-bucket", required=True, help="Destination S3 bucket name")
    p.add_argument("--dest-region", required=True, help="Destination bucket AWS region")
    p.add_argument("--source-profile", default=None, help="AWS profile for the source bucket")
    p.add_argument("--dest-profile", default=None, help="AWS profile for the destination bucket")
    p.add_argument("--source-region", default="us-east-1", help="Source bucket region (default: us-east-1)")
    p.add_argument("--dest-prefix", default="", help="Prefix to prepend to every destination key")
    p.add_argument(
        "--restore-tier",
        default="Standard",
        choices=["Expedited", "Standard", "Bulk"],
        help="Glacier restore tier (default: Standard)",
    )
    p.add_argument("--restore-days", type=int, default=3, help="Days restored copy stays available (default: 3)")
    p.add_argument("--storage-class", default="STANDARD", help="Destination storage class (default: STANDARD)")
    p.add_argument(
        "--multipart-threshold",
        type=int,
        default=_DEFAULT_MULTIPART_THRESHOLD,
        help="Use multipart copy above this byte size (default: 500 MiB)",
    )
    p.add_argument(
        "--part-size",
        type=int,
        default=_DEFAULT_PART_SIZE,
        help="Part size in bytes for multipart copy (default: 256 MiB)",
    )
    p.add_argument("--workers", type=int, default=16, help="Parallel copy threads (default: 16)")
    p.add_argument(
        "--poll-interval",
        type=int,
        default=300,
        help="Seconds between restore-status sweeps (default: 300)",
    )
    p.add_argument("--state-db", default="copy_state.db", help="SQLite state file (default: copy_state.db)")
    p.add_argument(
        "--source-bucket",
        default=None,
        help="Override the bucket column in every manifest row (use when combining files with different bucket names)",
    )
    p.add_argument("--max-objects", type=int, default=None, help="Stop after N objects (smoke test)")
    p.add_argument("--dry-run", action="store_true", help="Print actions without executing them")
    p.add_argument(
        "--retry-errors",
        action="store_true",
        help="Reset objects in 'error' state back to 'pending' so they are retried this run",
    )
    p.add_argument(
        "--restore-only",
        action="store_true",
        help="Submit Glacier restore requests and exit without copying (run again tomorrow to copy)",
    )
    p.add_argument(
        "--copy-only",
        action="store_true",
        help="Skip restore submission and go straight to polling + copy (use after --restore-only completes)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    p.add_argument("--log-file", default=None, help="Write log output to this file in addition to stderr")
    p.add_argument(
        "--status-interval",
        type=int,
        default=60,
        help="Seconds between periodic status log lines (default: 60; 0 to disable)",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Session / client helpers
# ---------------------------------------------------------------------------

def _build_session(profile: Optional[str], region: Optional[str]) -> boto3.Session:
    kwargs: Dict = {}
    if profile:
        kwargs["profile_name"] = profile
    if region:
        kwargs["region_name"] = region
    return boto3.Session(**kwargs)


def _get_thread_client(session: boto3.Session, attr: str):
    """Return a thread-local S3 client for the given session, keyed by attr."""
    if not hasattr(_thread_local, attr):
        setattr(_thread_local, attr, session.client("s3", config=_BOTO_CONFIG))
    return getattr(_thread_local, attr)


def _src_client(src_session: boto3.Session):
    return _get_thread_client(src_session, "src_s3")


def _dst_client(dst_session: boto3.Session):
    return _get_thread_client(dst_session, "dst_s3")


# ---------------------------------------------------------------------------
# SQLite state DB
# ---------------------------------------------------------------------------

def init_db(path: str) -> sqlite3.Connection:
    """Create or open the resume state database."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS objects (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket  TEXT    NOT NULL,
            key     TEXT    NOT NULL,
            size    INTEGER NOT NULL DEFAULT 0,
            state   TEXT    NOT NULL DEFAULT 'pending',
            error   TEXT,
            UNIQUE(bucket, key)
        )
        """
    )
    conn.commit()
    return conn


def _set_state(conn: sqlite3.Connection, lock: threading.Lock, bucket: str, key: str, state: str, error: str = "") -> None:
    with lock:
        conn.execute(
            "UPDATE objects SET state=?, error=? WHERE bucket=? AND key=?",
            (state, error or None, bucket, key),
        )
        conn.commit()


def _state_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    return {row[0]: row[1] for row in conn.execute("SELECT state, COUNT(*) FROM objects GROUP BY state")}


def _start_status_ticker(conn: sqlite3.Connection, interval: int, stop_evt: threading.Event) -> threading.Thread:
    """Start a daemon thread that logs state counts every *interval* seconds."""
    prev_done: List[int] = [0]
    prev_time: List[float] = [time.monotonic()]

    def _tick():
        while not stop_evt.wait(interval):
            c = _state_counts(conn)
            now = time.monotonic()
            done = c.get("done", 0)
            elapsed = now - prev_time[0]
            rate = (done - prev_done[0]) / elapsed if elapsed > 0 else 0.0
            prev_done[0] = done
            prev_time[0] = now
            log.info(
                "[status] done=%d  copying=%d  ready=%d  restoring=%d  pending=%d  error=%d  rate=%.1f obj/s",
                done,
                c.get("copying", 0),
                c.get("ready", 0),
                c.get("restoring", 0),
                c.get("pending", 0),
                c.get("error", 0),
                rate,
            )

    t = threading.Thread(target=_tick, name="status-ticker", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------

def load_manifest(
    conn: sqlite3.Connection,
    paths: List[str],
    max_objects: Optional[int],
    source_bucket_override: Optional[str] = None,
) -> int:
    """Insert manifest rows from one or more CSVs into the DB.

    Extra columns (e.g. the ``reason`` column in unverifiable.csv) are ignored.
    If source_bucket_override is set it replaces the bucket column for every row,
    which is necessary when combining files that carry different bucket names
    (e.g. weak_checksum_rows.csv has the destination bucket, not the source).
    """
    inserted = 0
    for path in paths:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            batch: List[Tuple] = []
            for row in reader:
                if max_objects and inserted + len(batch) >= max_objects:
                    break
                bucket = source_bucket_override or row["bucket"]
                batch.append((bucket, row["key"], int(row.get("size") or 0)))
                if len(batch) >= 10_000:
                    conn.executemany("INSERT OR IGNORE INTO objects(bucket, key, size) VALUES (?,?,?)", batch)
                    conn.commit()
                    inserted += len(batch)
                    batch.clear()
            if batch:
                conn.executemany("INSERT OR IGNORE INTO objects(bucket, key, size) VALUES (?,?,?)", batch)
                conn.commit()
                inserted += len(batch)
    return inserted


# ---------------------------------------------------------------------------
# Glacier restore helpers
# ---------------------------------------------------------------------------

def _submit_restore(
    src_session: boto3.Session,
    bucket: str,
    key: str,
    tier: str,
    days: int,
    dry_run: bool,
) -> str:
    """Submit a RestoreObject request.  Returns new state: 'restoring' or 'ready'."""
    if dry_run:
        log.debug("[DRY RUN] RestoreObject: s3://%s/%s", bucket, key)
        return "restoring"
    s3 = _src_client(src_session)
    # Check current storage class first
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        raise RuntimeError(f"HeadObject failed: {e.response['Error']['Code']}") from e

    sc = head.get("StorageClass", "STANDARD")
    if sc not in _GLACIER_RESTORE_CLASSES:
        # STANDARD, STANDARD_IA, GLACIER_IR — copyable without restore
        return "ready"

    # Check if already restored
    restore_hdr = head.get("Restore", "")
    if restore_hdr and 'ongoing-request="false"' in restore_hdr:
        return "ready"

    # Submit restore (idempotent)
    try:
        s3.restore_object(
            Bucket=bucket,
            Key=key,
            RestoreRequest={"Days": days, "GlacierJobParameters": {"Tier": tier}},
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "RestoreAlreadyInProgress":
            pass  # already in flight — fine
        else:
            raise
    return "restoring"


def _check_restore_status(src_session: boto3.Session, bucket: str, key: str) -> str:
    """Return 'ready' if restored/copyable, 'restoring' if still pending."""
    s3 = _src_client(src_session)
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        raise RuntimeError(f"HeadObject failed: {e.response['Error']['Code']}") from e

    sc = head.get("StorageClass", "STANDARD")
    if sc not in _GLACIER_RESTORE_CLASSES:
        return "ready"
    restore_hdr = head.get("Restore", "")
    if restore_hdr and 'ongoing-request="false"' in restore_hdr:
        return "ready"
    return "restoring"


# ---------------------------------------------------------------------------
# Copy helpers
# ---------------------------------------------------------------------------

def _copy_simple(
    dst_session: boto3.Session,
    src_bucket: str,
    src_key: str,
    dst_bucket: str,
    dst_key: str,
    storage_class: str,
) -> None:
    s3 = _dst_client(dst_session)
    s3.copy_object(
        Bucket=dst_bucket,
        Key=dst_key,
        CopySource={"Bucket": src_bucket, "Key": src_key},
        StorageClass=storage_class,
        MetadataDirective="COPY",
        TaggingDirective="COPY",
    )


def _copy_multipart(
    src_session: boto3.Session,
    dst_session: boto3.Session,
    src_bucket: str,
    src_key: str,
    dst_bucket: str,
    dst_key: str,
    size: int,
    part_size: int,
    storage_class: str,
) -> None:
    src_s3 = _src_client(src_session)
    dst_s3 = _dst_client(dst_session)

    # Fetch source metadata and tags so they survive the multipart upload.
    # create_multipart_upload creates a new object shell — without passing these
    # explicitly, user-defined metadata, ContentType, and tags are silently lost.
    head = src_s3.head_object(Bucket=src_bucket, Key=src_key)
    user_metadata = head.get("Metadata", {})
    content_type = head.get("ContentType", "binary/octet-stream")

    try:
        tag_resp = src_s3.get_object_tagging(Bucket=src_bucket, Key=src_key)
        tag_set = tag_resp.get("TagSet", [])
    except ClientError:
        tag_set = []

    resp = dst_s3.create_multipart_upload(
        Bucket=dst_bucket,
        Key=dst_key,
        StorageClass=storage_class,
        Metadata=user_metadata,
        ContentType=content_type,
    )
    upload_id = resp["UploadId"]
    parts: List[Dict] = []
    try:
        offset = 0
        part_num = 1
        while offset < size:
            end = min(offset + part_size - 1, size - 1)
            part_resp = dst_s3.upload_part_copy(
                Bucket=dst_bucket,
                Key=dst_key,
                UploadId=upload_id,
                PartNumber=part_num,
                CopySource={"Bucket": src_bucket, "Key": src_key},
                CopySourceRange=f"bytes={offset}-{end}",
            )
            parts.append({"PartNumber": part_num, "ETag": part_resp["CopyPartResult"]["ETag"]})
            offset = end + 1
            part_num += 1
        dst_s3.complete_multipart_upload(
            Bucket=dst_bucket,
            Key=dst_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        try:
            dst_s3.abort_multipart_upload(Bucket=dst_bucket, Key=dst_key, UploadId=upload_id)
        except ClientError:
            pass
        raise

    if tag_set:
        dst_s3.put_object_tagging(Bucket=dst_bucket, Key=dst_key, Tagging={"TagSet": tag_set})


def _do_copy(
    src_session: boto3.Session,
    dst_session: boto3.Session,
    src_bucket: str,
    src_key: str,
    dst_bucket: str,
    dst_key: str,
    size: int,
    storage_class: str,
    threshold: int,
    part_size: int,
    dry_run: bool,
) -> None:
    """Dispatch to simple or multipart copy based on size."""
    size_mib = size / 1024 / 1024
    if dry_run:
        method = "multipart" if size > threshold else "simple"
        log.debug("[DRY RUN] copy(%s) s3://%s/%s -> s3://%s/%s (%.1f MiB)", method, src_bucket, src_key, dst_bucket, dst_key, size_mib)
        return
    if size > threshold:
        _copy_multipart(src_session, dst_session, src_bucket, src_key, dst_bucket, dst_key, size, part_size, storage_class)
    else:
        _copy_simple(dst_session, src_bucket, src_key, dst_bucket, dst_key, storage_class)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _configure_logging(level: str, log_file: Optional[str]) -> None:
    fmt = "%(asctime)s %(levelname)-8s %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%S"
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, level), format=fmt, datefmt=datefmt, handlers=handlers)
    # Silence noisy boto3/urllib3 loggers unless DEBUG is requested
    if level != "DEBUG":
        for name in ("boto3", "botocore", "urllib3", "s3transfer"):
            logging.getLogger(name).setLevel(logging.WARNING)


def _raise_fd_limit() -> None:
    """Raise the open-file-descriptor limit to the OS hard cap.

    High worker counts open many sockets + botocore data files concurrently.
    macOS defaults to 256; we need far more for --workers > ~50.
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = min(hard, 65536)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            log.debug("Raised RLIMIT_NOFILE: %d -> %d (hard cap: %d)", soft, target, hard)
    except (ValueError, resource.error) as e:
        log.warning("Could not raise file descriptor limit: %s", e)


def main() -> int:
    """Two-phase Glacier restore + cross-region S3 copy."""
    args = parse_args()
    _configure_logging(args.log_level, args.log_file)
    _raise_fd_limit()

    src_session = _build_session(args.source_profile, args.source_region)
    dst_session = _build_session(args.dest_profile or args.source_profile, args.dest_region)

    log.info("Source region:       %s", args.source_region)
    log.info("Destination region:  %s  bucket: %s", args.dest_region, args.dest_bucket)
    log.info("Restore tier:        %s  days: %d", args.restore_tier, args.restore_days)
    log.info(
        "Multipart threshold: %.0f MiB  part size: %.0f MiB",
        args.multipart_threshold / 1024 / 1024,
        args.part_size / 1024 / 1024,
    )
    log.info("State DB:            %s", args.state_db)
    if args.dry_run:
        log.info("DRY RUN — no changes will be made.")

    conn = init_db(args.state_db)
    db_lock = threading.Lock()

    # Reset any mid-copy state from a previous crashed run so they get re-checked
    with db_lock:
        conn.execute("UPDATE objects SET state='restoring' WHERE state='copying'")
        if args.retry_errors:
            retried = conn.execute("UPDATE objects SET state='pending', error=NULL WHERE state='error'").rowcount
            log.info("--retry-errors: reset %d error objects back to 'pending'", retried)
        conn.commit()

    # Load manifest, inserting rows that aren't already in the DB
    loaded = load_manifest(conn, args.manifest, args.max_objects, args.source_bucket)
    log.info("Loaded %d new rows from manifest into state DB.", loaded)

    counts = _state_counts(conn)
    prior_errors = counts.get("error", 0)
    if any(counts.values()):
        log.info("Prior run state (from %s): %s", args.state_db, counts)
        if prior_errors:
            log.info("  (%d objects in 'error' state from a previous run — use --retry-errors to retry them)", prior_errors)
    else:
        log.info("State DB is empty — starting fresh.")

    # -----------------------------------------------------------------------
    # Phase 1: submit restores (parallel)
    # -----------------------------------------------------------------------
    if args.copy_only:
        log.info("--copy-only: skipping restore submission, going straight to copy phase.")
        with db_lock:
            promoted = conn.execute("UPDATE objects SET state='restoring' WHERE state='pending'").rowcount
            conn.commit()
        if promoted:
            log.info(
                "--copy-only: promoted %d pending objects to 'restoring' — poll loop will check actual restore status",
                promoted,
            )

    stop_ticker = threading.Event()
    if args.status_interval > 0:
        _start_status_ticker(conn, args.status_interval, stop_ticker)
        log.info("Status ticker started (every %ds). Use --status-interval 0 to disable.", args.status_interval)

    pending = conn.execute("SELECT bucket, key FROM objects WHERE state='pending'").fetchall() if not args.copy_only else []
    error_count_p1 = 0
    if pending:
        log.info("Phase 1: submitting restore requests for %d objects...", len(pending))
        submitted = 0
        ready_direct = 0

        def _submit_one(row: Tuple[str, str]) -> Tuple[str, str, str, str]:
            bucket, key = row
            try:
                new_state = _submit_restore(src_session, bucket, key, args.restore_tier, args.restore_days, args.dry_run)
                return bucket, key, new_state, ""
            except Exception as e:
                return bucket, key, "error", str(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for result in pool.map(_submit_one, pending):
                bucket, key, new_state, err = result
                _set_state(conn, db_lock, bucket, key, new_state, err)
                if new_state == "ready":
                    ready_direct += 1
                elif new_state == "restoring":
                    submitted += 1
                else:
                    error_count_p1 += 1
                    log.error("Restore failed s3://%s/%s: %s", bucket, key, err)
                submitted_total = submitted + ready_direct + error_count_p1
                if submitted_total in {1, 10, 100} or (submitted_total > 0 and submitted_total % 1000 == 0):
                    log.info(
                        "Phase 1 progress: %d processed (%d immediate, %d restoring, %d errors)",
                        submitted_total, ready_direct, submitted, error_count_p1,
                    )

        log.info("Phase 1 complete: %d restore requests submitted, %d immediately ready, %d errors", submitted, ready_direct, error_count_p1)

    if args.restore_only:
        c = _state_counts(conn)
        new_errors = error_count_p1 if pending else 0
        total_errors = c.get("error", 0)
        log.info("--restore-only: exiting after restore submission.")
        log.info("  Restoring:    %d  (objects awaiting Glacier restore — will be copied next run)", c.get("restoring", 0))
        log.info("  Ready:        %d  (objects already available, will be copied next run)", c.get("ready", 0))
        log.info("  Errors:       %d total (%d new this run, %d from prior runs)", total_errors, new_errors, total_errors - new_errors)
        log.info("Re-run without --restore-only tomorrow to execute the copy phase.")
        return 1 if new_errors else 0

    # -----------------------------------------------------------------------
    # Phase 2: poll restore status + copy
    # -----------------------------------------------------------------------
    log.info("Phase 2: polling for restore completion and copying...")

    done_count = 0
    error_count = 0
    poll_batch = 500   # objects to poll per sweep
    copy_queue_size = args.workers * 4

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        in_flight: Dict[concurrent.futures.Future, Tuple[str, str]] = {}

        def _drain_completed() -> None:
            nonlocal done_count, error_count
            done_futs = [f for f in in_flight if f.done()]
            for fut in done_futs:
                bucket, key = in_flight.pop(fut)
                try:
                    fut.result()
                    _set_state(conn, db_lock, bucket, key, "done")
                    done_count += 1
                    if done_count in {1, 10, 100} or done_count % 250 == 0:
                        log.info("Phase 2 progress: %d objects copied so far (%d errors)", done_count, error_count)
                except Exception as e:
                    _set_state(conn, db_lock, bucket, key, "error", str(e))
                    error_count += 1
                    log.error("Copy failed s3://%s/%s: %s", bucket, key, e)

        sweep = 0
        interrupted = False
        try:
            while True:
                sweep += 1
                _drain_completed()

                # Promote any restoring objects that are now available
                restoring_rows = conn.execute(
                    "SELECT bucket, key FROM objects WHERE state='restoring' LIMIT ?", (poll_batch,)
                ).fetchall()

                newly_ready = 0
                if restoring_rows:
                    def _check_one(row: Tuple[str, str]) -> Tuple[str, str, str]:
                        bucket, key = row
                        try:
                            status = _check_restore_status(src_session, bucket, key)
                            return bucket, key, status
                        except Exception:
                            return bucket, key, "restoring"  # retry next sweep

                    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.workers, 32)) as poll_pool:
                        for bucket, key, status in poll_pool.map(_check_one, restoring_rows):
                            if status == "ready":
                                _set_state(conn, db_lock, bucket, key, "ready")
                                newly_ready += 1

                    log.info(
                        "Sweep %d: polled %d restoring objects — %d newly ready, %d still restoring",
                        sweep, len(restoring_rows), newly_ready, len(restoring_rows) - newly_ready,
                    )

                # Dispatch ready objects to copy workers (respect queue cap)
                dispatched = 0
                while len(in_flight) < copy_queue_size:
                    rows = conn.execute(
                        "SELECT bucket, key, size FROM objects WHERE state='ready' LIMIT ?",
                        (copy_queue_size - len(in_flight),),
                    ).fetchall()
                    if not rows:
                        break
                    for bucket, key, size in rows:
                        _set_state(conn, db_lock, bucket, key, "copying")
                        dst_key = args.dest_prefix + key if args.dest_prefix else key
                        fut = executor.submit(
                            _do_copy,
                            src_session, dst_session,
                            bucket, key,
                            args.dest_bucket, dst_key,
                            size,
                            args.storage_class,
                            args.multipart_threshold,
                            args.part_size,
                            args.dry_run,
                        )
                        in_flight[fut] = (bucket, key)
                        dispatched += 1

                if dispatched:
                    log.info("Sweep %d: dispatched %d objects to copy workers (%d in-flight)", sweep, dispatched, len(in_flight))

                # Check termination condition
                c = _state_counts(conn)
                remaining = c.get("pending", 0) + c.get("restoring", 0) + c.get("ready", 0) + c.get("copying", 0) + len(in_flight)
                if remaining == 0:
                    break

                # Only sleep when there is truly nothing to do: no copies in-flight,
                # no objects ready to dispatch, and Glacier restores are still pending.
                # Never sleep while ready objects are waiting — that stalls copying.
                if newly_ready == 0 and not in_flight and c.get("ready", 0) == 0:
                    restoring_left = c.get("restoring", 0)
                    if restoring_left:
                        log.info(
                            "Sweep %d: %d restoring, nothing in-flight — sleeping %ds before next poll...",
                            sweep, restoring_left, args.poll_interval,
                        )
                        try:
                            time.sleep(args.poll_interval)
                        except KeyboardInterrupt:
                            log.info("Interrupted during poll sleep — stopping after in-flight copies finish.")
                            interrupted = True
                            break

        except KeyboardInterrupt:
            log.info("Interrupted — waiting for %d in-flight copies to finish before exiting...", len(in_flight))
            interrupted = True

        # Final drain
        for fut in concurrent.futures.as_completed(list(in_flight.keys())):
            bucket, key = in_flight[fut]
            try:
                fut.result()
                _set_state(conn, db_lock, bucket, key, "done")
                done_count += 1
            except Exception as e:
                _set_state(conn, db_lock, bucket, key, "error", str(e))
                error_count += 1
                log.error("Copy failed s3://%s/%s: %s", bucket, key, e)

    stop_ticker.set()

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    final = _state_counts(conn)
    log.info("=== Copy Summary%s ===", " (interrupted)" if interrupted else "")
    log.info("Done:      %d", final.get("done", 0))
    log.info("Errors:    %d", final.get("error", 0))
    log.info("Restoring: %d  (re-run to continue)", final.get("restoring", 0))
    log.info("Remaining: %d", final.get("pending", 0) + final.get("ready", 0))
    if interrupted:
        log.info("Re-run with --copy-only to resume from where this left off.")

    if final.get("error", 0):
        log.warning("Re-query errors: SELECT key, error FROM objects WHERE state='error';")
        log.warning("State DB: %s", args.state_db)

    return 1 if final.get("error", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
