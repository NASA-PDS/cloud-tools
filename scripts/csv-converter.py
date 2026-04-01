#!/usr/bin/env python3

import csv
import gzip
import glob
import os
from pathlib import Path

# Input: one or more raw S3 Inventory CSV.gz files
INPUT_GLOBS = [
    "/Users/jpadams/proj/pds/pdsen/workspace/cloud-tools/data/inventories/new_bucket_raw/**/*.csv.gz",
]

# Output: one normalized gzipped CSV
OUTPUT_FILE = "/Users/jpadams/proj/pds/pdsen/workspace/cloud-tools/data/inventories/new_bucket_processed/new_row_level_normalized.csv.gz"

HEADER = [
    "bucket",
    "key",
    "size",
    "checksum_algorithm",
    "checksum_type",
    "checksum_value",
    "etag",
]

def iter_input_files(patterns):
    for pattern in patterns:
        for path in glob.glob(pattern, recursive=True):
            yield path

def main():
    input_files = list(iter_input_files(INPUT_GLOBS))
    if not input_files:
        raise SystemExit("No input files found.")

    Path(os.path.dirname(OUTPUT_FILE) or ".").mkdir(parents=True, exist_ok=True)

    rows_written = 0

    with gzip.open(OUTPUT_FILE, "wt", newline="", encoding="utf-8") as out_f:
        writer = csv.writer(out_f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(HEADER)

        for input_file in input_files:
            with gzip.open(input_file, "rt", newline="", encoding="utf-8") as in_f:
                reader = csv.reader(in_f)

                for row in reader:
                    if not row:
                        continue

                    # Skip header rows if present
                    if row[0] == "Bucket" or row[0] == "bucket":
                        continue

                    # We only need these source positions from the raw inventory row:
                    # 0 = bucket
                    # 1 = key
                    # 2 = size
                    # 4 = e_tag
                    try:
                        bucket = row[0]
                        key = row[1]
                        size = row[2]
                        etag = row[4]
                    except IndexError:
                        raise ValueError(f"Unexpected row shape in {input_file}: {row}")

                    if not etag:
                        continue

                    writer.writerow([
                        bucket,
                        key,
                        size,
                        "etag",
                        "etag",
                        etag,
                        etag,
                    ])
                    rows_written += 1

    print(f"Wrote {rows_written} rows to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
