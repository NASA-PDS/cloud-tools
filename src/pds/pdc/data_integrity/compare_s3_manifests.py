#!/usr/bin/env python3
"""
Compare two S3 checksum manifests.

Goal:
- Verify that every object in OLD has at least one matching object in NEW
  by content signature, regardless of key name.

Primary signature:
  (size, checksum_type, checksum_value)

Fallback/debug fields:
  checksum_algorithm, etag

Outputs:
- summary to stdout
- missing coverage CSV  (confirmed missing from NEW)
- unverifiable CSV      (no checksum available; cannot confirm presence in NEW)
- weak rows CSV         (NEW-side rows without usable checksums)

Exit codes:
  0 — PASS: every source object is confirmed present in destination
  1 — FAIL: one or more source objects are missing or unverifiable
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
from collections import Counter
from typing import Dict, List, Tuple


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", required=True, nargs="+", help="OLD manifest CSV(s), plain or gzipped")
    parser.add_argument("--new", required=True, nargs="+", help="NEW manifest CSV(s), plain or gzipped")
    parser.add_argument(
        "--missing-output",
        default="missing_in_new.csv",
        help="Output CSV file path for OLD objects with no matching signature in NEW (default: %(default)s)",
    )
    parser.add_argument(
        "--unverifiable-output",
        default="unverifiable.csv",
        help="Output CSV file path for OLD objects with no checksum; cannot confirm presence in NEW (default: %(default)s)",
    )
    parser.add_argument(
        "--weak-output",
        default="weak_checksum_rows.csv",
        help="Output CSV file path for NEW-side rows without usable checksums (default: %(default)s)",
    )
    return parser.parse_args()


def _open_csv(path: str) -> io.TextIOWrapper:
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", newline="")  # type: ignore[return-value]
    return open(path, "r", newline="", encoding="utf-8")


def load_rows(paths: List[str]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for path in paths:
        with _open_csv(path) as f:
            rows.extend(csv.DictReader(f))
    return rows


def usable_signature(row: Dict[str, str]) -> bool:
    value = (row.get("checksum_value") or "").strip()
    ctype = (row.get("checksum_type") or "").strip()
    size = (row.get("size") or "").strip()
    return bool(size and ctype and value and not value.startswith("ERROR:"))


def signature(row: Dict[str, str]) -> Tuple[str, str, str]:
    return (
        (row.get("size") or "").strip(),
        (row.get("checksum_type") or "").strip(),
        (row.get("checksum_value") or "").strip(),
    )


def main() -> int:
    args = parse_args()

    print(f"Loading OLD manifest(s): {args.old}")
    old_rows = load_rows(args.old)
    print(f"  Loaded {len(old_rows):,} rows from OLD manifest(s)")

    print(f"Loading NEW manifest(s): {args.new}")
    new_rows = load_rows(args.new)
    print(f"  Loaded {len(new_rows):,} rows from NEW manifest(s)")

    new_sig_counts: Counter = Counter()
    new_weak_rows: List[Dict[str, str]] = []

    print("Indexing NEW manifest signatures...")
    for row in new_rows:
        if usable_signature(row):
            new_sig_counts[signature(row)] += 1
        else:
            new_weak_rows.append(row)
    print(f"  {len(new_sig_counts):,} unique signatures indexed; {len(new_weak_rows):,} weak/unusable rows")

    missing_rows: List[Dict[str, str]] = []
    unverifiable_rows: List[Dict[str, str]] = []

    old_bucket = (old_rows[0].get("bucket") or "OLD") if old_rows else "OLD"
    new_bucket = (new_rows[0].get("bucket") or "NEW") if new_rows else "NEW"

    old_total = len(old_rows)
    old_covered = 0

    print(f"Comparing {old_total:,} OLD objects against NEW...")
    report_interval = max(1, old_total // 10)
    for i, row in enumerate(old_rows, 1):
        if i % report_interval == 0 or i == old_total:
            print(f"  Progress: {i:,}/{old_total:,} ({100 * i // old_total}%)")
        if not usable_signature(row):
            unverifiable_rows.append(row)
            continue

        sig = signature(row)
        if new_sig_counts[sig] > 0:
            old_covered += 1
        else:
            missing_rows.append(row)

    row_fieldnames = list(old_rows[0].keys()) if old_rows else []

    print(f"Writing missing objects report -> {args.missing_output}")
    with open(args.missing_output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row_fieldnames)
        writer.writeheader()
        writer.writerows(missing_rows)

    print(f"Writing unverifiable objects report -> {args.unverifiable_output}")
    with open(args.unverifiable_output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row_fieldnames)
        writer.writeheader()
        writer.writerows(unverifiable_rows)

    if new_weak_rows:
        new_fieldnames = list(new_rows[0].keys()) if new_rows else []
        print(f"Writing weak checksum report -> {args.weak_output}")
        with open(args.weak_output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=new_fieldnames)
            writer.writeheader()
            writer.writerows(new_weak_rows)

    missing_count = len(missing_rows)
    unverifiable_count = len(unverifiable_rows)
    success = (missing_count == 0 and unverifiable_count == 0)

    print("=== S3 Manifest Comparison Summary ===")
    print(f"Source bucket:                             {old_bucket}")
    print(f"Destination bucket:                        {new_bucket}")
    print()
    print(f"{old_bucket} objects total:                {old_total}")
    print(f"{old_bucket} objects confirmed in {new_bucket}: {old_covered}")
    print(f"{old_bucket} objects confirmed missing:    {missing_count}")
    print(f"{old_bucket} objects unable to verify:     {unverifiable_count}")
    print(f"{new_bucket} objects with weak checksum:   {len(new_weak_rows)}")
    print()
    print(f"Confirmed missing report:  {args.missing_output}")
    print(f"Unverifiable report:       {args.unverifiable_output}")
    if new_weak_rows:
        print(f"Weak checksum report:      {args.weak_output}")
    print()
    if success:
        print("RESULT: PASS")
        print(f"Every object in {old_bucket} is confirmed present in {new_bucket}.")
    else:
        print("RESULT: FAIL")
        if missing_count:
            print(f"  {missing_count} object(s) from {old_bucket} have no matching content signature in {new_bucket}.")
        if unverifiable_count:
            print(f"  {unverifiable_count} object(s) from {old_bucket} have no checksum and cannot be verified.")

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
