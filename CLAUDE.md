# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`pds.pdc-cloud-tools` provides CLI tools and utilities for accessing and managing Planetary Data Cloud (PDC) resources on AWS S3, part of the NASA Planetary Data System (PDS) ecosystem. The package uses Python namespace packages under `pds.pdc`.

## Development Setup

```bash
pip install --editable '.[dev]'
pre-commit install
pre-commit install -t pre-push
pre-commit install -t prepare-commit-msg
pre-commit install -t commit-msg
```

## Commands

### Testing
```bash
pytest                          # run all tests
pytest tests/path/to/test.py    # run single test file
ptw                             # watch mode
tox -e py39                     # run tests via tox (parallel with coverage)
```

### Linting
```bash
tox -e lint                     # run all linters via pre-commit
pre-commit run --all            # run pre-commit hooks directly
```

### Build
```bash
python setup.py sdist bdist_wheel
```

## Code Style

- Line length: 120 characters (Black + Flake8)
- Docstrings: Google style (enforced by pydocstyle via flake8-docstrings)
- Import order: enforced by `reorder-python-imports` pre-commit hook
- Type annotations: mypy runs on `src/` at pre-commit time

## Architecture

The source lives under `src/pds/pdc/` using PDS namespace packaging. CLI entry points are registered in `setup.cfg`:

- **`pdc-s3-download`** (`s3_download.py`) — Downloads S3 objects filtered by prefix, preserving directory structure. Accepts `--source-profile`, `--source-bucket`, `--source-prefix`, `--local-dest-dir` (also reads `AWS_PROFILE` and `AWS_BUCKET` env vars). Optional `--start-datetime` and `--end-datetime` (UTC, `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`) filter by last-modified time.

- **`pdc-inventory-summary`** (`inventory_summary.py`) — Summarizes S3 inventory files (gzipped CSV). Reads all `.gz` files in a directory tree and reports total object count and cumulative size. Assumes object size is in CSV column index 2.

- **`data_integrity/`** — Data integrity verification for cloud migrations:
  - `build_s3_checksum_manifest.py` — Generates a CSV manifest of S3 objects with checksums (prefers CRC64NVME, falls back to others). Supports `--resume-from` for incremental runs. Outputs: `bucket, key, size, checksum_algorithm, checksum_type, checksum_value, etag`.
  - `compare_s3_manifests.py` — Compares two manifests to verify every object in OLD is present in NEW. Uses a tiered matching strategy (see below). Returns exit code 1 if any OLD objects are unmatched with a stable checksum.

    **Matching tiers (in priority order):**
    1. `CHECKSUM_VERIFIED` — primary match on `(size, checksum_type, checksum_value)`; cryptographic content proof.
    2. `ETAG_VERIFIED` — cross-type match: OLD object has S3CMD-MD5 metadata checksum, matched against NEW object's ETag. Valid only for single-part uploads where ETag == content MD5.
    3. `SIZE_CONFIRMED` — weak fallback for multipart-ETag objects where no stable checksum exists for cross-migration comparison. Matches on exact filename (basename of S3 key) + byte-count. Used when: (a) OLD `checksum_value` is a multipart ETag (`<hex>-<N>`), or (b) OLD has an S3CMD-MD5 checksum but its ETag is multipart, meaning the MD5 was likely not re-attached during server-side copy and won't appear in NEW's manifest. Identical filename + size for large objects is strong circumstantial evidence of a successful copy, but is not a cryptographic integrity guarantee.

    **Why multipart ETags break checksum comparison:** AWS computes multipart ETags as a hash-of-part-hashes suffixed with the part count. If an object is re-uploaded or server-side copied with different part boundaries, the ETag changes even if the bytes are identical. This makes them useless as cross-migration identifiers.

    **Output files:** `matched_objects.csv` (with `match_type` column), `missing_in_new.csv`, `unverifiable.csv` (with `reason` column: `no_checksum` or `multipart_etag`), `weak_checksum_rows.csv`.

## CI/CD

GitHub Actions workflows in `.github/workflows/`:
- `branch-cicd.yaml` — runs on feature branches
- `unstable-cicd.yaml` — runs on `main` pushes, publishes SNAPSHOT to Test PyPI via NASA-PDS Roundup Action
- `stable-cicd.yaml` — runs on `release/*` branches for stable releases
- `terraform_cicd.yaml` — infrastructure deployment

Secrets required by CI: `ADMIN_GITHUB_TOKEN`, `TEST_PYPI_USERNAME`, `TEST_PYPI_PASSWORD`.
