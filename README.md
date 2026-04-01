# Planetary Data Cloud Tools

Tools and utility scripts for accessing and managing Planetary Data Cloud (PDC) resources.


## User Quickstart

Install with:

    pip install pds.cloud-tools


## S3 Download (`pdc-s3-download`)

Downloads objects from an S3 bucket matching a given prefix to a local directory, preserving the key structure as a directory tree.

### Basic usage

```bash
pdc-s3-download \
  --source-profile my-aws-profile \
  --source-bucket my-bucket \
  --source-prefix path/to/objects/ \
  --local-dest-dir ./downloads
```

`--source-profile` and `--source-bucket` can also be supplied via the `AWS_PROFILE` and `AWS_BUCKET` environment variables.

### Filtering by last-modified time

Use `--start-datetime` and/or `--end-datetime` to restrict downloads to objects last modified within a time range. All datetimes are UTC. Accepted formats: `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`.

| Arguments provided | Behavior |
|--------------------|----------|
| `--start-datetime` only | Download objects modified from start to now |
| `--end-datetime` only | Download objects modified up to end |
| Both | Download objects modified within [start, end] |
| Neither | Download all matching objects |

```bash
# Download only objects modified in January 2025
pdc-s3-download \
  --source-profile my-aws-profile \
  --source-bucket my-bucket \
  --source-prefix path/to/objects/ \
  --local-dest-dir ./downloads \
  --start-datetime 2025-01-01 \
  --end-datetime 2025-02-01

# Download everything modified since a specific timestamp
pdc-s3-download \
  --source-profile my-aws-profile \
  --source-bucket my-bucket \
  --source-prefix path/to/objects/ \
  --local-dest-dir ./downloads \
  --start-datetime 2025-06-15T12:00:00
```


## Data Integrity Verification (S3 Bucket Migration)

Use these two tools to verify that every object in an OLD S3 bucket has a matching copy in a NEW bucket — by content (size + checksum), not just by key name.

### Overview

| Command | Purpose |
|---------|---------|
| `pdc-build-checksum-manifest` | Generate a CSV manifest of every object in a bucket with its checksum, size, and ETag |
| `pdc-compare-manifests` | Compare two manifests and report any OLD objects not represented in NEW |

### AWS Credentials Setup

The scripts use standard boto3 credential resolution. Options, in order of preference:

**Named profile** (recommended):

```ini
# ~/.aws/credentials
[old-account]
aws_access_key_id = AKIA...
aws_secret_access_key = ...

[new-account]
aws_access_key_id = AKIA...
aws_secret_access_key = ...
```

**IAM role assumption** (for cross-account access without long-lived keys):

```ini
# ~/.aws/config
[profile new-account]
role_arn = arn:aws:iam::123456789012:role/DataMigrationRole
source_profile = old-account
region = us-west-2
```

**Environment variables** (for CI/automation):

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...   # required when using temporary credentials
```

**Minimum required IAM permissions** for each bucket:

```json
{
  "Effect": "Allow",
  "Action": [
    "s3:ListBucket",
    "s3:GetObject",
    "s3:GetObjectAttributes"
  ],
  "Resource": [
    "arn:aws:s3:::your-bucket-name",
    "arn:aws:s3:::your-bucket-name/*"
  ]
}
```

### Step-by-Step Workflow

**Step 1 — Generate the OLD bucket manifest:**

```bash
pdc-build-checksum-manifest \
  --bucket old-bucket-name \
  --output old_manifest.csv \
  --profile old-account \
  --region us-west-2
```

**Step 2 — Generate the NEW bucket manifest:**

```bash
pdc-build-checksum-manifest \
  --bucket new-bucket-name \
  --output new_manifest.csv \
  --profile new-account \
  --region us-west-2
```

**Step 3 — Compare the manifests:**

```bash
pdc-compare-manifests \
  --old old_manifest.csv \
  --new new_manifest.csv \
  --missing-output missing_in_new.csv \
  --weak-output weak_checksums.csv
```

The comparison exits with code `0` (PASS) if every OLD object has a matching content signature in NEW, or `1` (FAIL) if any are missing.

Sample output:

```
=== S3 Manifest Comparison Summary ===
OLD objects total:               4823901
OLD objects covered by NEW:      4823901
OLD objects missing in NEW:      0
OLD objects with weak checksum:  142
Missing report:                  missing_in_new.csv
Weak rows report:                weak_checksums.csv

RESULT: PASS
Every OLD object has at least one matching content signature in NEW.
```

### Handling Buckets with Millions of Objects

Manifest generation uses the `list_objects_v2` paginator, which pages through arbitrarily large buckets automatically — there is no per-bucket size limit. Progress is printed to stderr every 1,000 objects.

The comparison step loads both manifests into memory simultaneously. For very large buckets (tens of millions of objects), ensure the host has sufficient RAM.

### Resuming After a Failure

If manifest generation is interrupted (timeout, credential expiry, network error), resume by passing the partial output file as `--resume-from`. Both `--output` and `--resume-from` must point to the same file:

```bash
# Initial run (interrupted):
pdc-build-checksum-manifest \
  --bucket old-bucket-name \
  --output old_manifest.csv \
  --profile old-account

# Resume run — pass the same file to both flags:
pdc-build-checksum-manifest \
  --bucket old-bucket-name \
  --output old_manifest.csv \
  --resume-from old_manifest.csv \
  --profile old-account
```

The resume run appends new rows to the existing CSV and skips any key already recorded in it.

### Understanding the Output Files

**`missing_in_new.csv`** — OLD objects not found in NEW, with a `reason` column:

| Reason | Meaning |
|--------|---------|
| `NO_MATCHING_SIGNATURE_IN_NEW` | Object content (size + checksum) not present in NEW |
| `NO_USABLE_CHECKSUM` | Object has no stored checksum; cannot verify by content signature |

**`weak_checksums.csv`** — rows from either manifest lacking a usable checksum. This occurs when objects were uploaded without `--checksum-algorithm`. These objects cannot be verified by content signature alone.

### Limitations

- Checksums are only present if they were stored at upload time. Objects copied with `aws s3 cp` without `--checksum-algorithm` will have no checksum and appear in the weak-checksum report.
- Matching is by content signature `(size, checksum_type, checksum_value)`, not by key. An object that was renamed or moved but has identical content will be counted as covered.


## Code of Conduct

All users and developers of the NASA-PDS software are expected to abide by our [Code of Conduct](https://github.com/NASA-PDS/.github/blob/main/CODE_OF_CONDUCT.md). Please read this to ensure you understand the expectations of our community.


## Development

To develop this project, use your favorite text editor, or an integrated development environment with Python support, such as [PyCharm](https://www.jetbrains.com/pycharm/).


### Contributing

For information on how to contribute to NASA-PDS codebases please take a look at our [Contributing guidelines](https://github.com/NASA-PDS/.github/blob/main/CONTRIBUTING.md).


### Installation

Install in editable mode and with extra developer dependencies into your virtual environment of choice:

    pip install --editable '.[dev]'

### Pre-commit
Configure the `pre-commit` hooks:

    pre-commit install
    pre-commit install -t pre-push
    pre-commit install -t prepare-commit-msg
    pre-commit install -t commit-msg

These hooks then will check for any future commits that might contain secrets. They also check code formatting, PEP8 compliance, type hints, etc.

👉 **Note:** A one time setup is required both to support `detect-secrets` and in your global Git configuration. See [the wiki entry on Secrets](https://github.com/NASA-PDS/nasa-pds.github.io/wiki/Git-and-Github-Guide#detect-secrets) to learn how.


### Packaging

To isolate and be able to re-produce the environment for this package, you should use a [Python Virtual Environment](https://docs.python.org/3/tutorial/venv.html). To do so, run:

    python -m venv venv

Then exclusively use `venv/bin/python`, `venv/bin/pip`, etc.

If you have `tox` installed and would like it to create your environment and install dependencies for you run:

    tox --devenv <name you'd like for env> -e dev

Dependencies for development are specified as the `dev` `extras_require` in `setup.cfg`; they are installed into the virtual environment as follows:

    pip install --editable '.[dev]'

All the source code is in a sub-directory under `src`.

You should update the `setup.cfg` file with:

- name of your module
- license, default apache, update if needed
- description
- download url, when you release your package on github add the url here
- keywords
- classifiers
- install_requires, add the dependencies of you package
- extras_require, add the development Dependencies of your package
- entry_points, when your package can be called in command line, this helps to deploy command lines entry points pointing to scripts in your package

For the packaging details, see https://packaging.python.org/tutorials/packaging-projects/ as a reference.


### Configuration

It is convenient to use ConfigParser package to manage configuration. It allows a default configuration which can be overwritten by the user in a specific file in their environment. See https://pymotw.com/2/ConfigParser/

For example:

    candidates = ['my_pds_module.ini', 'my_pds_module.ini.default']
    found = parser.read(candidates)


### Logs

You should not use `print()`vin the purpose of logging information on the execution of your code. Depending on where the code runs these information could be redirected to specific log files.

To make that work, start each Python file with:

```python
"""My module."""
import logging

logger = logging.getLogger(__name__)
```

To log a message:

    logger.info("my message")

In your `main` routine, include:

    logging.basicConfig(level=logging.INFO)

to get a basic logging system configured.


### Tooling

The `dev` `extras_require` included in the template repo installs `black`, `flake8` (plus some plugins), and `mypy` along with default configuration for all of them. You can run all of these (and more!) with:

    tox -e lint


### Code Style

So that your code is readable, you should comply with the [PEP8 style guide](https://www.python.org/dev/peps/pep-0008/). Our code style is automatically enforced in via [black](https://pypi.org/project/black/) and [flake8](https://flake8.pycqa.org/en/latest/). See the [Tooling section](#-tooling) for information on invoking the linting pipeline.

❗Important note for template users❗
The included [pre-commit configuration file](.pre-commit-config.yaml) executes `flake8` (along with `mypy`) across the entire `src` folder and not only on changed files. If you're converting a pre-existing code base over to this template that may result in a lot of errors that you aren't ready to deal with.

You can instead execute `flake8` only over a diff of the current changes being made by modifying the `pre-commit` `entry` line:

    entry: git diff -u | flake8 --diff

Or you can change the `pre-commit` config so `flake8` is only called on changed files which match a certain filtering criteria:

    -   repo: local
        hooks:
        -   id: flake8
            name: flake8
            entry: flake8
            files: ^src/|tests/
            language: system


### Recommended Libraries

Python offers a large variety of libraries. In PDS scope, for the most current usage we should use:

| Library      | Usage                                           |
|--------------|------------------------------------------------ |
| configparser | manage and parse configuration files            |
| argparse     | command line argument documentation and parsing |
| requests     | interact with web APIs                          |
| lxml         | read/write XML files                            |
| json         | read/write JSON files                           |
| pyyaml       | read/write YAML files                           |
| pystache     | generate files from templates                   |

Some of these are built into Python 3; others are open source add-ons you can include in your `requirements.txt`.


### Tests

This section describes testing for your package.

A complete "build" including test execution, linting (`mypy`, `black`, `flake8`, etc.), and documentation build is executed via:

    tox


#### Unit tests

Your project should have built-in unit tests, functional, validation, acceptance, etc., tests.

For unit testing, check out the [unittest](https://docs.python.org/3/library/unittest.html) module, built into Python 3.

Tests objects should be in packages `test` modules or preferably in project 'tests' directory which mirrors the project package structure.

Our unit tests are launched with command:

    pytest

If you want your tests to run automatically as you make changes start up `pytest` in watch mode with:

    ptw


#### Integration/Behavioral Tests

One should use the `behave package` and push the test results to "testrail".

See an example in https://github.com/NASA-PDS/pds-doi-service#behavioral-testing-for-integration--testing


### Documentation

Your project should use [Sphinx](https://www.sphinx-doc.org/en/master/) to build its documentation. PDS' documentation template is already configured as part of the default build. You can build your projects docs with:

    python setup.py build_sphinx

You can access the build files in the following directory relative to the project root:

    build/sphinx/html/


## Build

    pip install wheel
    python setup.py sdist bdist_wheel


## Publication

NASA PDS packages can publish automatically using the [Roundup Action](https://github.com/NASA-PDS/roundup-action), which leverages GitHub Actions to perform automated continuous integration and continuous delivery. A default workflow that includes the Roundup is provided in the `.github/workflows/unstable-cicd.yaml` file. (Unstable here means an interim release.)


### Manual Publication

Create the package:

    python setup.py bdist_wheel

Publish it as a Github release.

Publish on PyPI (you need a PyPI account and configure `$HOME/.pypirc`):

    pip install twine
    twine upload dist/*

Or publish on the Test PyPI (you need a Test PyPI account and configure `$HOME/.pypirc`):

    pip install twine
    twine upload --repository testpypi dist/*

## CI/CD

The template repository comes with our two "standard" CI/CD workflows, `stable-cicd` and `unstable-cicd`. The unstable build runs on any push to `main` (± ignoring changes to specific files) and the stable build runs on push of a release branch of the form `release/<release version>`. Both of these make use of our GitHub actions build step, [Roundup](https://github.com/NASA-PDS/roundup-action). The `unstable-cicd` will generate (and constantly update) a SNAPSHOT release. If you haven't done a formal software release you will end up with a `v0.0.0-SNAPSHOT` release (see NASA-PDS/roundup-action#56 for specifics).
