"""S3 helpers shared by the migration tools: clients, resumable batch processing and outage handling."""
from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from typing import Callable
from typing import List

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError

from pds.pdc.migration.errors import MigrationStoppedError

log = logging.getLogger(__name__)

BOTO_CONFIG = Config(retries={"max_attempts": 10, "mode": "standard"}, max_pool_connections=128)

# Network drops (VPN) raise BotoCoreError subclasses; HTTP-level problems raise ClientError.
AWS_ERRORS = (ClientError, BotoCoreError)

# Stop after this many failures in a row (expired credentials, VPN down, a permission change).
MAX_CONSECUTIVE_FAILURES = 500
# Objects processed (and saved) per batch.
BATCH_SIZE = 5000
# After an outage is detected, probe this often for up to this long before stopping (resumable either way).
RECOVERY_PROBE_SECONDS = 30
RECOVERY_WAIT_SECONDS = 900
# S3 throttling ("SlowDown") is retried with exponential backoff before the run stops.
THROTTLE_CODES = ("SlowDown", "503", "ServiceUnavailable", "RequestLimitExceeded")
THROTTLE_RETRIES = 8
THROTTLE_BASE_DELAY_SECONDS = 5
THROTTLE_MAX_DELAY_SECONDS = 120

_thread_local = threading.local()


def s3_client(session: boto3.Session):
    """Return a thread-local S3 client."""
    if not hasattr(_thread_local, "s3"):
        _thread_local.s3 = session.client("s3", config=BOTO_CONFIG)
    return _thread_local.s3


def is_throttle(error: Exception) -> bool:
    """Return True if *error* is S3 asking the client to slow down."""
    return isinstance(error, ClientError) and error.response.get("Error", {}).get("Code") in THROTTLE_CODES


def resumable(action: Callable[[], object]):
    """Run *action*, backing off on throttling and turning other network and credential failures into a clean stop.

    Raises:
        MigrationStoppedError: if the action fails with an AWS or network error, or is still throttled after
            ``THROTTLE_RETRIES`` attempts.
    """
    attempt = 0
    while True:
        try:
            return action()
        except AWS_ERRORS as e:
            if not is_throttle(e) or attempt >= THROTTLE_RETRIES:
                raise MigrationStoppedError(
                    f"AWS request failed ({type(e).__name__}: {e}). Check the network/VPN and credentials, then "
                    f"rerun the same command to resume where it stopped."
                ) from e
            delay = min(THROTTLE_MAX_DELAY_SECONDS, THROTTLE_BASE_DELAY_SECONDS * 2**attempt)
            attempt += 1
            log.warning("S3 asked to slow down; retrying in %ds (attempt %d of %d)", delay, attempt, THROTTLE_RETRIES)
            time.sleep(delay)


def wait_for_recovery(probe: Callable[[], tuple], error: object) -> None:
    """Pause and probe until requests succeed again, riding out a short network or VPN outage.

    Args:
        probe: Issues one request and returns its result tuple; a failure has an AWS error in position 1.
        error: The error that triggered the wait, for the log and the final message.

    Raises:
        MigrationStoppedError: if requests are still failing after ``RECOVERY_WAIT_SECONDS``.
    """
    waited = 0
    while waited < RECOVERY_WAIT_SECONDS:
        log.warning(
            "Requests are failing (%s). Check the VPN/network and credentials; retrying in %ds (waited %ds of %ds)",
            error,
            RECOVERY_PROBE_SECONDS,
            waited,
            RECOVERY_WAIT_SECONDS,
        )
        time.sleep(RECOVERY_PROBE_SECONDS)
        waited += RECOVERY_PROBE_SECONDS
        if not isinstance(probe()[1], AWS_ERRORS):
            log.info("Requests are succeeding again; resuming.")
            return
    raise MigrationStoppedError(
        f"Requests kept failing for {RECOVERY_WAIT_SECONDS}s (last: {error}). Check the network/VPN and credentials, "
        f"then rerun the same command to resume where it stopped."
    )


def run_batches(
    workers: int,
    next_batch: Callable[[str, int], List[str]],
    work: Callable[[str], tuple],
    save: Callable[[List[tuple]], None],
    total: int,
    what: str,
) -> None:
    """Process pending keys in bounded batches, saving after each batch, and ride out short outages.

    Args:
        workers: Parallel request threads.
        next_batch: Returns up to N pending keys after a given key, in key order.
        work: Issues one request; returns ``(key, *payload)`` or ``(key, <AWS error>)`` on failure.
        save: Stores a list of successful results.
        total: Pending count at the start, for progress messages.
        what: Short description for log messages.

    Raises:
        MigrationStoppedError: if requests keep failing (network, credentials) after progress has been saved.
    """
    after = ""
    done = failures = consecutive = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            keys = next_batch(after, BATCH_SIZE)
            if not keys:
                break
            after = keys[-1]
            good: List[tuple] = []
            outage = None
            for result in pool.map(work, keys):
                if isinstance(result[1], AWS_ERRORS):
                    failures += 1
                    consecutive += 1
                    if failures <= 20:
                        log.warning("%s failed for %s: %s", what, result[0], result[1])
                    if consecutive >= MAX_CONSECUTIVE_FAILURES:
                        outage = result
                        break
                else:
                    consecutive = 0
                    good.append(result)
            save(good)
            if outage is not None:
                wait_for_recovery(lambda key=outage[0]: work(key), outage[1])
                consecutive = 0
                after = ""  # go back for the keys that failed during the outage
                continue
            previous, done = done, done + len(good)
            if done // 50000 != previous // 50000:
                log.info("    %s progress: %d/%d (%d failed so far)", what, done, total, failures)
    if failures:
        log.warning("  %d %s requests failed; rerun the same command to retry what is left", failures, what)
