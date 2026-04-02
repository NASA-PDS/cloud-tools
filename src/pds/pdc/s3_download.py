#!/usr/bin/env python
r"""Download files from an S3 bucket to a local directory.

This script uses the boto3 library to download objects from a specified S3
bucket that match a given prefix. Objects can optionally be filtered by their
last-modified timestamp. The AWS profile, S3 bucket name, prefix, and local
destination directory are provided as command-line arguments or through the
AWS_PROFILE and AWS_BUCKET environment variables.

Usage:
    pdc-s3-download --source-prefix prefix/path/to/objects/ --local-dest-dir local/path/
    pdc-s3-download --source-prefix prefix/ --local-dest-dir local/ --start-datetime 2025-01-01
    pdc-s3-download --source-prefix prefix/ --local-dest-dir local/ \\
        --start-datetime 2025-01-01T00:00:00 --end-datetime 2025-06-01T00:00:00

    All datetimes are interpreted as UTC. Accepted formats: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS.

    Time range behavior:
        --start-datetime only  → download objects modified from start to now
        --end-datetime only    → download objects modified up to end
        both provided          → download objects modified within [start, end]
        neither provided       → download all objects (no time filter)

    (Optionally set AWS_PROFILE and AWS_BUCKET in your environment)
"""
import argparse
import os
from datetime import datetime
from datetime import timezone

import boto3
from botocore.exceptions import ClientError


def parse_datetime(value):
    """Parse a datetime string in ISO 8601 format, returning an aware UTC datetime."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Invalid datetime format: '{value}'. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS.")


def main():
    """Parse command-line arguments and download S3 objects to a local directory."""
    # Get defaults from environment variables if available
    default_source_profile = os.environ.get("AWS_PROFILE")
    default_source_bucket = os.environ.get("AWS_BUCKET")

    parser = argparse.ArgumentParser(
        description="Download files from an S3 bucket (filtered by a prefix) to a local directory."
    )
    parser.add_argument(
        "--source-profile",
        default=default_source_profile,
        help=("AWS profile name for the source bucket (defaults to AWS_PROFILE environment variable)"),
    )
    parser.add_argument(
        "--source-bucket",
        default=default_source_bucket,
        help=("Name of the source S3 bucket (defaults to AWS_BUCKET environment variable)"),
    )
    parser.add_argument("--source-prefix", required=True, help="Prefix in the source bucket to filter objects")
    parser.add_argument("--local-dest-dir", required=True, help="Local directory where files will be downloaded")
    parser.add_argument(
        "--start-datetime",
        type=parse_datetime,
        default=None,
        metavar="DATETIME",
        help="Only download objects last modified at or after this datetime (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS, UTC)",
    )
    parser.add_argument(
        "--end-datetime",
        type=parse_datetime,
        default=None,
        metavar="DATETIME",
        help=(
            "Only download objects last modified before or at this datetime "
            "(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS, UTC). "
            "Defaults to now if --start-datetime is provided."
        ),
    )

    args = parser.parse_args()

    # Validate that required AWS parameters are provided either via command-line or env variables
    if not args.source_profile:
        parser.error("The --source-profile argument is required or set the AWS_PROFILE environment variable.")
    if not args.source_bucket:
        parser.error("The --source-bucket argument is required or set the AWS_BUCKET environment variable.")

    source_profile = args.source_profile
    source_bucket_name = args.source_bucket
    source_prefix = args.source_prefix
    local_dest_dir = args.local_dest_dir

    start_dt = args.start_datetime
    end_dt = args.end_datetime
    if start_dt and not end_dt:
        end_dt = datetime.now(tz=timezone.utc)

    # Create a session for the source AWS profile and initialize S3 resource
    source_session = boto3.Session(profile_name=source_profile)
    source_s3 = source_session.resource("s3")
    source_bucket = source_s3.Bucket(source_bucket_name)

    # Ensure the local destination directory exists
    if not os.path.exists(local_dest_dir):
        os.makedirs(local_dest_dir)

    # Loop through objects with the specified prefix in the source bucket
    for obj in source_bucket.objects.filter(Prefix=source_prefix):
        # Remove the source prefix from the object's key to construct a relative path
        relative_path = obj.key[len(source_prefix) :].lstrip("/")

        # Skip directory markers or keys that resolve to an empty relative path
        if not relative_path or obj.key.endswith("/"):
            print(f"Skipping directory marker or empty key: {obj.key}")
            continue

        # Filter by last modified time range if specified
        last_modified = obj.last_modified
        if start_dt and last_modified < start_dt:
            print(f"Skipping {obj.key} (last modified {last_modified} is before {start_dt})")
            continue
        if end_dt and last_modified > end_dt:
            print(f"Skipping {obj.key} (last modified {last_modified} is after {end_dt})")
            continue

        # Construct the local file path
        local_file_path = os.path.join(local_dest_dir, relative_path)

        # Ensure the directory structure exists locally
        local_dir = os.path.dirname(local_file_path)
        if not os.path.exists(local_dir):
            os.makedirs(local_dir)

        print(f"Downloading {obj.key} to {local_file_path}...")
        try:
            source_bucket.download_file(obj.key, local_file_path)
        except ClientError as e:
            print(f"Error downloading {obj.key}: {e}")

    print("Download complete!")


if __name__ == "__main__":
    main()
