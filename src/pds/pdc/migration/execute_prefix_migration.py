#!/usr/bin/env python3
r"""Copy the objects in a migration plan to their new keys and verify every copy.

Reads the plan CSV written by ``pdc-plan-prefix-migration`` and, for every MOVE, RENAME, SUPERSEDE and GARBAGE
row, server-side copies ``src_key`` to ``dst_key`` within the bucket, then compares each copy with its source.
Nothing is ever deleted or overwritten here: sources are left in place, and a fresh start refuses to run if
any destination prefix already holds objects.

You must choose a mode:

  --dry-run   Read the plan, check the destinations and print what would be copied.  Writes nothing anywhere.
  --execute   Copy and verify.

Progress is kept in a SQLite state file (``<plan>.exec.db`` by default), saved after every batch, so a run
interrupted by a VPN drop or expired credentials continues where it stopped when you rerun the same command.
A state file is tied to the exact plan it was started with; a changed plan is refused.

An object counts as verified only when its size, its single-part ETag or CRC64NVME checksum (at least one of
them must be comparable), its user metadata and its content type match the source, and the source still has
the size and ETag recorded in the plan.

Usage
-----
  pdc-execute-prefix-migration \
    --bucket my-bucket \
    --profile my-aws-profile \
    --plan plan_LADEE.csv \
    --dry-run

Optional
--------
  --phase PHASE            copy | verify | both (default: both)
  --workers N              Parallel request threads (default: 64)
  --state FILE             SQLite state file (default: <plan>.exec.db)
  --retry-failed           Retry objects whose copy or verification failed on an earlier run
  --allow-existing-destinations
                           Start even though destination prefixes already hold objects
  --region REGION          AWS region (default: us-west-2)
  --log-level LEVEL        DEBUG | INFO | WARNING | ERROR (default: INFO)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sqlite3
import sys
import threading
from collections import Counter
from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple

import boto3
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError

from pds.pdc.migration.errors import MigrationPlanError
from pds.pdc.migration.errors import MigrationStateError
from pds.pdc.migration.errors import MigrationStoppedError
from pds.pdc.migration.rules import SUPERSEDED_PREFIX
from pds.pdc.migration.s3_common import resumable
from pds.pdc.migration.s3_common import run_batches
from pds.pdc.migration.s3_common import s3_client

log = logging.getLogger(__name__)

# Plan actions that copy an object to a new key.
COPY_ACTIONS = ("MOVE", "RENAME", "SUPERSEDE", "GARBAGE")

# A single CopyObject request handles objects up to 5 GiB.
MAX_COPY_OBJECT_BYTES = 5 * 1024**3

PENDING = "pending"
COPIED = "copied"
VERIFIED = "verified"
SOURCE_DELETED = "source_deleted"
COPY_FAILED = "copy_failed"
VERIFY_FAILED = "verify_failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS copies (
    src_key TEXT PRIMARY KEY,
    dst_key TEXT NOT NULL,
    action TEXT NOT NULL,
    size INTEGER NOT NULL,
    etag TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    error TEXT
);
CREATE INDEX IF NOT EXISTS copies_state ON copies (state, src_key);
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS removals (
    src_key TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    reference_key TEXT NOT NULL,
    size INTEGER NOT NULL,
    etag TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    note TEXT
);
CREATE TABLE IF NOT EXISTS dst_seen (key TEXT PRIMARY KEY, size INTEGER NOT NULL, etag TEXT NOT NULL);
"""


# ---------------------------------------------------------------------------
# Plan reading and pure checks
# ---------------------------------------------------------------------------


def file_sha256(path: str) -> str:
    """Return the SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_copy_rows(plan_path: str) -> Iterator[Tuple[str, str, str, int, str]]:
    """Yield ``(src_key, dst_key, action, size, etag)`` for every plan row that copies an object."""
    with open(plan_path, newline="") as fh:
        for row in csv.DictReader(fh):
            if row["action"] in COPY_ACTIONS and row["dst_key"]:
                yield row["src_key"], row["dst_key"], row["action"], int(row["size"]), row["etag"]


def destination_root(dst_key: str) -> str:
    """Return the prefix whose emptiness is checked before a fresh start.

    This is ``pds4/<family>/``, ``pds4/superseded/<top>/`` or ``potential-garbage/<top>/``; an object that sits
    directly under ``pds4/`` is its own root.
    """
    parts = dst_key.split("/")
    depth = 3 if dst_key.startswith(SUPERSEDED_PREFIX) else 2
    if len(parts) <= depth:
        return dst_key
    return "/".join(parts[:depth]) + "/"


def compare_heads(src: dict, dst: dict, planned_size: int, planned_etag: str) -> Optional[str]:
    """Compare HeadObject responses for a source and its copy; return the first problem found, or None.

    Args:
        src: HeadObject response for the source (with checksums enabled).
        dst: HeadObject response for the copy.
        planned_size: Size recorded in the plan.
        planned_etag: ETag recorded in the plan.
    """
    src_etag = src.get("ETag", "").strip('"')
    dst_etag = dst.get("ETag", "").strip('"')
    if src["ContentLength"] != planned_size or ("-" not in planned_etag and src_etag != planned_etag):
        return "source_changed_since_plan"
    if dst["ContentLength"] != src["ContentLength"]:
        return "size_mismatch"
    compared = False
    if "-" not in src_etag and "-" not in dst_etag:
        if src_etag != dst_etag:
            return "etag_mismatch"
        compared = True
    if src.get("ChecksumCRC64NVME") and dst.get("ChecksumCRC64NVME"):
        if src["ChecksumCRC64NVME"] != dst["ChecksumCRC64NVME"]:
            return "crc64nvme_mismatch"
        compared = True
    if not compared:
        return "no_comparable_hash"
    if (src.get("Metadata") or {}) != (dst.get("Metadata") or {}):
        return "metadata_mismatch"
    if src.get("ContentType") != dst.get("ContentType"):
        return "content_type_mismatch"
    return None


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class ExecDb:
    """Thread-safe wrapper around the SQLite state file."""

    def __init__(self, path: str):
        """Open (creating if needed) the state file at *path*."""
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def meta(self, name: str) -> Optional[str]:
        """Return a stored metadata value, or None."""
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    def set_meta(self, name: str, value: str) -> None:
        """Store a metadata value."""
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO meta (name, value) VALUES (?, ?)", (name, value))
            self._conn.commit()

    def load_plan(self, plan_path: str, batch: int = 5000) -> Set[str]:
        """Load the copy rows of a plan (rows already present are kept) and return their destination roots."""
        roots: Set[str] = set()
        rows: List[tuple] = []

        def flush() -> None:
            with self._lock:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO copies (src_key, dst_key, action, size, etag) VALUES (?, ?, ?, ?, ?)", rows
                )
                self._conn.commit()
            rows.clear()

        for row in iter_copy_rows(plan_path):
            roots.add(destination_root(row[1]))
            rows.append(row)
            if len(rows) >= batch:
                flush()
        flush()
        return roots

    def has_progress(self) -> bool:
        """Return True if any object has been copied (or tried) already."""
        with self._lock:
            return self._conn.execute("SELECT 1 FROM copies WHERE state != ? LIMIT 1", (PENDING,)).fetchone() is not None

    def next_keys(self, state: str, after: str, limit: int) -> List[str]:
        """Return up to *limit* source keys in *state*, in key order, after *after*."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT src_key FROM copies WHERE state = ? AND src_key > ? ORDER BY src_key LIMIT ?",
                (state, after, limit),
            )
            return [r[0] for r in cur]

    def rows_for(self, keys: List[str]) -> Dict[str, Tuple[str, int, str]]:
        """Return ``{src_key: (dst_key, size, etag)}`` for the given source keys."""
        marks = ",".join("?" for _ in keys)
        with self._lock:
            cur = self._conn.execute(
                f"SELECT src_key, dst_key, size, etag FROM copies WHERE src_key IN ({marks})", keys
            )
            return {k: (dst, size, etag) for k, dst, size, etag in cur}

    def set_states(self, updates: List[Tuple[str, Optional[str], str]]) -> None:
        """Apply ``(state, error, src_key)`` updates."""
        with self._lock:
            self._conn.executemany("UPDATE copies SET state = ?, error = ? WHERE src_key = ?", updates)
            self._conn.commit()

    def retry_failed(self) -> int:
        """Put failed objects back in line: failed copies are retried, failed verifications re-checked."""
        with self._lock:
            n = self._conn.execute(
                "UPDATE copies SET state = ?, error = NULL WHERE state = ?", (PENDING, COPY_FAILED)
            ).rowcount
            n += self._conn.execute(
                "UPDATE copies SET state = ?, error = NULL WHERE state = ?", (COPIED, VERIFY_FAILED)
            ).rowcount
            self._conn.commit()
        return n

    def counts(self) -> Dict[str, Tuple[int, int]]:
        """Return ``{state: (objects, bytes)}``."""
        with self._lock:
            cur = self._conn.execute("SELECT state, COUNT(*), COALESCE(SUM(size), 0) FROM copies GROUP BY state")
            return {state: (n, size) for state, n, size in cur}

    def failures(self, limit: int = 10) -> List[Tuple[str, str, str]]:
        """Return up to *limit* ``(src_key, state, error)`` rows for failed objects."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT src_key, state, error FROM copies WHERE state IN (?, ?) ORDER BY src_key LIMIT ?",
                (COPY_FAILED, VERIFY_FAILED, limit),
            )
            return list(cur)

    # -- cleanup of the sources (used by delete_migrated_sources) --------------------------------------------

    def load_removals(self, plan_path: str, batch: int = 5000) -> int:
        """Load the DROP and JUNK rows of a plan (rows already present are kept).  Returns how many are in the plan."""
        total = 0
        rows: List[tuple] = []

        def flush() -> None:
            with self._lock:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO removals (src_key, action, reference_key, size, etag) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
                self._conn.commit()
            rows.clear()

        with open(plan_path, newline="") as fh:
            for row in csv.DictReader(fh):
                if row["action"] in ("DROP", "JUNK"):
                    rows.append((row["src_key"], row["action"], row["reference_key"], int(row["size"]), row["etag"]))
                    total += 1
                    if len(rows) >= batch:
                        flush()
        flush()
        return total

    def clear_listing(self) -> None:
        """Forget the destination listing."""
        with self._lock:
            self._conn.execute("DELETE FROM dst_seen")
            self._conn.commit()

    def store_listing(self, items: List[Tuple[str, int, str]]) -> None:
        """Remember listed destination objects as ``(key, size, etag)``."""
        with self._lock:
            self._conn.executemany("INSERT OR REPLACE INTO dst_seen (key, size, etag) VALUES (?, ?, ?)", items)
            self._conn.commit()

    def copy_counts_by_deletability(self) -> Dict[str, int]:
        """Count verified copies whose destination was just seen with the planned size and ETag, and those not."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), SUM(s.key IS NOT NULL) FROM copies c LEFT JOIN dst_seen s "
                "ON s.key = c.dst_key AND s.size = c.size AND (s.etag = c.etag OR INSTR(c.etag, '-') > 0) "
                "WHERE c.state = ?",
                (VERIFIED,),
            ).fetchone()
        eligible = row[1] or 0
        return {"deletable": eligible, "blocked": (row[0] or 0) - eligible}

    def next_deletable_copies(self, after: str, limit: int) -> List[Tuple[str, str, int, str]]:
        """Return up to *limit* ``(src_key, dst_key, size, etag)`` of verified copies that may be deleted."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT c.src_key, c.dst_key, c.size, c.etag FROM copies c JOIN dst_seen s "
                "ON s.key = c.dst_key AND s.size = c.size AND (s.etag = c.etag OR INSTR(c.etag, '-') > 0) "
                "WHERE c.state = ? AND c.src_key > ? ORDER BY c.src_key LIMIT ?",
                (VERIFIED, after, limit),
            )
            return list(cur)

    def mark_sources_deleted(self, keys: List[str]) -> None:
        """Record that the sources of these copies were deleted."""
        self.set_states([(SOURCE_DELETED, None, k) for k in keys])

    def kept_copy(self, key: str) -> Optional[Tuple[str, int, str, str]]:
        """Follow a DROP chain from *key* to the copied object that keeps its content.

        Returns ``(dst_key, size, etag, state)`` of that copy, or None if the chain does not end at a copy row.
        """
        with self._lock:
            for _ in range(20):
                row = self._conn.execute(
                    "SELECT dst_key, size, etag, state FROM copies WHERE src_key = ?", (key,)
                ).fetchone()
                if row:
                    return row
                step = self._conn.execute(
                    "SELECT reference_key FROM removals WHERE src_key = ? AND action = 'DROP'", (key,)
                ).fetchone()
                if not step:
                    return None
                key = step[0]
        return None

    def seen_at_destination(self, key: str, size: int, etag: str) -> bool:
        """Return True if the destination listing holds *key* with this size (and ETag, unless multipart)."""
        with self._lock:
            row = self._conn.execute("SELECT size, etag FROM dst_seen WHERE key = ?", (key,)).fetchone()
        return bool(row) and row[0] == size and (row[1] == etag or "-" in etag)

    def next_removals(self, action: str, after: str, limit: int) -> List[Tuple[str, str, int, str]]:
        """Return up to *limit* pending ``(src_key, reference_key, size, etag)`` removals of one action."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT src_key, reference_key, size, etag FROM removals "
                "WHERE action = ? AND state = 'pending' AND src_key > ? ORDER BY src_key LIMIT ?",
                (action, after, limit),
            )
            return list(cur)

    def set_removal_states(self, updates: List[Tuple[str, Optional[str], str]]) -> None:
        """Apply ``(state, note, src_key)`` updates to removals."""
        with self._lock:
            self._conn.executemany("UPDATE removals SET state = ?, note = ? WHERE src_key = ?", updates)
            self._conn.commit()

    def removal_counts(self) -> Dict[Tuple[str, str], int]:
        """Return ``{(action, state): objects}`` for the removals."""
        with self._lock:
            cur = self._conn.execute("SELECT action, state, COUNT(*) FROM removals GROUP BY action, state")
            return {(action, state): n for action, state, n in cur}

    def close(self) -> None:
        """Close the state file."""
        self._conn.close()


# ---------------------------------------------------------------------------
# S3 operations
# ---------------------------------------------------------------------------


def copy_one(s3, bucket: str, src_key: str, dst_key: str, size: int) -> tuple:
    """Copy one object within the bucket and return ``(src_key, status, message)``.

    ``status`` is ``copied`` or ``failed``.  An AWS error other than a missing source is returned in the
    second position instead, so the batch runner can treat repeated ones as an outage.
    """
    if size > MAX_COPY_OBJECT_BYTES:
        return src_key, "failed", "object_over_5GiB_needs_multipart_copy"
    try:
        s3.copy_object(
            Bucket=bucket,
            Key=dst_key,
            CopySource={"Bucket": bucket, "Key": src_key},
            MetadataDirective="COPY",
            TaggingDirective="COPY",
            ServerSideEncryption="AES256",
            ChecksumAlgorithm="CRC64NVME",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return src_key, "failed", "source_missing"
        return src_key, e
    except BotoCoreError as e:
        return src_key, e
    return src_key, "copied", ""


def verify_one(s3, bucket: str, src_key: str, dst_key: str, size: int, etag: str) -> tuple:
    """Compare one copy with its source and return ``(src_key, status, message)``.

    ``status`` is ``verified`` or ``failed``.
    """
    try:
        src = s3.head_object(Bucket=bucket, Key=src_key, ChecksumMode="ENABLED")
        dst = s3.head_object(Bucket=bucket, Key=dst_key, ChecksumMode="ENABLED")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return src_key, "failed", "source_or_copy_missing"
        return src_key, e
    except BotoCoreError as e:
        return src_key, e
    problem = compare_heads(src, dst, size, etag)
    return (src_key, "failed", problem) if problem else (src_key, "verified", "")


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


def check_destinations_empty(session: boto3.Session, bucket: str, roots: Set[str]) -> None:
    """Refuse to start if any destination prefix already holds objects.

    Raises:
        MigrationStateError: naming the prefixes that are not empty.
    """
    s3 = s3_client(session)
    occupied = [
        root
        for root in sorted(roots)
        if resumable(lambda r=root: s3.list_objects_v2(Bucket=bucket, Prefix=r, MaxKeys=1).get("KeyCount", 0))
    ]
    if occupied:
        shown = ", ".join(occupied[:10])
        raise MigrationStateError(
            f"{len(occupied)} destination prefix(es) already hold objects (for example: {shown}). Copying could "
            f"overwrite them. Remove them, or use --allow-existing-destinations if they are from an earlier run."
        )


def run_copy_phase(session: boto3.Session, bucket: str, db: ExecDb, workers: int) -> None:
    """Copy every pending object."""
    pending = db.counts().get(PENDING, (0, 0))[0]
    if not pending:
        return
    log.info("Copying %d objects", pending)
    batch_rows: Dict[str, Tuple[str, int, str]] = {}

    def next_batch(after: str, limit: int) -> List[str]:
        keys = db.next_keys(PENDING, after, limit)
        batch_rows.clear()
        if keys:
            batch_rows.update(db.rows_for(keys))
        return keys

    def work(key: str):
        dst, size, _ = batch_rows[key]
        return copy_one(s3_client(session), bucket, key, dst, size)

    def save(results: List[tuple]) -> None:
        db.set_states(
            [(COPIED, None, k) if status == "copied" else (COPY_FAILED, message, k) for k, status, message in results]
        )

    run_batches(workers, next_batch, work, save, pending, "copy")


def run_verify_phase(session: boto3.Session, bucket: str, db: ExecDb, workers: int) -> None:
    """Verify every copied object against its source."""
    pending = db.counts().get(COPIED, (0, 0))[0]
    if not pending:
        return
    log.info("Verifying %d copied objects", pending)
    batch_rows: Dict[str, Tuple[str, int, str]] = {}

    def next_batch(after: str, limit: int) -> List[str]:
        keys = db.next_keys(COPIED, after, limit)
        batch_rows.clear()
        if keys:
            batch_rows.update(db.rows_for(keys))
        return keys

    def work(key: str):
        dst, size, etag = batch_rows[key]
        return verify_one(s3_client(session), bucket, key, dst, size, etag)

    def save(results: List[tuple]) -> None:
        db.set_states(
            [
                (VERIFIED, None, k) if status == "verified" else (VERIFY_FAILED, message, k)
                for k, status, message in results
            ]
        )

    run_batches(workers, next_batch, work, save, pending, "verify")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(
        description="Copy the objects in a migration plan to their new keys and verify every copy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--bucket", required=True, help="S3 bucket the plan was made for")
    p.add_argument("--profile", default=None, help="AWS profile name")
    p.add_argument("--region", default="us-west-2", help="AWS region (default: us-west-2)")
    p.add_argument("--plan", required=True, help="Plan CSV written by pdc-plan-prefix-migration")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Show what would be copied; write nothing")
    mode.add_argument("--execute", action="store_true", help="Copy and verify")
    p.add_argument("--phase", choices=["copy", "verify", "both"], default="both")
    p.add_argument("--workers", type=int, default=64, help="Parallel request threads (default: 64)")
    p.add_argument("--state", default=None, help="SQLite state file (default: <plan>.exec.db)")
    p.add_argument("--retry-failed", action="store_true", help="Retry objects that failed on an earlier run")
    p.add_argument("--allow-existing-destinations", action="store_true", help="Start although destinations hold objects")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


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


def dry_run(session: boto3.Session, args: argparse.Namespace) -> int:
    """Describe what an execute run would copy.  Reads the plan and lists destination prefixes; writes nothing."""
    counts: Counter = Counter()
    sizes: Counter = Counter()
    samples: Dict[str, List[Tuple[str, str]]] = {a: [] for a in COPY_ACTIONS}
    roots: Set[str] = set()
    too_big = 0
    for src, dst, action, size, _ in iter_copy_rows(args.plan):
        counts[action] += 1
        sizes[action] += size
        roots.add(destination_root(dst))
        too_big += size > MAX_COPY_OBJECT_BYTES
        if len(samples[action]) < 3:
            samples[action].append((src, dst))

    log.info("DRY RUN: nothing will be written.  Plan: %s", args.plan)
    for action in COPY_ACTIONS:
        log.info("%-9s %10d objects  %16d bytes", action, counts[action], sizes[action])
        for src, dst in samples[action]:
            log.info("    s3://%s/%s\n      -> s3://%s/%s", args.bucket, src, args.bucket, dst)
    if too_big:
        log.warning("%d objects are over 5 GiB and need a multipart copy, which this tool does not do yet", too_big)
    log.info("Destination prefixes to be written: %d", len(roots))
    try:
        check_destinations_empty(session, args.bucket, roots)
        log.info("All destination prefixes are empty.")
    except MigrationStateError as e:
        log.warning("%s", e)
    return 0


def main() -> int:
    """Copy and verify the objects in a migration plan."""
    args = parse_args()
    _configure_logging(args.log_level)
    session = boto3.Session(profile_name=args.profile, region_name=args.region)

    if args.dry_run:
        try:
            return dry_run(session, args)
        except MigrationStoppedError as e:
            log.error("%s", e)
            return 1

    db = ExecDb(args.state or f"{args.plan}.exec.db")
    try:
        fingerprint = file_sha256(args.plan)
        stored = db.meta("plan_sha256")
        if stored is None:
            log.info("Loading the plan into %s", args.state or f"{args.plan}.exec.db")
            roots = db.load_plan(args.plan)
            db.set_meta("destination_roots", json.dumps(sorted(roots)))
            db.set_meta("plan_sha256", fingerprint)
        elif stored != fingerprint:
            raise MigrationStateError(
                "The plan file has changed since this state file was started. Use a different --state file, "
                "or the original plan."
            )

        if args.retry_failed:
            log.info("Re-queued %d failed objects", db.retry_failed())
        if not db.has_progress() and not args.allow_existing_destinations:
            check_destinations_empty(session, args.bucket, set(json.loads(db.meta("destination_roots") or "[]")))

        if args.phase in ("copy", "both"):
            run_copy_phase(session, args.bucket, db, args.workers)
        if args.phase in ("verify", "both"):
            run_verify_phase(session, args.bucket, db, args.workers)
    except MigrationPlanError as e:  # includes MigrationStoppedError and MigrationStateError
        log.error("%s", e)
        db.close()
        return 1
    except KeyboardInterrupt:
        log.warning("Interrupted. Progress is saved; rerun the same command to resume.")
        db.close()
        return 130

    counts = db.counts()
    log.info("=== Summary ===")
    for state in (PENDING, COPIED, VERIFIED, COPY_FAILED, VERIFY_FAILED):
        n, size = counts.get(state, (0, 0))
        log.info("%-14s %10d objects  %16d bytes", state, n, size)
    failures = db.failures()
    for src, state, error in failures:
        log.warning("%s: %s (%s)", state, src, error)
    db.close()
    failed = sum(counts.get(s, (0, 0))[0] for s in (COPY_FAILED, VERIFY_FAILED))
    log.info("Sources were not modified or deleted.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
