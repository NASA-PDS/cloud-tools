#!/usr/bin/env python3
"""Transition S3 objects from STANDARD storage to Glacier Flexible Retrieval.

Lists all objects in a bucket (optionally filtered by prefix) whose storage
class is STANDARD and copies them in-place with StorageClass=GLACIER.

Note: S3 copy_object has a 5 GiB single-operation limit. Objects larger than
5 GiB are logged and skipped — use an S3 Lifecycle rule for those.

Usage:
  pdc-transition-to-glacier --bucket my-bucket

Optional:
  --prefix PDS4/
  --profile my-aws-profile
  --region us-west-2
  --dry-run            Print what would be transitioned without making changes
  --max-objects 100    Stop after N objects (smoke test)
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Dict
from typing import Iterable
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


# 5 GiB — hard limit for single-operation CopyObject
_COPY_OBJECT_MAX_BYTES = 5 * 1024 * 1024 * 1024


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Transition STANDARD objects in an S3 bucket to Glacier Flexible Retrieval."
    )
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument("--prefix", default="", help="Optional key prefix filter")
    parser.add_argument("--profile", default=None, help="AWS profile name")
    parser.add_argument("--region", default=None, help="AWS region")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List objects that would be transitioned without making any changes",
    )
    parser.add_argument(
        "--max-objects",
        type=int,
        default=None,
        help="Stop after transitioning this many objects (useful for smoke tests)",
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


def list_standard_objects(s3_client, bucket: str, prefix: str) -> Iterable[Dict]:
    """Yield objects in STANDARD storage class, paginating over the full bucket."""
    paginator = s3_client.get_paginator("list_objects_v2")
    kwargs: Dict = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix

    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            if obj.get("StorageClass", "STANDARD") == "STANDARD":
                yield obj


def copy_to_glacier_with_retry(s3_client, bucket: str, key: str, retries: int = 5) -> None:
    """Copy an object to itself with StorageClass=GLACIER, with exponential-backoff retry."""
    delay = 1.0
    for attempt in range(retries):
        try:
            s3_client.copy_object(
                Bucket=bucket,
                Key=key,
                CopySource={"Bucket": bucket, "Key": key},
                StorageClass="GLACIER",
                MetadataDirective="COPY",
                TaggingDirective="COPY",
            )
            return
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in {"SlowDown", "Throttling", "RequestTimeout", "InternalError"} and attempt < retries - 1:
                print(f"  Throttled on {key} (attempt {attempt + 1}), retrying in {delay:.0f}s...", file=sys.stderr)
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError(f"Exhausted {retries} retries for CopyObject on {key}")


def main() -> int:
    """Transition all STANDARD objects in the bucket to Glacier Flexible Retrieval."""
    args = parse_args()

    profile_str = args.profile or "default"
    region_str = args.region or "default"
    dry_run_str = " [DRY RUN]" if args.dry_run else ""
    print(
        f"Building session (profile={profile_str}, region={region_str}){dry_run_str}...",
        file=sys.stderr,
    )
    session = build_session(args.profile, args.region)
    s3_client = session.client(
        "s3",
        config=Config(retries={"max_attempts": 10, "mode": "standard"}),
    )

    prefix_str = args.prefix or "(none)"
    print(
        f"Listing STANDARD objects in s3://{args.bucket} prefix={prefix_str} ...",
        file=sys.stderr,
    )

    transitioned = 0
    skipped_large = 0
    errors = 0

    for obj in list_standard_objects(s3_client, args.bucket, args.prefix):
        key = obj["Key"]
        size = obj.get("Size", 0)

        if size > _COPY_OBJECT_MAX_BYTES:
            size_gib = size / (1024 ** 3)
            print(
                f"  SKIP (too large for single copy — {size_gib:.1f} GiB): {key}",
                file=sys.stderr,
            )
            skipped_large += 1
            continue

        if args.dry_run:
            print(f"  [DRY RUN] would transition: {key} ({size:,} bytes)", file=sys.stderr)
        else:
            try:
                copy_to_glacier_with_retry(s3_client, args.bucket, key)
            except ClientError as e:
                err_code = e.response.get("Error", {}).get("Code", "UNKNOWN")
                print(f"  ERROR transitioning {key}: {err_code}", file=sys.stderr)
                errors += 1
                continue

        transitioned += 1

        if transitioned in {1, 10, 100} or transitioned % 1000 == 0:
            print(f"Transitioned {transitioned} objects so far...", file=sys.stderr)

        if args.max_objects and transitioned >= args.max_objects:
            print(f"Reached --max-objects limit of {args.max_objects}. Stopping.", file=sys.stderr)
            break

    action = "Would transition" if args.dry_run else "Transitioned"
    print(
        f"\nDone.{dry_run_str} {action} {transitioned} objects to Glacier Flexible Retrieval.",
        file=sys.stderr,
    )
    if skipped_large:
        print(
            f"Skipped {skipped_large} objects > 5 GiB (use an S3 Lifecycle rule for those).",
            file=sys.stderr,
        )
    if errors:
        print(f"Encountered {errors} errors (see above).", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
