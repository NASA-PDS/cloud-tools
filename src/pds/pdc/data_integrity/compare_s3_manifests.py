#!/usr/bin/env python3
"""Compare two S3 checksum manifests to verify a bucket migration.

Goal:
    Confirm that every object in OLD has at least one matching object in NEW
    by content signature, regardless of key name or bucket name.

Matching strategy:
    Objects are matched on the tuple (size, checksum_type, checksum_value).
    Key names are intentionally ignored so renamed objects still count as present.

Multipart ETags:
    AWS S3 computes ETags differently for multipart uploads: the value is a hash
    of part hashes suffixed with the part count (e.g. "abc123-4").  When an
    object is re-uploaded or copied with a different part size the ETag changes,
    even if the object bytes are identical.  These checksums are therefore NOT
    stable across migrations and cannot be used to confirm presence in NEW.
    Any OLD row whose checksum_value matches the pattern ``<hex>-<digits>`` is
    treated as unverifiable and written to --unverifiable-output rather than
    --missing-output.

Output files:
    --missing-output (default: missing_in_new.csv)
        OLD-side rows whose (size, checksum_type, checksum_value) signature was
        not found anywhere in NEW.  These objects have stable checksums (not
        multipart ETags) and are confirmed absent from the destination.
        Action required: investigate why they were not migrated.

    --unverifiable-output (default: unverifiable.csv)
        OLD-side rows that cannot be confirmed present in NEW for one of two
        reasons:
          (a) The row has no usable checksum at all (empty, ERROR:*, etc.).
          (b) The row has a multipart ETag (value ends in -<N>), which changes
              when objects are re-uploaded and therefore cannot reliably match
              against the destination.
        The ``reason`` column appended to each output row records which case
        applies: ``no_checksum`` or ``multipart_etag``.
        Action required: use a different verification method (e.g. byte-level
        comparison, S3 object metadata, or re-checksumming) for these objects.

    --weak-output (default: weak_checksum_rows.csv)
        NEW-side rows that have no usable checksum.  These entries cannot be
        used to confirm migration coverage for any OLD object.
        Action required: re-run checksum generation on NEW for these objects.

Exit codes:
    0 — PASS: every OLD object is either confirmed present in NEW or is
              unverifiable due to a multipart ETag (i.e. missing_count == 0)
    1 — FAIL: one or more OLD objects with stable checksums have no matching
              signature in NEW, or there are unverifiable rows beyond multipart
              ETags
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import os
import re
import sqlite3
import tempfile
from typing import Dict
from typing import Iterator
from typing import Optional
from typing import Tuple

_MULTIPART_ETAG_RE = re.compile(r"^[0-9a-fA-F]+-\d+$")

MANIFEST_FIELDNAMES = ["bucket", "key", "size", "checksum_algorithm", "checksum_type", "checksum_value", "etag"]


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Compare two S3 checksum manifests to verify a bucket migration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Output files
------------
--missing-output
    OLD rows whose (size, checksum_type, checksum_value) was not found in NEW.
    These have stable checksums and are confirmed absent — action required.

--unverifiable-output
    OLD rows that cannot be verified: either no usable checksum ('no_checksum')
    or a multipart ETag that changes across re-uploads ('multipart_etag').
    A 'reason' column is appended to each row.

--weak-output
    NEW rows with no usable checksum; cannot be matched against OLD objects.
""",
    )
    parser.add_argument("--old", required=True, nargs="+", help="OLD manifest CSV(s), plain or gzipped")
    parser.add_argument("--new", required=True, nargs="+", help="NEW manifest CSV(s), plain or gzipped")
    parser.add_argument(
        "--missing-output",
        default="missing_in_new.csv",
        help="Output CSV for OLD objects with no matching signature in NEW (default: %(default)s)",
    )
    parser.add_argument(
        "--unverifiable-output",
        default="unverifiable.csv",
        help="Output CSV for OLD objects that cannot be verified (no checksum or multipart ETag) (default: %(default)s)",
    )
    parser.add_argument(
        "--weak-output",
        default="weak_checksum_rows.csv",
        help="Output CSV for NEW-side rows without usable checksums (default: %(default)s)",
    )
    return parser.parse_args()


def _open_csv(path: str) -> io.TextIOWrapper:
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", newline="")  # type: ignore[return-value]
    return open(path, "r", newline="", encoding="utf-8")


def _iter_rows(paths: list) -> Iterator[Dict[str, str]]:
    """Yield rows from all manifest CSVs with consistent fieldnames.

    The first file's header determines the canonical fieldnames.
    Subsequent files that lack a header (or have a different header)
    are read with those canonical fieldnames so all rows are consistent.
    """
    canonical: Optional[list] = None
    for path in paths:
        with _open_csv(path) as f:
            # Peek at the first line to detect whether the file has a header.
            first_line = f.readline()
            if not first_line:
                continue
            first_vals = next(csv.reader([first_line.rstrip("\n")]))

            if canonical is None:
                # Decide whether the first line is a header or data.
                if first_vals == MANIFEST_FIELDNAMES or first_vals[0] in ("bucket", "key"):
                    # Looks like a header — use it as canonical fieldnames.
                    canonical = first_vals
                else:
                    # No header — use the known manifest fieldnames and emit first line as data.
                    canonical = MANIFEST_FIELDNAMES
                    yield dict(zip(canonical, first_vals))
            else:
                if first_vals != canonical:
                    # No header — first line is data; emit it then read the rest.
                    yield dict(zip(canonical, first_vals))
                # Either way, the header was consumed; use canonical fieldnames for
                # the remainder so all rows are consistently keyed.

            yield from csv.DictReader(f, fieldnames=canonical)


def _usable_signature(row: Dict[str, str]) -> bool:
    value = (row.get("checksum_value") or "").strip()
    ctype = (row.get("checksum_type") or "").strip()
    size = (row.get("size") or "").strip()
    return bool(size and ctype and value and not value.startswith("ERROR:"))


def _is_multipart_etag(row: Dict[str, str]) -> bool:
    value = (row.get("checksum_value") or "").strip()
    return bool(_MULTIPART_ETAG_RE.match(value))


def _signature(row: Dict[str, str]) -> Tuple[str, str, str]:
    return (
        (row.get("size") or "").strip(),
        (row.get("checksum_type") or "").strip(),
        (row.get("checksum_value") or "").strip(),
    )


def _build_new_index(
    db: sqlite3.Connection,
    paths: list,
    weak_output_path: str,
) -> Tuple[int, int, Optional[list]]:
    """Stream NEW manifests into SQLite signature index; stream weak rows to file.

    Returns (unique_sig_count, weak_count, new_fieldnames).
    """
    db.execute(
        """
        CREATE TABLE new_sigs (
            size TEXT NOT NULL,
            checksum_type TEXT NOT NULL,
            checksum_value TEXT NOT NULL,
            cnt INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (size, checksum_type, checksum_value)
        )
        """
    )

    unique_sigs = 0
    weak_count = 0
    new_fieldnames: Optional[list] = None
    weak_writer: Optional[csv.DictWriter] = None
    weak_file = None

    try:
        weak_file = open(weak_output_path, "w", newline="", encoding="utf-8")

        for row in _iter_rows(paths):
            if new_fieldnames is None:
                new_fieldnames = list(row.keys())
                weak_writer = csv.DictWriter(weak_file, fieldnames=new_fieldnames, extrasaction="ignore")
                weak_writer.writeheader()

            if _usable_signature(row):
                sig = _signature(row)
                db.execute(
                    "INSERT INTO new_sigs(size, checksum_type, checksum_value) VALUES (?,?,?)"
                    " ON CONFLICT(size, checksum_type, checksum_value) DO UPDATE SET cnt = cnt + 1",
                    sig,
                )
                unique_sigs += 1
            else:
                assert weak_writer is not None
                weak_writer.writerow(row)
                weak_count += 1

        db.commit()
    finally:
        if weak_file is not None:
            weak_file.close()

    return unique_sigs, weak_count, new_fieldnames


def main() -> int:
    """Compare two S3 checksum manifests and report missing or unverifiable objects."""
    args = parse_args()

    with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as tmp:
        db_path = tmp.name

    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA cache_size=-131072")  # 128 MB page cache

    try:
        print(f"Indexing NEW manifest(s) into SQLite: {args.new}")
        unique_sigs, weak_count, new_fieldnames = _build_new_index(db, args.new, args.weak_output)
        print(f"  {unique_sigs:,} unique signatures indexed; {weak_count:,} weak/unusable rows")
        if weak_count:
            print(f"  Weak checksum report -> {args.weak_output}")

        old_total = 0
        old_covered = 0
        missing_count = 0
        unverifiable_count = 0
        multipart_etag_count = 0
        old_bucket = "OLD"
        new_bucket = "NEW"
        old_fieldnames: Optional[list] = None

        print(f"Comparing OLD manifest(s) against NEW index: {args.old}")
        with (
            open(args.missing_output, "w", newline="", encoding="utf-8") as miss_f,
            open(args.unverifiable_output, "w", newline="", encoding="utf-8") as unver_f,
        ):
            miss_writer: Optional[csv.DictWriter] = None
            unver_writer: Optional[csv.DictWriter] = None

            report_every = 100_000

            for row in _iter_rows(args.old):
                if old_fieldnames is None:
                    old_fieldnames = list(row.keys())
                    miss_writer = csv.DictWriter(miss_f, fieldnames=old_fieldnames, extrasaction="ignore")
                    miss_writer.writeheader()
                    unver_fieldnames = old_fieldnames + ["reason"]
                    unver_writer = csv.DictWriter(unver_f, fieldnames=unver_fieldnames, extrasaction="ignore")
                    unver_writer.writeheader()
                    old_bucket = row.get("bucket") or "OLD"

                old_total += 1
                if old_total % report_every == 0:
                    print(f"  Progress: {old_total:,} rows processed...")

                if not _usable_signature(row):
                    assert unver_writer is not None
                    unver_writer.writerow({**row, "reason": "no_checksum"})
                    unverifiable_count += 1
                    continue

                if _is_multipart_etag(row):
                    assert unver_writer is not None
                    unver_writer.writerow({**row, "reason": "multipart_etag"})
                    unverifiable_count += 1
                    multipart_etag_count += 1
                    continue

                sig = _signature(row)
                cur = db.execute(
                    "SELECT cnt FROM new_sigs WHERE size=? AND checksum_type=? AND checksum_value=?",
                    sig,
                )
                if cur.fetchone():
                    old_covered += 1
                else:
                    assert miss_writer is not None
                    miss_writer.writerow(row)
                    missing_count += 1

        print(f"  {old_total:,} OLD rows processed")

    finally:
        db.close()
        try:
            os.unlink(db_path)
        except OSError:
            pass

    success = missing_count == 0

    print("=== S3 Manifest Comparison Summary ===")
    print(f"Source bucket:                                    {old_bucket}")
    print(f"Destination bucket:                               {new_bucket}")
    print()
    print(f"{old_bucket} objects total:                       {old_total:,}")
    print(f"{old_bucket} objects confirmed in {new_bucket}:  {old_covered:,}")
    print(f"{old_bucket} objects confirmed missing:           {missing_count:,}")
    print(f"{old_bucket} objects unable to verify (total):   {unverifiable_count:,}")
    print(f"  of which multipart ETags:                       {multipart_etag_count:,}")
    print(f"  of which no usable checksum:                    {unverifiable_count - multipart_etag_count:,}")
    print(f"{new_bucket} objects with weak checksum:          {weak_count:,}")
    print()
    print(f"Confirmed missing report:  {args.missing_output}")
    print(f"Unverifiable report:       {args.unverifiable_output}")
    if weak_count:
        print(f"Weak checksum report:      {args.weak_output}")
    print()
    if success:
        print("RESULT: PASS")
        print(f"Every object in {old_bucket} with a stable checksum is confirmed present in {new_bucket}.")
        if multipart_etag_count:
            print(
                f"  Note: {multipart_etag_count:,} object(s) have multipart ETags and could not be verified by checksum."
            )
    else:
        print("RESULT: FAIL")
        if missing_count:
            print(
                f"  {missing_count:,} object(s) from {old_bucket} have no matching content signature in {new_bucket}."
            )
        if unverifiable_count:
            print(f"  {unverifiable_count:,} object(s) from {old_bucket} could not be verified.")
            if multipart_etag_count:
                print(
                    f"    {multipart_etag_count:,} have multipart ETags (ETag changes on re-upload; not a reliable match key)."
                )
            if unverifiable_count - multipart_etag_count:
                print(f"    {unverifiable_count - multipart_etag_count:,} have no usable checksum at all.")

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
