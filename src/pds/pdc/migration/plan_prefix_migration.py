#!/usr/bin/env python3
r"""Plan the PDS4/ -> pds4/ rename and family consolidation.  READ-ONLY.

This tool only lists and HEADs objects; it never writes, copies or deletes in S3.  It writes a plan CSV
that says, for every source object, what a later step would do with it:

  MOVE        copy to pds4/<family>/<rel> (the newest version of a key, or an object with no collision)
  RENAME      copy an older version to pds4/<family>/<dir>/<stem>_v<version_id><ext>, beside the newest
              version (version_id is the PDS4 version_id of the older object's label, e.g. f_v1.0.XML)
  SUPERSEDE   copy to pds4/superseded/<top>/<rel>: an older version with no label or no version_id (most likely
              removed or overwritten), or one whose renamed key is unavailable
  DROP        provably identical to a kept object: delete the source, do not copy
  GARBAGE     copy to potential-garbage/<top>/<rel> for review: the garbage directories, and older versions
              that are in-place updates of a newer one
  JUNK        .DS_Store, ._* and .*.swp files: delete the source, do not copy
  REVIEW      colliding objects whose ``mtime`` is missing; left untouched and written to the review CSV

Older versions with different content are classified by the PDS4 ``<version_id>`` in the object's label: the
object itself for ``.xml``; the same-named ``.xml`` for a data file; otherwise a label in the same directory
whose ``<file_name>`` names the file.  The same ``version_id`` as the newest version means an in-place update
(GARBAGE), a different one is a real earlier version (RENAME), and no label or no ``version_id`` means the
file was most likely removed or overwritten (SUPERSEDE, so a copy is kept).

The newest version of a colliding key is chosen by the newest ``mtime`` stored in the ``s3cmd-attrs`` user
metadata (``--winner-rule mtime``, the default) or by the highest prefix number (``--winner-rule
prefix-number``, which needs no HEADs).  S3 LastModified is never used.  Two objects count as identical only
when sizes match and every hash agrees: the ETag of a single-part object, an s3cmd md5 or a CRC64NVME checksum.

Efficiency and resuming
-----------------------
Working state lives in a SQLite database (``<output>.db`` by default), so large families do not have to fit
in memory and any interruption (VPN drop, expired credentials, Ctrl-C) can be resumed by rerunning the same
command:

  * Listing is split into many small sub-prefixes that are listed in parallel.  Each page of results and the
    position within its sub-prefix are saved together, so a half-listed sub-prefix continues where it stopped.
  * Only objects that share a destination key need a HEAD for ``mtime``, and groups whose objects are all
    identical single-part objects need none.  HEADs run in bounded batches and are saved per batch.
  * Many consecutive failures stop the run cleanly instead of burning through requests.

Usage
-----
  pdc-plan-prefix-migration \
    --bucket my-bucket \
    --profile my-aws-profile \
    --family LADEE

Optional
--------
  --all                  Plan every prefix under PDS4/ (instead of --family)
  --exclude-family NAME  Leave a family out of an --all run (repeatable), for example one already migrated
  --winner-rule RULE     mtime | prefix-number (default: mtime)
  --output FILE          Plan CSV to write (default: migration_plan.csv)
  --review-output FILE   CSV of objects held out for review (default: <output>_review.csv)
  --db FILE              SQLite working state (default: <output>.db)
  --split-depth N        Directory levels below each prefix that are split for parallel listing (default: 3)
  --list-workers N       Parallel listing threads (default: 16)
  --workers N            Parallel HEAD threads (default: 64)
  --show-collisions N    Print N example collision groups (default: 20)
  --region REGION        AWS region (default: us-west-2)
  --log-level LEVEL      DEBUG | INFO | WARNING | ERROR (default: INFO)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import itertools
import logging
import re
import sqlite3
import sys
import threading
from collections import Counter
from collections import defaultdict
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import fields
from typing import Callable
from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple

import boto3
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError

from pds.pdc.migration.errors import MigrationPlanError
from pds.pdc.migration.s3_common import AWS_ERRORS
from pds.pdc.migration.s3_common import BATCH_SIZE
from pds.pdc.migration.s3_common import resumable
from pds.pdc.migration.s3_common import run_batches
from pds.pdc.migration.s3_common import s3_client
from pds.pdc.migration.rules import classify_prefix
from pds.pdc.migration.rules import destination_key
from pds.pdc.migration.rules import garbage_key
from pds.pdc.migration.rules import is_garbage
from pds.pdc.migration.rules import is_junk
from pds.pdc.migration.rules import Membership
from pds.pdc.migration.rules import SOURCE_ROOT
from pds.pdc.migration.rules import split_source_key
from pds.pdc.migration.rules import superseded_key
from pds.pdc.migration.rules import versioned_key

log = logging.getLogger(__name__)

MOVE = "MOVE"
RENAME = "RENAME"
SUPERSEDE = "SUPERSEDE"
DROP = "DROP"
GARBAGE = "GARBAGE"
JUNK = "JUNK"
REVIEW = "REVIEW"
MERGE = "MERGE"  # internal category: object takes part in family consolidation

WINNER_MTIME = "mtime"
WINNER_PREFIX = "prefix-number"

# Bytes of a label fetched to read its <version_id>; a larger read is tried if the tag is not found.
LABEL_RANGE_BYTES = 8192
LABEL_RETRY_BYTES = 65536
# Labels that are searched for the data files they describe (<file_name>) are read this far.
LABEL_FILES_BYTES = 262144

_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    key TEXT PRIMARY KEY,
    top TEXT NOT NULL,
    rel TEXT NOT NULL,
    size INTEGER NOT NULL,
    etag TEXT NOT NULL,
    last_modified TEXT NOT NULL,
    category TEXT NOT NULL,
    dst TEXT NOT NULL,
    mtime INTEGER,
    md5 TEXT,
    crc64nvme TEXT,
    head_done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS objects_dst ON objects (dst);
CREATE INDEX IF NOT EXISTS objects_content ON objects (size, etag);
CREATE TABLE IF NOT EXISTS listed_units (unit TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS unit_progress (unit TEXT PRIMARY KEY, last_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS label_versions (
    label_key TEXT PRIMARY KEY,
    version_id TEXT,
    done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS label_files (
    label_dir TEXT NOT NULL,
    file_name TEXT NOT NULL,
    label_key TEXT NOT NULL,
    PRIMARY KEY (label_dir, file_name, label_key)
);
"""


@dataclass
class SourceObject:
    """One object under PDS4/ plus what the planner learns about it."""

    key: str
    top: str
    rel: str
    size: int
    etag: str
    last_modified: str
    order: float
    mtime: Optional[int] = None
    md5: Optional[str] = None
    crc64nvme: Optional[str] = None


@dataclass
class PlanRow:
    """One line of the plan CSV."""

    action: str
    family: str
    src_key: str
    dst_key: str
    size: int
    etag: str
    last_modified: str
    mtime: str
    reason: str
    reference_key: str
    version_id: str = ""


# ---------------------------------------------------------------------------
# Pure planning logic
# ---------------------------------------------------------------------------

# The product's own <version_id> is the first one inside <Identification_Area>; later ones (for example in
# Modification_History) describe earlier changes.
_VERSION_ID_RE = re.compile(
    r"<(?:[\w.-]+:)?Identification_Area\b.*?<(?:[\w.-]+:)?version_id>\s*([^<]*?)\s*</(?:[\w.-]+:)?version_id>",
    re.DOTALL,
)


def parse_version_id(text: str) -> Optional[str]:
    """Return the product ``version_id`` from the start of a PDS4 label, or None if it is not there."""
    match = _VERSION_ID_RE.search(text)
    return match.group(1) or None if match else None


_FILE_NAME_RE = re.compile(r"<(?:[\w.-]+:)?file_name>\s*([^<]*?)\s*</(?:[\w.-]+:)?file_name>")


def parse_file_names(text: str) -> List[str]:
    """Return the data file names a PDS4 label lists in its ``<file_name>`` elements, without duplicates."""
    return list(dict.fromkeys(name for name in _FILE_NAME_RE.findall(text) if name))


def label_key_candidates(obj: SourceObject) -> List[str]:
    """Return the keys of the PDS4 label that describes *obj*, most likely first.

    An XML object is its own label.  A data file is described by ``<stem>.xml`` (or ``.XML``) in the same
    directory, and a ``<name>_inventory`` file by ``<name>.xml``.
    """
    if obj.rel.lower().endswith(".xml"):
        return [obj.key]
    directory, sep, name = obj.key.rpartition("/")
    stem = name.rpartition(".")[0] or name
    stems = [stem] + ([stem[: -len("_inventory")]] if stem.endswith("_inventory") else [])
    return [f"{directory}{sep}{s}{ext}" for s in stems for ext in (".xml", ".XML")]


def parse_s3cmd_attrs(value: Optional[str]) -> Dict[str, str]:
    """Parse an ``s3cmd-attrs`` metadata value such as ``uid:1/gname:x/mtime:1456789012/md5:abc``."""
    attrs: Dict[str, str] = {}
    if not value:
        return attrs
    for part in value.split("/"):
        name, sep, val = part.partition(":")
        if sep:
            attrs[name] = val
    return attrs


_MD5_RE = re.compile(r"^[0-9a-f]{32}$")


def extract_head(head: dict) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Return ``(mtime, md5, crc64nvme)`` from a HeadObject response; each is None when absent."""
    attrs = parse_s3cmd_attrs((head.get("Metadata") or {}).get("s3cmd-attrs"))
    mtime_text = attrs.get("mtime", "")
    mtime = int(mtime_text) if mtime_text.isdigit() else None
    md5 = attrs.get("md5", "").lower()
    # CRC64NVME is always a full-object checksum, so it is comparable across copies.
    return mtime, (md5 if _MD5_RE.match(md5) else None), head.get("ChecksumCRC64NVME") or None


def apply_head(obj: SourceObject, head: dict) -> None:
    """Fill mtime, md5 and CRC64NVME on *obj* from a HeadObject response."""
    obj.mtime, obj.md5, obj.crc64nvme = extract_head(head)


def _hash_value(obj: SourceObject, kind: str) -> Optional[str]:
    """Return one content hash of *obj*, or None when that kind is unavailable.

    The ETag is the content MD5 only for single-part, SSE-S3 objects, so a multipart ETag
    (``<hex>-<parts>``) is not a usable hash.
    """
    if kind == "etag":
        return obj.etag if obj.etag and "-" not in obj.etag else None
    return getattr(obj, kind)


def same_content(a: SourceObject, b: SourceObject) -> bool:
    """Return True only when two objects are provably byte-identical.

    Sizes must match, at least one hash kind must be present on both, and every hash kind present on
    both must agree.  Hash kinds are the single-part ETag, the s3cmd md5 and the CRC64NVME checksum.
    """
    if a.size != b.size:
        return False
    compared = False
    for kind in ("etag", "crc64nvme", "md5"):
        va, vb = _hash_value(a, kind), _hash_value(b, kind)
        if va is not None and vb is not None:
            if va != vb:
                return False
            compared = True
    return compared


def route_object(obj: SourceObject) -> Tuple[str, str]:
    """Return ``(category, dst)`` for an object: JUNK, GARBAGE, or MERGE with its pds4/ destination."""
    if is_junk(obj.rel):
        return JUNK, ""
    if is_garbage(obj.top, obj.rel):
        return GARBAGE, garbage_key(obj.top, obj.rel)
    return MERGE, destination_key(classify_prefix(obj.top), obj.top, obj.rel)


def _rank(obj: SourceObject, rule: str, use_mtime: bool) -> tuple:
    """Sort key where the largest value is the winner."""
    if rule == WINNER_PREFIX:
        return (obj.order, obj.mtime if obj.mtime is not None else -1, obj.last_modified)
    return (obj.mtime if use_mtime else -1, obj.order, obj.last_modified)


def _row(
    action: str, membership: Membership, obj: SourceObject, dst: str, reason: str, ref: str = "", version: str = ""
) -> PlanRow:
    return PlanRow(
        action=action,
        family=membership.family,
        src_key=obj.key,
        dst_key=dst,
        size=obj.size,
        etag=obj.etag,
        last_modified=obj.last_modified,
        mtime="" if obj.mtime is None else str(obj.mtime),
        reason=reason,
        reference_key=ref,
        version_id=version,
    )


def plan_non_merge(obj: SourceObject, category: str, dst: str, kept_duplicate: Optional[str] = None) -> PlanRow:
    """Return the plan row for a JUNK or GARBAGE object.

    A GARBAGE object that is provably identical to a kept object (*kept_duplicate*, its key) is a DROP.
    """
    membership = classify_prefix(obj.top)
    if category == JUNK:
        return _row(JUNK, membership, obj, "", "junk_file")
    if kept_duplicate is not None:
        return _row(DROP, membership, obj, "", "identical_to_kept_content", kept_duplicate)
    return _row(GARBAGE, membership, obj, dst, "garbage_directory")


def resolve_group(
    members: List[SourceObject],
    rule: str,
    taken: Callable[[str], bool],
    version_of: Callable[[SourceObject], Optional[str]],
) -> List[PlanRow]:
    """Decide the fate of objects that all map to the same destination key.

    The winner (newest) keeps the plain destination key.  Every other object is DROPped if it is identical
    to the winner (or to an older object already kept).  Objects are visited newest first, so the copy kept
    for each distinct content is the newest one.

    The remaining older versions are classified by the PDS4 ``version_id`` of their label.  The same
    ``version_id`` as the winner, or as a newer older version already kept, is an in-place update, so it is
    GARBAGE.  A different ``version_id`` is a real earlier version, so it is RENAMEd to
    ``<stem>_v<version_id><ext>`` beside the winner (or SUPERSEDEd if that key is already used).  An older
    version with no label, or none with a ``version_id``, was most likely removed or overwritten and is
    SUPERSEDEd so that a copy is kept.  The winner keeps the plain name even without a label, so that a current
    file always remains; when its ``version_id`` is unknown, older versions are only compared with each other.

    With the mtime rule, a group whose objects differ in content and where any object lacks ``mtime`` cannot be
    ranked; every object in it gets a REVIEW row and is left alone.

    Args:
        members: Objects that map to the same destination key.
        rule: ``mtime`` or ``prefix-number``.
        taken: Returns True if a destination key is already claimed by another object.
        version_of: Returns the ``version_id`` of an object's label, or None if it is unknown.
    """
    all_identical = all(same_content(members[0], m) for m in members[1:])
    use_mtime = rule == WINNER_MTIME and all(m.mtime is not None for m in members)
    if rule == WINNER_MTIME and not use_mtime and not all_identical:
        return [_row(REVIEW, classify_prefix(m.top), m, "", "mtime_missing") for m in members]

    ordered = sorted(members, key=lambda o: _rank(o, rule, use_mtime), reverse=True)
    winner = ordered[0]
    win_membership = classify_prefix(winner.top)
    if use_mtime:
        reason = "newest_mtime"
    elif rule == WINNER_PREFIX:
        reason = "highest_prefix_number"
    else:
        reason = "identical_content"
    win_dst = destination_key(win_membership, winner.top, winner.rel)
    rows = [_row(MOVE, win_membership, winner, win_dst, reason)]

    kept_losers: List[SourceObject] = []
    twins: Dict[str, List[SourceObject]] = {}  # identical copies of a kept older version, decided below
    for obj in ordered[1:]:
        membership = classify_prefix(obj.top)
        if same_content(winner, obj):
            rows.append(_row(DROP, membership, obj, "", "identical_to_winner", winner.key))
            continue
        twin = next((k for k in kept_losers if same_content(k, obj)), None)
        if twin is not None:
            twins.setdefault(twin.key, []).append(obj)
            continue
        kept_losers.append(obj)

    versions: Dict[str, str] = {}
    if kept_losers:
        for o in [winner, *kept_losers]:
            found = version_of(o)
            if found is not None:
                versions[o.key] = found
        if winner.key in versions:
            rows[0] = _row(MOVE, win_membership, winner, win_dst, reason, version=versions[winner.key])
    seen_versions = {versions[winner.key]} if winner.key in versions else set()

    used = {win_dst}
    for obj in kept_losers:
        membership = classify_prefix(obj.top)
        version = versions.get(obj.key)
        for copy in twins.get(obj.key, []):
            rows.append(_row(DROP, classify_prefix(copy.top), copy, "", "identical_to_older_kept", obj.key))
        if version is None:
            # No label (or no version_id in it): most likely removed or overwritten.  Keep a copy.
            dst = superseded_key(obj.top, obj.rel)
            rows.append(_row(SUPERSEDE, membership, obj, dst, "no_label_version_id", winner.key))
            continue
        if version in seen_versions:
            dst = garbage_key(obj.top, obj.rel)
            rows.append(_row(GARBAGE, membership, obj, dst, "same_version_id_in_place_update", winner.key, version))
            continue
        seen_versions.add(version)
        renamed = versioned_key(membership, obj.top, obj.rel, version)
        if renamed is not None and renamed not in used and not taken(renamed):
            used.add(renamed)
            rows.append(_row(RENAME, membership, obj, renamed, "older_version_renamed", winner.key, version))
        else:
            dst = superseded_key(obj.top, obj.rel)
            rows.append(_row(SUPERSEDE, membership, obj, dst, "rename_unavailable", winner.key, version))
    return rows


def plan_group(
    dst: str,
    members: List[SourceObject],
    rule: str,
    taken: Callable[[str], bool],
    version_of: Callable[[SourceObject], Optional[str]],
) -> List[PlanRow]:
    """Return plan rows for every object that maps to *dst* (one object means no collision)."""
    if len(members) == 1:
        return [_row(MOVE, classify_prefix(members[0].top), members[0], dst, "no_collision")]
    return resolve_group(members, rule, taken, version_of)


# ---------------------------------------------------------------------------
# SQLite working state
# ---------------------------------------------------------------------------


class PlanDb:
    """Thread-safe wrapper around the SQLite working database."""

    def __init__(self, path: str):
        """Open (creating or upgrading if needed) the database at *path*."""
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        columns = {r[1] for r in self._conn.execute("PRAGMA table_info(objects)")}
        if "needs_head" not in columns:
            self._conn.execute("ALTER TABLE objects ADD COLUMN needs_head INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()
        label_columns = {r[1] for r in self._conn.execute("PRAGMA table_info(label_versions)")}
        if "want_files" not in label_columns:
            self._conn.execute("ALTER TABLE label_versions ADD COLUMN want_files INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()
        self._lock = threading.Lock()

    def insert_objects(self, objs: List[SourceObject], unit: Optional[str] = None) -> None:
        """Insert listed objects, ignoring any already present (keeps HEAD results on resume).

        When *unit* is given, the last key inserted is saved with the rows so an interrupted listing of that
        sub-prefix can continue after it.
        """
        rows = []
        for o in objs:
            category, dst = route_object(o)
            rows.append((o.key, o.top, o.rel, o.size, o.etag, o.last_modified, category, dst))
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO objects (key, top, rel, size, etag, last_modified, category, dst) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            if unit is not None and objs:
                self._conn.execute(
                    "INSERT OR REPLACE INTO unit_progress (unit, last_key) VALUES (?, ?)", (unit, objs[-1].key)
                )
            self._conn.commit()

    def unit_resume_key(self, unit: str) -> Optional[str]:
        """Return the last key saved while listing *unit*, or None if it was never started."""
        with self._lock:
            row = self._conn.execute("SELECT last_key FROM unit_progress WHERE unit = ?", (unit,)).fetchone()
        return row[0] if row else None

    def mark_unit_listed(self, unit: str) -> None:
        """Record that a sub-prefix has been listed completely."""
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO listed_units (unit) VALUES (?)", (unit,))
            self._conn.execute("DELETE FROM unit_progress WHERE unit = ?", (unit,))
            self._conn.commit()

    def listed_units(self) -> Set[str]:
        """Return the set of fully listed sub-prefixes."""
        with self._lock:
            return {r[0] for r in self._conn.execute("SELECT unit FROM listed_units")}

    def mark_needs_head(self) -> int:
        """Flag the objects whose ``mtime`` is needed and return how many still need a HEAD.

        An object needs one when it shares a destination key with another object, unless every object in
        that group is the same single-part content (then nothing has to be ranked: all but one are dropped).
        """
        with self._lock:
            self._conn.execute(
                "UPDATE objects SET needs_head = 1 WHERE category = ? AND needs_head = 0 AND dst IN ("
                "SELECT dst FROM objects WHERE category = ? GROUP BY dst HAVING COUNT(*) > 1 AND NOT ("
                "MIN(size) = MAX(size) AND MIN(etag) = MAX(etag) AND INSTR(MIN(etag), '-') = 0))",
                (MERGE, MERGE),
            )
            self._conn.commit()
        return self.pending_head_count()

    def pending_head_count(self) -> int:
        """Return the number of flagged objects that have not been HEAD-ed yet."""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM objects WHERE needs_head = 1 AND head_done = 0"
            ).fetchone()[0]

    def next_head_batch(self, after_key: str, limit: int) -> List[str]:
        """Return up to *limit* keys that still need a HEAD, in key order, starting after *after_key*."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT key FROM objects WHERE needs_head = 1 AND head_done = 0 AND key > ? ORDER BY key LIMIT ?",
                (after_key, limit),
            )
            return [r[0] for r in cur]

    def save_heads(self, results: List[Tuple[str, Optional[int], Optional[str], Optional[str]]]) -> None:
        """Store HEAD results as ``(key, mtime, md5, crc64nvme)``."""
        with self._lock:
            self._conn.executemany(
                "UPDATE objects SET mtime = ?, md5 = ?, crc64nvme = ?, head_done = 1 WHERE key = ?",
                [(m, md5, crc, key) for key, m, md5, crc in results],
            )
            self._conn.commit()

    def iter_flagged_objects(self, page: int = 5000) -> Iterator[SourceObject]:
        """Yield the objects flagged by ``mark_needs_head`` (members of groups with differing content)."""
        after = ""
        while True:
            rows = self._conn.execute(
                "SELECT key, top, rel, size, etag, last_modified FROM objects "
                "WHERE needs_head = 1 AND key > ? ORDER BY key LIMIT ?",
                (after, page),
            ).fetchall()
            if not rows:
                return
            after = rows[-1][0]
            for key, top, rel, size, etag, modified in rows:
                yield SourceObject(key, top, rel, size, etag, modified, classify_prefix(top).order)

    def object_exists(self, key: str) -> bool:
        """Return True if an object with this exact key was listed."""
        return self._conn.execute("SELECT 1 FROM objects WHERE key = ?", (key,)).fetchone() is not None

    def add_labels(self, label_keys: List[str]) -> None:
        """Queue labels whose ``version_id`` has to be read (already queued or read ones are kept)."""
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO label_versions (label_key) VALUES (?)", [(k,) for k in label_keys]
            )
            self._conn.commit()

    def pending_label_count(self) -> int:
        """Return the number of queued labels that have not been read yet."""
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM label_versions WHERE done = 0").fetchone()[0]

    def next_label_batch(self, after_key: str, limit: int) -> List[str]:
        """Return up to *limit* unread label keys in key order, starting after *after_key*."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT label_key FROM label_versions WHERE done = 0 AND label_key > ? ORDER BY label_key LIMIT ?",
                (after_key, limit),
            )
            return [r[0] for r in cur]

    def queue_file_labels(self, label_keys: List[str]) -> None:
        """Queue labels to be read in full for the data files they name; re-queue ones already read without it."""
        with self._lock:
            self._conn.executemany(
                "INSERT INTO label_versions (label_key, want_files) VALUES (?, 1) "
                "ON CONFLICT(label_key) DO UPDATE SET "
                "done = CASE WHEN label_versions.want_files = 1 THEN label_versions.done ELSE 0 END, want_files = 1",
                [(k,) for k in label_keys],
            )
            self._conn.commit()

    def labels_wanting_files(self, label_keys: List[str]) -> Set[str]:
        """Return which of *label_keys* are to be read in full for their ``file_name`` entries."""
        marks = ",".join("?" for _ in label_keys)
        with self._lock:
            cur = self._conn.execute(
                f"SELECT label_key FROM label_versions WHERE want_files = 1 AND label_key IN ({marks})", label_keys
            )
            return {r[0] for r in cur}

    def save_versions(self, results: List[Tuple[str, Optional[str], List[str]]]) -> None:
        """Store label results as ``(label_key, version_id, file_names)``.

        A version_id of None means the tag was not found.  ``file_names`` are the data files the label describes.
        """
        with self._lock:
            self._conn.executemany(
                "UPDATE label_versions SET version_id = ?, done = 1 WHERE label_key = ?",
                [(version, key) for key, version, _ in results],
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO label_files (label_dir, file_name, label_key) VALUES (?, ?, ?)",
                [(key.rpartition("/")[0] + "/", name, key) for key, _, names in results for name in names],
            )
            self._conn.commit()

    def label_for_file(self, directory: str, file_name: str) -> Optional[str]:
        """Return the key of a label in *directory* whose ``<file_name>`` names *file_name*, if one was read."""
        row = self._conn.execute(
            "SELECT label_key FROM label_files WHERE label_dir = ? AND file_name = ? ORDER BY label_key LIMIT 1",
            (directory, file_name),
        ).fetchone()
        return row[0] if row else None

    def xml_keys_in_dir(self, directory: str) -> List[str]:
        """Return the XML objects directly in *directory* (which ends with ``/``)."""
        upper = directory[:-1] + "0"  # '0' sorts right after '/', so this bounds the keys under the directory
        rows = self._conn.execute("SELECT key FROM objects WHERE key >= ? AND key < ? ORDER BY key", (directory, upper))
        return [k for (k,) in rows if "/" not in k[len(directory):] and k.lower().endswith(".xml")]

    def has_sibling_data_file(self, xml_key: str) -> bool:
        """Return True if a non-XML object with the same name stem sits beside *xml_key*."""
        stem = xml_key.rpartition(".")[0]
        rows = self._conn.execute("SELECT key FROM objects WHERE key >= ? AND key < ?", (stem + ".", stem + "/"))
        return any(not k.lower().endswith(".xml") and "/" not in k[len(stem) + 1:] for (k,) in rows)

    def label_version(self, label_key: str) -> Optional[str]:
        """Return the stored ``version_id`` of a label, or None if unread or not found."""
        row = self._conn.execute("SELECT version_id FROM label_versions WHERE label_key = ?", (label_key,)).fetchone()
        return row[0] if row else None

    def dst_exists(self, dst: str) -> bool:
        """Return True if a MERGE object already maps to *dst*."""
        row = self._conn.execute(
            "SELECT 1 FROM objects WHERE category = ? AND dst = ? LIMIT 1", (MERGE, dst)
        ).fetchone()
        return row is not None

    def find_kept_duplicate(self, size: int, etag: str, tops: List[str]) -> Optional[str]:
        """Return the key of a MERGE object under *tops* with this size and single-part ETag, if any."""
        if not etag or "-" in etag:
            return None
        marks = ",".join("?" for _ in tops)
        row = self._conn.execute(
            f"SELECT key FROM objects WHERE category = ? AND size = ? AND etag = ? AND top IN ({marks}) LIMIT 1",
            (MERGE, size, etag, *tops),
        ).fetchone()
        return row[0] if row else None

    def iter_non_merge(self) -> Iterator[Tuple[SourceObject, str, str]]:
        """Yield ``(object, category, dst)`` for JUNK and GARBAGE objects."""
        cur = self._conn.execute(
            "SELECT key, top, rel, size, etag, last_modified, category, dst FROM objects WHERE category != ?",
            (MERGE,),
        )
        for key, top, rel, size, etag, modified, category, dst in cur:
            yield SourceObject(key, top, rel, size, etag, modified, classify_prefix(top).order), category, dst

    def iter_merge_groups(self) -> Iterator[Tuple[str, List[SourceObject]]]:
        """Yield ``(dst, members)`` for every destination key, one group in memory at a time."""
        cur = self._conn.execute(
            "SELECT dst, key, top, rel, size, etag, last_modified, mtime, md5, crc64nvme FROM objects "
            "WHERE category = ? ORDER BY dst",
            (MERGE,),
        )
        for dst, rows in itertools.groupby(cur, key=lambda r: r[0]):
            members = [
                SourceObject(key, top, rel, size, etag, modified, classify_prefix(top).order, mtime, md5, crc)
                for _, key, top, rel, size, etag, modified, mtime, md5, crc in rows
            ]
            yield dst, members

    def close(self) -> None:
        """Close the database."""
        self._conn.close()


# ---------------------------------------------------------------------------
# S3 access
# ---------------------------------------------------------------------------


def _to_source_object(item: dict) -> SourceObject:
    top, rel = split_source_key(item["Key"])
    return SourceObject(
        key=item["Key"],
        top=top,
        rel=rel,
        size=item["Size"],
        etag=item["ETag"].strip('"'),
        last_modified=item["LastModified"].isoformat(),
        order=classify_prefix(top).order if top else 0.0,
    )


def list_top_level(s3, bucket: str) -> Tuple[List[str], List[SourceObject]]:
    """Return the prefix names directly under PDS4/ and any objects sitting directly in PDS4/."""

    def run():
        names: List[str] = []
        root_objects: List[SourceObject] = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=SOURCE_ROOT, Delimiter="/"):
            names.extend(cp["Prefix"][len(SOURCE_ROOT):-1] for cp in page.get("CommonPrefixes", []))
            root_objects.extend(_to_source_object(o) for o in page.get("Contents", []) if o["Key"] != SOURCE_ROOT)
        return names, root_objects

    return resumable(run)


def is_listed(prefix: str, done: Set[str]) -> bool:
    """Return True if *prefix* or any of its ancestors was already listed completely."""
    parts = prefix.split("/")
    return any("/".join(parts[:i]) + "/" in done for i in range(1, len(parts)))


def expand_prefix(session: boto3.Session, bucket: str, prefix: str, depth: int, done: Set[str], db: PlanDb) -> List[str]:
    """Split *prefix* into leaf sub-prefixes that can be listed in parallel.

    Directories are walked *depth* levels with a delimiter (cheap: S3 skips over the contents), objects found
    directly in those directories are stored, and the leaves below are returned.  Anything already listed is
    skipped, including everything below a previously listed ancestor.
    """
    if is_listed(prefix, done):
        return []
    if depth == 0:
        return [prefix]
    s3 = s3_client(session)
    subprefixes: List[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        subprefixes.extend(cp["Prefix"] for cp in page.get("CommonPrefixes", []))
        db.insert_objects([_to_source_object(o) for o in page.get("Contents", [])])
    leaves: List[str] = []
    for sub in subprefixes:
        leaves.extend(expand_prefix(session, bucket, sub, depth - 1, done, db))
    return leaves


def list_unit(session: boto3.Session, bucket: str, prefix: str, db: PlanDb) -> int:
    """Recursively list *prefix* into the database, continuing after any saved position.  Returns the count."""
    s3 = s3_client(session)
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    resume = db.unit_resume_key(prefix)
    if resume:
        kwargs["StartAfter"] = resume
    count = 0
    for page in s3.get_paginator("list_objects_v2").paginate(**kwargs):
        items = [_to_source_object(o) for o in page.get("Contents", [])]
        db.insert_objects(items, unit=prefix)
        count += len(items)
        if count and count % 500000 < len(items):
            log.info("    %s: %d objects so far", prefix, count)
    db.mark_unit_listed(prefix)
    return count


def _run_all(pool: concurrent.futures.Executor, fn: Callable, items: List) -> Iterator[Tuple[object, object]]:
    """Yield ``(item, result)`` as work completes; on the first failure cancel the rest and stop cleanly."""
    futures = {pool.submit(fn, item): item for item in items}
    try:
        for fut in concurrent.futures.as_completed(futures):
            yield futures[fut], resumable(fut.result)
    finally:
        for fut in futures:
            fut.cancel()


def list_family(
    session: boto3.Session, bucket: str, tops: List[str], db: PlanDb, workers: int, depth: int
) -> None:
    """List every prefix of a family in parallel, skipping what earlier runs already listed."""
    done = db.listed_units()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        prefixes = [f"{SOURCE_ROOT}{t}/" for t in tops]
        units: List[str] = []
        for _, leaves in _run_all(pool, lambda p: expand_prefix(session, bucket, p, depth, done, db), prefixes):
            units.extend(leaves)
        units = sorted(set(units))
        log.info("  %d sub-prefixes left to list", len(units))
        for n, (unit, count) in enumerate(_run_all(pool, lambda u: list_unit(session, bucket, u, db), units), 1):
            if n % 50 == 0 or n == len(units) or count >= 100000:
                log.info("  [%d/%d] listed %s (%d objects)", n, len(units), unit, count)


def find_label(obj: SourceObject, db: PlanDb) -> Optional[str]:
    """Return the key of the label describing *obj*, or None.

    Tries the same-named label first.  For a data file without one, falls back to a label in the same
    directory whose ``<file_name>`` names the file (these labels are read by ``collect_label_keys``).
    """
    label = next((c for c in label_key_candidates(obj) if db.object_exists(c)), None)
    if label or obj.rel.lower().endswith(".xml"):
        return label
    directory, _, name = obj.key.rpartition("/")
    return db.label_for_file(directory + "/", name)


def collect_label_keys(db: PlanDb) -> int:
    """Queue the label of every object in a group with differing content.  Returns the labels still unread.

    A data file with no same-named label is matched through the ``<file_name>`` entries of the labels beside
    it.  Only labels that have no data file of their own with the same name stem can describe such a file
    (for example ``collection_x.xml`` describing ``collection_x_inventory.csv``), so only those are queued to
    be read in full.
    """
    batch: List[str] = []
    unlabeled_dirs: Set[str] = set()
    for obj in db.iter_flagged_objects():
        label = next((c for c in label_key_candidates(obj) if db.object_exists(c)), None)
        if label:
            batch.append(label)
        elif not obj.rel.lower().endswith(".xml"):
            unlabeled_dirs.add(obj.key.rpartition("/")[0] + "/")
        if len(batch) >= BATCH_SIZE:
            db.add_labels(batch)
            batch = []
    db.add_labels(batch)

    for directory in sorted(unlabeled_dirs):
        orphans = [k for k in db.xml_keys_in_dir(directory) if not db.has_sibling_data_file(k)]
        db.queue_file_labels(orphans)
    return db.pending_label_count()


def head_collisions(session: boto3.Session, bucket: str, db: PlanDb, workers: int) -> None:
    """HEAD the objects whose ``mtime`` is needed, in bounded batches, saving results after each batch."""
    pending = db.pending_head_count()
    if not pending:
        return
    log.info("  HEAD-ing %d objects that share a destination key and need an mtime", pending)

    def head_one(key: str):
        try:
            head = s3_client(session).head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        except AWS_ERRORS as e:
            return key, e
        return (key, *extract_head(head))

    run_batches(workers, db.next_head_batch, head_one, db.save_heads, pending, "HEAD")


def fetch_versions(session: boto3.Session, bucket: str, db: PlanDb, workers: int) -> None:
    """Read the ``version_id`` from the start of each pending label, in bounded batches, saving after each."""
    pending = db.pending_label_count()
    if not pending:
        return
    log.info("  Reading the version_id of %d labels", pending)
    wants_files: Set[str] = set()

    def next_batch(after: str, limit: int) -> List[str]:
        keys = db.next_label_batch(after, limit)
        wants_files.clear()
        if keys:
            wants_files.update(db.labels_wanting_files(keys))
        return keys

    def get_range(s3, key: str, nbytes: int) -> str:
        body = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{nbytes - 1}")["Body"].read()
        return body.decode("utf-8", errors="replace")

    def read_one(key: str):
        s3 = s3_client(session)
        want = key in wants_files
        nbytes = LABEL_FILES_BYTES if want else LABEL_RANGE_BYTES
        try:
            text = get_range(s3, key, nbytes)
            version = parse_version_id(text)
            if version is None and len(text) >= nbytes < LABEL_RETRY_BYTES:
                version = parse_version_id(get_range(s3, key, LABEL_RETRY_BYTES))
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "InvalidRange"):
                return key, None, []  # an empty or vanished label has no version_id
            return key, e
        except BotoCoreError as e:
            return key, e
        return key, version, parse_file_names(text) if want else []

    run_batches(workers, next_batch, read_one, db.save_versions, pending, "label")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(
        description="Plan the PDS4/ -> pds4/ rename and family consolidation (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--bucket", required=True, help="S3 bucket to plan against")
    p.add_argument("--profile", default=None, help="AWS profile name")
    p.add_argument("--region", default="us-west-2", help="AWS region (default: us-west-2)")
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--family", action="append", help="Family (destination prefix) to plan; repeatable")
    scope.add_argument("--all", action="store_true", help="Plan every prefix under PDS4/")
    p.add_argument(
        "--exclude-family",
        action="append",
        help="Leave this family out (repeatable), for example one that is already migrated; use with --all",
    )
    p.add_argument(
        "--winner-rule",
        choices=[WINNER_MTIME, WINNER_PREFIX],
        default=WINNER_MTIME,
        help="How a collision winner is chosen (default: mtime from s3cmd-attrs; prefix-number needs no HEADs)",
    )
    p.add_argument("--output", default="migration_plan.csv", help="Plan CSV to write (default: migration_plan.csv)")
    p.add_argument("--review-output", default=None, help="CSV of objects held out for review (default: <output>_review.csv)")
    p.add_argument("--db", default=None, help="SQLite working state (default: <output>.db)")
    p.add_argument("--split-depth", type=int, default=3, help="Directory levels split for parallel listing (default: 3)")
    p.add_argument("--list-workers", type=int, default=16, help="Parallel listing threads (default: 16)")
    p.add_argument("--workers", type=int, default=64, help="Parallel HEAD threads (default: 64)")
    p.add_argument("--show-collisions", type=int, default=20, help="Example collision groups to print (default: 20)")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )
    if level != "DEBUG":
        for name in ("boto3", "botocore", "urllib3", "s3transfer"):
            logging.getLogger(name).setLevel(logging.WARNING)


def _format_group(group: List[PlanRow]) -> str:
    lines = []
    for r in group:
        target = r.dst_key or "(not copied)"
        lines.append(f"    {r.action:<9} {r.src_key}  ->  {target}  [{r.reason}]")
    return "\n".join(lines)


def _review_path(output: str, explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    return f"{output[:-4]}_review.csv" if output.endswith(".csv") else f"{output}.review.csv"


def write_plan(db: PlanDb, args: argparse.Namespace, by_family: Dict[str, List[str]], wanted_tops: Set[str]) -> None:
    """Stream the plan and review CSVs from the database and log a summary.  Read-only for S3."""
    totals: Counter = Counter()
    sizes: Counter = Counter()
    examples: List[List[PlanRow]] = []
    collision_groups = 0
    names = [f.name for f in fields(PlanRow)]
    review_path = _review_path(args.output, args.review_output)

    def version_of(obj: SourceObject) -> Optional[str]:
        label = find_label(obj, db)
        return db.label_version(label) if label else None

    with open(args.output, "w", newline="") as plan_fh, open(review_path, "w", newline="") as review_fh:
        plan_csv = csv.DictWriter(plan_fh, fieldnames=names)
        review_csv = csv.DictWriter(review_fh, fieldnames=names)
        plan_csv.writeheader()
        review_csv.writeheader()

        def write(rows: List[PlanRow]) -> None:
            # Destinations are unique by construction: one winner per pds4/<family>/<rel> group, renamed keys are
            # checked against every existing key, and the superseded and garbage keys embed the source prefix.
            for r in rows:
                (review_csv if r.action == REVIEW else plan_csv).writerow(asdict(r))
                totals[r.action] += 1
                sizes[r.action] += r.size

        for obj, category, dst in db.iter_non_merge():
            if obj.top not in wanted_tops:
                continue
            duplicate = None
            if category == GARBAGE:
                duplicate = db.find_kept_duplicate(obj.size, obj.etag, by_family[classify_prefix(obj.top).family])
            write([plan_non_merge(obj, category, dst, duplicate)])

        for dst, members in db.iter_merge_groups():
            members = [m for m in members if m.top in wanted_tops]
            if not members:
                continue
            rows = plan_group(dst, members, args.winner_rule, db.dst_exists, version_of)
            write(rows)
            if len(members) > 1:
                collision_groups += 1
                if len(examples) < args.show_collisions:
                    examples.append(rows)

    log.info("=== Plan summary ===")
    for action in (MOVE, RENAME, SUPERSEDE, DROP, GARBAGE, JUNK, REVIEW):
        log.info("%-9s %10d objects  %16d bytes", action, totals[action], sizes[action])
    log.info("Collision groups: %d", collision_groups)
    log.info("Plan:   %s", args.output)
    log.info("Review: %s  (%d objects held out, left untouched)", review_path, totals[REVIEW])
    if examples:
        log.info("=== Example collision groups ===")
        for group in examples:
            log.info("\n  %s\n%s", group[0].dst_key, _format_group(group))


def select_families(
    available: Dict[str, List[str]], families: Optional[List[str]], select_all: bool, exclude: List[str]
) -> List[str]:
    """Return the families to plan.

    Args:
        available: Family name -> its prefixes under PDS4/.
        families: Families named with ``--family`` (None when ``--all`` is used).
        select_all: True for ``--all``.
        exclude: Families to leave out (``--exclude-family``), for example ones already migrated.

    Raises:
        MigrationPlanError: if a named or excluded family has no prefixes under PDS4/.
    """
    unknown = [f for f in [*(families or []), *exclude] if f not in available]
    if unknown:
        raise MigrationPlanError(f"No prefixes found for family: {', '.join(unknown)}")
    wanted = sorted(available) if select_all else list(families or [])
    return [f for f in wanted if f not in set(exclude)]


def main() -> int:
    """Build a migration plan CSV for one or more families.  Read-only, resumable."""
    args = parse_args()
    _configure_logging(args.log_level)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    db = PlanDb(args.db or f"{args.output}.db")
    try:
        names, root_objects = list_top_level(s3_client(session), args.bucket)
        by_family: Dict[str, List[str]] = defaultdict(list)
        for name in names:
            by_family[classify_prefix(name).family].append(name)

        wanted = select_families(by_family, args.family, args.all, args.exclude_family or [])
        if not wanted:
            raise MigrationPlanError("No families are left to plan after --exclude-family.")

        log.info("READ-ONLY plan against s3://%s/%s  (winner rule: %s)", args.bucket, SOURCE_ROOT, args.winner_rule)
        for family in wanted:
            tops = sorted(by_family[family])
            log.info("Family %s: prefixes %s", family, ", ".join(tops))
            list_family(session, args.bucket, tops, db, args.list_workers, args.split_depth)
        if args.all and root_objects:
            db.insert_objects(root_objects)

        db.mark_needs_head()
        if args.winner_rule == WINNER_MTIME:
            head_collisions(session, args.bucket, db, args.workers)
            if db.pending_head_count():
                log.warning("Some colliding objects could not be HEAD-ed; they are held out for review.")
        log.info("  Finding the labels of objects whose content differs")
        collect_label_keys(db)
        fetch_versions(session, args.bucket, db, args.workers)
        if db.pending_label_count():
            log.warning("Some labels could not be read; their groups are held out for review.")
    except MigrationPlanError as e:
        log.error("%s", e)
        db.close()
        return 1
    except KeyboardInterrupt:
        log.warning("Interrupted. Progress is saved; rerun the same command to resume.")
        db.close()
        return 130

    wanted_tops = {t for f in wanted for t in by_family[f]} | ({""} if args.all else set())
    write_plan(db, args, by_family, wanted_tops)
    log.info("Nothing was changed in S3.")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
