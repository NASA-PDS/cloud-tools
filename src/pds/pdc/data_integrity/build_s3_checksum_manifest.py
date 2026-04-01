#!/usr/bin/env python3
"""
Build a per-object checksum manifest for an S3 bucket.

Checksum resolution order (first match wins):
  1. Native S3 checksum from GetObjectAttributes (CRC64NVME preferred)
  2. MD5 from x-amz-meta-s3cmd-attrs user metadata (HeadObject fallback)
  3. No checksum recorded — object will appear as unverifiable in comparison

Output CSV columns:
  bucket, key, size, checksum_algorithm, checksum_type, checksum_value, etag

Usage:
  pdc-build-checksum-manifest --bucket my-bucket --output manifest.csv

Optional:
  --prefix PDS4/
  --profile my-aws-profile
  --region us-west-2
  --resume-from manifest.csv   # resume an interrupted run (same file as --output)
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from typing import Dict, Iterable, Optional, Set, Tuple

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


def parse_args() -> argparse.Namespace:
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
    return parser.parse_args()


def build_session(profile: Optional[str], region: Optional[str]):
    session_kwargs = {}
    if profile:
        session_kwargs["profile_name"] = profile
    if region:
        session_kwargs["region_name"] = region
    return boto3.Session(**session_kwargs)


def load_existing_keys(csv_path: str) -> Set[str]:
    keys: Set[str] = set()
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            keys.add(row["key"])
    return keys


def choose_checksum(resp: Dict) -> Tuple[str, str, str]:
    """
    Prefer CRC64NVME if present. Otherwise take the first available checksum.
    Returns: (algorithm, checksum_type, checksum_value)
    """
    checksum = resp.get("Checksum", {}) or {}
    checksum_type = checksum.get("ChecksumType", "")

    for algo_name, field_name in CHECKSUM_FIELDS:
        value = checksum.get(field_name)
        if value:
            return algo_name, checksum_type, value

    return "", checksum_type, ""


def list_objects(s3_client, bucket: str, prefix: str) -> Iterable[Dict]:
    paginator = s3_client.get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix

    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            yield obj


def get_object_attrs_with_retry(s3_client, bucket: str, key: str, retries: int = 5) -> Dict:
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


def head_object_with_retry(s3_client, bucket: str, key: str, retries: int = 5) -> Dict:
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


def main() -> int:
    args = parse_args()

    session = build_session(args.profile, args.region)
    s3_client = session.client(
        "s3",
        config=Config(
            retries={"max_attempts": 10, "mode": "standard"},
        ),
    )

    processed_keys: Set[str] = set()
    write_header = True

    mode = "w"
    if args.resume_from:
        processed_keys = load_existing_keys(args.resume_from)
        mode = "a"
        write_header = False

    with open(args.output, mode, newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(CSV_HEADERS)

        count = 0
        for obj in list_objects(s3_client, args.bucket, args.prefix):
            key = obj["Key"]
            if key in processed_keys:
                continue

            try:
                attrs = get_object_attrs_with_retry(s3_client, args.bucket, key)
                algo, checksum_type, checksum_value = choose_checksum(attrs)
                size = attrs.get("ObjectSize", obj.get("Size", ""))
                etag = (attrs.get("ETag") or "").strip('"')

                if not checksum_value:
                    # No native S3 checksum — try x-amz-meta-s3cmd-attrs metadata.
                    head = head_object_with_retry(s3_client, args.bucket, key)
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
                size = obj.get("Size", "")
                etag = ""
                algo = ""
                checksum_type = ""
                checksum_value = f"ERROR:{err_code}"

            writer.writerow([
                args.bucket,
                key,
                size,
                algo,
                checksum_type,
                checksum_value,
                etag,
            ])
            count += 1

            if count % 1000 == 0:
                print(f"Processed {count} objects...", file=sys.stderr)

    print(f"Done. Wrote manifest to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
