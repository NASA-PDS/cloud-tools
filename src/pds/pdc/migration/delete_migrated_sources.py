#!/usr/bin/env python3
r"""Delete the source objects of a migration plan once their content is safely kept elsewhere.

Run this after ``pdc-execute-prefix-migration`` has copied and verified a plan.  It deletes exactly the keys
the plan names and never a whole prefix, so an object added after planning cannot be touched.  The bucket is
versioned, so a delete only adds a delete marker; the data stays recoverable as a noncurrent version until
your lifecycle rule purges it.  Every key is also written to a deletions file before it is deleted, together
with where its content is kept, so a restore stays possible after that window.

What is deleted:

  * The source of every MOVE, RENAME, SUPERSEDE and GARBAGE row, once its copy is verified and the destination
    listing still shows it with the planned size and ETag.
  * The source of a DROP row, only if the object that keeps its content has a verified copy and a fresh
    comparison of size, ETag and CRC64NVME between the two agrees.
  * JUNK files (``.DS_Store``, ``._*``, ``.*.swp``), which are not copied.

It refuses to start unless every copy in the plan is verified (use ``--allow-partial`` to delete only the
verified ones), and it refuses a plan that differs from the one the state file was started with.

You must choose a mode:

  --dry-run   Report what would be deleted and what is blocked.  Deletes nothing.
  --execute   Delete.

Usage
-----
  pdc-delete-migrated-sources \
    --bucket my-bucket \
    --profile my-aws-profile \
    --plan plan_LADEE.csv \
    --dry-run

Optional
--------
  --state FILE         State file written by pdc-execute-prefix-migration (default: <plan>.exec.db)
  --deletions FILE     Record of deleted keys (default: <plan>.deleted.csv)
  --workers N          Parallel request threads (default: 16)
  --allow-partial      Delete the verified copies' sources even though other copies are not verified yet
  --max-objects N      Stop after deleting N source objects in total (run a small slice first, then rerun)
  --region REGION      AWS region (default: us-west-2)
  --log-level LEVEL    DEBUG | INFO | WARNING | ERROR (default: INFO)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import logging
import sys
from typing import Dict
from typing import List
from typing import Optional
from typing import TextIO
from typing import Tuple

import boto3
from botocore.exceptions import ClientError

from pds.pdc.migration.errors import MigrationPlanError
from pds.pdc.migration.errors import MigrationStateError
from pds.pdc.migration.execute_prefix_migration import ExecDb
from pds.pdc.migration.execute_prefix_migration import file_sha256
from pds.pdc.migration.execute_prefix_migration import SOURCE_DELETED
from pds.pdc.migration.execute_prefix_migration import VERIFIED
from pds.pdc.migration.s3_common import resumable
from pds.pdc.migration.s3_common import s3_client

log = logging.getLogger(__name__)

# DeleteObjects accepts at most 1000 keys per request.
DELETE_BATCH = 1000

REMOVED = "removed"


def content_matches(a: dict, b: dict) -> Optional[str]:
    """Compare two HeadObject responses for identical content; return the first problem, or None if they match.

    Size must match, and the single-part ETags and the CRC64NVME checksums that both objects have must match,
    with at least one of those two comparable.  Metadata and content type are deliberately ignored: two
    objects with the same bytes can differ there.
    """
    if a["ContentLength"] != b["ContentLength"]:
        return "size_mismatch"
    etag_a, etag_b = a.get("ETag", "").strip('"'), b.get("ETag", "").strip('"')
    compared = False
    if "-" not in etag_a and "-" not in etag_b:
        if etag_a != etag_b:
            return "etag_mismatch"
        compared = True
    if a.get("ChecksumCRC64NVME") and b.get("ChecksumCRC64NVME"):
        if a["ChecksumCRC64NVME"] != b["ChecksumCRC64NVME"]:
            return "crc64nvme_mismatch"
        compared = True
    return None if compared else "no_comparable_hash"


def list_destinations(session: boto3.Session, bucket: str, roots: List[str], db: ExecDb) -> int:
    """Record every object under the destination roots (key, size, ETag) so copies can be checked in bulk."""
    s3 = s3_client(session)
    db.clear_listing()
    total = 0
    for root in roots:
        pages = resumable(lambda r=root: s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=r))
        for page in pages:
            items = [(o["Key"], o["Size"], o["ETag"].strip('"')) for o in page.get("Contents", [])]
            db.store_listing(items)
            total += len(items)
            if total and total % 500000 < len(items):
                log.info("    listed %d destination objects so far", total)
    return total


def delete_keys(s3, bucket: str, keys: List[str]) -> List[str]:
    """Delete *keys* in one request and return the keys that failed.

    On a versioned bucket this adds delete markers; it never removes a stored version.
    """
    response = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True})
    return [e["Key"] for e in response.get("Errors", [])]


def delete_resumably(session: boto3.Session, bucket: str, keys: List[str]) -> List[str]:
    """Delete *keys*, turning a network or credential failure into a clean, resumable stop.  Returns refused keys."""
    return resumable(lambda: delete_keys(s3_client(session), bucket, keys))


def _record(writer: csv.writer, fh: TextIO, rows: List[Tuple[str, str, str, int, str]]) -> None:
    """Write the deletion record for *rows* and flush it to disk before anything is deleted."""
    writer.writerows(rows)
    fh.flush()


def delete_copied_sources(
    session: boto3.Session,
    bucket: str,
    db: ExecDb,
    writer: csv.writer,
    fh: TextIO,
    workers: int,
    limit: Optional[int] = None,
) -> int:
    """Delete the sources of verified copies that are still present at their destination.  Returns the count.

    Stops after *limit* deletions when a limit is given.
    """
    after = ""
    deleted = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            want = DELETE_BATCH * workers if limit is None else min(DELETE_BATCH * workers, limit - deleted)
            if want <= 0:
                return deleted
            rows = db.next_deletable_copies(after, want)
            if not rows:
                return deleted
            after = rows[-1][0]
            chunks = [rows[i : i + DELETE_BATCH] for i in range(0, len(rows), DELETE_BATCH)]
            _record(writer, fh, [(src, "COPIED", dst, size, etag) for src, dst, size, etag in rows])
            outcomes = pool.map(lambda chunk: delete_resumably(session, bucket, [c[0] for c in chunk]), chunks)
            for chunk, failed in zip(chunks, outcomes):
                refused = set(failed)
                ok = [c[0] for c in chunk if c[0] not in refused]
                db.mark_sources_deleted(ok)
                deleted += len(ok)
                if failed:
                    raise MigrationStateError(f"{len(failed)} deletes were refused, for example {failed[0]}")
            log.info("    deleted %d sources of copied objects so far", deleted)


def delete_duplicates(
    session: boto3.Session,
    bucket: str,
    db: ExecDb,
    writer: csv.writer,
    fh: TextIO,
    workers: int,
    limit: Optional[int] = None,
) -> Tuple[int, int]:
    """Delete DROP sources whose content is kept by a verified copy.  Returns ``(deleted, skipped)``.

    Stops after *limit* deletions when a limit is given.
    """
    after = ""
    deleted = skipped = 0

    def check(row: Tuple[str, str, int, str]):
        src, reference, size, etag = row
        kept = db.kept_copy(reference)
        if kept is None or kept[3] not in (VERIFIED, SOURCE_DELETED):
            return row, None, "kept_copy_not_verified"
        dst, kept_size, kept_etag, _ = kept
        if not db.seen_at_destination(dst, kept_size, kept_etag):
            return row, None, "kept_copy_not_at_destination"
        s3 = s3_client(session)
        try:
            mine = s3.head_object(Bucket=bucket, Key=src, ChecksumMode="ENABLED")
            theirs = s3.head_object(Bucket=bucket, Key=dst, ChecksumMode="ENABLED")
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return row, None, "missing"
            raise
        problem = content_matches(mine, theirs)
        return row, (dst if problem is None else None), problem or ""

    def checked(row: Tuple[str, str, int, str]):
        return resumable(lambda: check(row))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            want = DELETE_BATCH if limit is None else min(DELETE_BATCH, limit - deleted)
            if want <= 0:
                return deleted, skipped
            rows = db.next_removals("DROP", after, want)
            if not rows:
                return deleted, skipped
            after = rows[-1][0]
            results = list(pool.map(checked, rows))
            good = [(r, dst) for r, dst, _ in results if dst]
            db.set_removal_states([("skipped", note, r[0]) for r, dst, note in results if not dst])
            skipped += len(rows) - len(good)
            if not good:
                continue
            _record(writer, fh, [(r[0], "DROP", dst, r[2], r[3]) for r, dst in good])
            failed = set(delete_resumably(session, bucket, [r[0] for r, _ in good]))
            db.set_removal_states([(REMOVED, None, r[0]) for r, _ in good if r[0] not in failed])
            deleted += len(good) - len(failed)
            if failed:
                raise MigrationStateError(f"{len(failed)} deletes were refused, for example {sorted(failed)[0]}")
            log.info("    deleted %d duplicate sources so far (%d skipped)", deleted, skipped)


def delete_junk(
    session: boto3.Session, bucket: str, db: ExecDb, writer: csv.writer, fh: TextIO, limit: Optional[int] = None
) -> int:
    """Delete the JUNK files named in the plan.  Returns the count.  Stops after *limit* deletions if given."""
    deleted = 0
    while True:
        want = DELETE_BATCH if limit is None else min(DELETE_BATCH, limit - deleted)
        if want <= 0:
            return deleted
        rows = db.next_removals("JUNK", "", want)
        if not rows:
            return deleted
        _record(writer, fh, [(src, "JUNK", "", size, etag) for src, _, size, etag in rows])
        failed = set(delete_resumably(session, bucket, [r[0] for r in rows]))
        db.set_removal_states([(REMOVED, None, r[0]) for r in rows if r[0] not in failed])
        deleted += len(rows) - len(failed)
        if failed:
            raise MigrationStateError(f"{len(failed)} deletes were refused, for example {sorted(failed)[0]}")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(
        description="Delete migrated source objects once their content is safely kept elsewhere.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--bucket", required=True, help="S3 bucket the plan was made for")
    p.add_argument("--profile", default=None, help="AWS profile name")
    p.add_argument("--region", default="us-west-2", help="AWS region (default: us-west-2)")
    p.add_argument("--plan", required=True, help="Plan CSV written by pdc-plan-prefix-migration")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Report what would be deleted; delete nothing")
    mode.add_argument("--execute", action="store_true", help="Delete")
    p.add_argument("--state", default=None, help="State file (default: <plan>.exec.db)")
    p.add_argument("--deletions", default=None, help="Record of deleted keys (default: <plan>.deleted.csv)")
    p.add_argument("--workers", type=int, default=16, help="Parallel request threads (default: 16)")
    p.add_argument("--allow-partial", action="store_true", help="Delete verified copies even if others are not")
    p.add_argument(
        "--max-objects",
        type=int,
        default=None,
        help="Stop after deleting this many source objects in total; run a small slice first, then rerun without it",
    )
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


def summarize_plan(db: ExecDb) -> Dict[str, int]:
    """Log how many removals are eligible and why the others are not.  Returns the headline counts."""
    counts = db.counts()
    unverified = sum(n for state, (n, _) in counts.items() if state not in (VERIFIED, SOURCE_DELETED))
    copies = db.copy_counts_by_deletability()
    removals = db.removal_counts()
    log.info("Copies: %d verified and present at the destination, %d verified but not seen there",
             copies["deletable"], copies["blocked"])
    log.info("Copies not verified yet: %d", unverified)
    log.info("Duplicates (DROP) pending: %d   Junk pending: %d",
             removals.get(("DROP", "pending"), 0), removals.get(("JUNK", "pending"), 0))
    return {"unverified": unverified, "blocked": copies["blocked"], **copies}


def main() -> int:
    """Delete the sources of a verified migration plan."""
    args = parse_args()
    _configure_logging(args.log_level)
    state_path = args.state or f"{args.plan}.exec.db"
    session = boto3.Session(profile_name=args.profile, region_name=args.region)

    db = ExecDb(state_path)
    try:
        stored = db.meta("plan_sha256")
        if stored is None:
            raise MigrationStateError(f"{state_path} has no plan loaded; run pdc-execute-prefix-migration first.")
        if stored != file_sha256(args.plan):
            raise MigrationStateError("The plan file differs from the one this state file was started with.")
        if db.meta("removals_loaded") is None:
            log.info("Loading the DROP and JUNK rows of the plan")
            db.load_removals(args.plan)
            db.set_meta("removals_loaded", "1")

        roots = sorted(json.loads(db.meta("destination_roots") or "[]"))
        log.info("Listing %d destination prefix(es)", len(roots))
        log.info("Destination objects seen: %d", list_destinations(session, args.bucket, roots, db))
        headline = summarize_plan(db)

        if headline["unverified"] and not args.allow_partial:
            raise MigrationStateError(
                f"{headline['unverified']} copies are not verified yet. Finish pdc-execute-prefix-migration, "
                f"or use --allow-partial to delete only the verified ones."
            )
        if args.dry_run:
            log.info("DRY RUN: nothing was deleted.")
            return 0

        deletions = args.deletions or f"{args.plan}.deleted.csv"
        with open(deletions, "a", newline="") as fh:
            writer = csv.writer(fh)
            if fh.tell() == 0:
                writer.writerow(["deleted_source_key", "kind", "content_kept_at", "size", "etag"])
            log.info("Deleting sources of copied objects (record: %s)", deletions)
            budget = args.max_objects
            copied = delete_copied_sources(session, args.bucket, db, writer, fh, args.workers, budget)
            budget = None if budget is None else budget - copied
            log.info("Deleting duplicates")
            dropped, skipped = delete_duplicates(session, args.bucket, db, writer, fh, args.workers, budget)
            budget = None if budget is None else budget - dropped
            log.info("Deleting junk files")
            junk = delete_junk(session, args.bucket, db, writer, fh, budget)
        log.info("=== Deleted: %d copied sources, %d duplicates (%d skipped), %d junk ===", copied, dropped, skipped, junk)
        if skipped:
            log.warning("%d duplicates were NOT deleted because their kept copy could not be confirmed.", skipped)
    except MigrationPlanError as e:  # includes MigrationStoppedError and MigrationStateError
        log.error("%s", e)
        db.close()
        return 1
    except KeyboardInterrupt:
        log.warning("Interrupted. Progress is saved; rerun the same command to resume.")
        db.close()
        return 130
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
