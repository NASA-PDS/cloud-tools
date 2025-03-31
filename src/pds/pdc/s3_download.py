#!/usr/bin/env python
"""Download files from an S3 bucket to a local directory.

This script uses the boto3 library to download objects from a specified S3
bucket that match a given prefix. The AWS profile, S3 bucket name, prefix,
and local destination directory are provided as command-line arguments or
through the AWS_PROFILE and AWS_BUCKET environment variables.

Usage:
    python s3_download.py --source-prefix prefix/path/to/objects/ --local-dest-dir local/path/
    (Optionally set AWS_PROFILE and AWS_BUCKET in your environment)
"""
import argparse
import os

import boto3
from botocore.exceptions import ClientError


def main():
    """Parse command-line arguments and download S3 objects to a local directory."""
    # Get defaults from environment variables if available
    default_source_profile = os.environ.get("AWS_PROFILE")
    default_source_bucket = os.environ.get("AWS_BUCKET")

    parser = argparse.ArgumentParser(
        description="Download files from an S3 bucket (filtered by a prefix) " "to a local directory."
    )
    parser.add_argument(
        "--source-profile",
        default=default_source_profile,
        help=("AWS profile name for the source bucket " "(defaults to AWS_PROFILE environment variable)"),
    )
    parser.add_argument(
        "--source-bucket",
        default=default_source_bucket,
        help=("Name of the source S3 bucket " "(defaults to AWS_BUCKET environment variable)"),
    )
    parser.add_argument("--source-prefix", required=True, help="Prefix in the source bucket to filter objects")
    parser.add_argument("--local-dest-dir", required=True, help="Local directory where files will be downloaded")

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
        relative_path = obj.key[len(source_prefix) :]

        # Skip directory markers or keys that resolve to an empty relative path
        if not relative_path or obj.key.endswith("/"):
            print(f"Skipping directory marker or empty key: {obj.key}")
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
