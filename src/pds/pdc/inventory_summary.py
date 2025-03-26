import os
import sys
import gzip
import csv


def process_inventory_file(file_path):
    """
    Process a single gzipped CSV inventory file.
    Assumes the object size is in the third column (index 2).

    Returns:
        count (int): Number of objects in this file.
        total_size (int): Sum of object sizes in this file.
    """
    count = 0
    total_size = 0

    try:
        with gzip.open(file_path, mode='rt', newline='') as gz_file:
            reader = csv.reader(gz_file)
            for row in reader:
                if len(row) < 3:
                    continue  # skip if row doesn't have enough columns
                try:
                    size = int(row[2])
                except ValueError:
                    # If conversion fails, skip this row.
                    continue
                total_size += size
                count += 1
    except Exception as e:
        print(f"Error processing file {file_path}: {e}")

    return count, total_size


def process_inventory_directory(directory):
    """
    Process all gzipped CSV inventory files in a directory.

    Returns:
        total_count (int): Total number of objects across all files.
        overall_size (int): Cumulative object size across all files.
    """
    total_count = 0
    overall_size = 0

    # Walk through directory to find all .gz files
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(".gz"):
                file_path = os.path.join(root, file)
                print(f"Processing: {file_path}")
                count, size = process_inventory_file(file_path)
                total_count += count
                overall_size += size

    return total_count, overall_size


def main():
    if len(sys.argv) != 2:
        print("Usage: python inventory_process.py <directory_path>")
        sys.exit(1)

    directory_path = sys.argv[1]

    if not os.path.isdir(directory_path):
        print(f"Error: {directory_path} is not a valid directory.")
        sys.exit(1)

    total_count, overall_size = process_inventory_directory(directory_path)

    print("\nFinal Inventory Summary:")
    print(f"Total objects: {total_count}")
    print(f"Total object size: {overall_size} bytes")


if __name__ == "__main__":
    main()