"""Tests for the cleanup step of the PDS4 prefix migration (no AWS access)."""
import csv
import io

import pytest
from botocore.exceptions import ClientError

from pds.pdc.migration import delete_migrated_sources as dm
from pds.pdc.migration import execute_prefix_migration as ex

HEADER = [
    "action",
    "family",
    "src_key",
    "dst_key",
    "size",
    "etag",
    "last_modified",
    "mtime",
    "reason",
    "reference_key",
    "version_id",
]


def write_plan(path, rows):
    """rows are (action, src, dst, size, etag, reference)."""
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        for action, src, dst, size, etag, ref in rows:
            writer.writerow([action, "F", src, dst, size, etag, "t", "", "r", ref, ""])
    return str(path)


def head(size=10, etag="e1", crc="c1"):
    return {"ContentLength": size, "ETag": f'"{etag}"', "ChecksumCRC64NVME": crc}


class FakeS3:
    """Just enough of an S3 client: heads, a listing and soft deletes."""

    def __init__(self, objects):
        self.objects = objects
        self.deleted = []

    def head_object(self, Bucket, Key, ChecksumMode):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return self.objects[Key]

    def delete_objects(self, Bucket, Delete):
        keys = [o["Key"] for o in Delete["Objects"]]
        self.deleted.extend(keys)
        for key in keys:
            self.objects.pop(key, None)  # a delete marker hides the key; the data stays as a noncurrent version
        return {}

    def get_paginator(self, name):
        s3 = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                contents = [
                    {"Key": k, "Size": v["ContentLength"], "ETag": v["ETag"]}
                    for k, v in sorted(s3.objects.items())
                    if k.startswith(Prefix)
                ]
                return [{"Contents": contents[:2]}, {"Contents": contents[2:]}]

        return Paginator()


def test_content_matches_ignores_metadata_but_not_bytes():
    assert dm.content_matches(head(), head()) is None
    assert dm.content_matches(head(size=9), head()) == "size_mismatch"
    assert dm.content_matches(head(etag="x"), head()) == "etag_mismatch"
    assert dm.content_matches(head(crc="other"), head()) == "crc64nvme_mismatch"
    assert dm.content_matches(head(etag="m-2", crc=None), head(etag="m-2", crc=None)) == "no_comparable_hash"
    assert dm.content_matches(head(etag="m-2"), head(etag="m-3")) is None  # multipart ETags, same checksum


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    """Four copied sources (one whose copy is missing), two duplicates and a junk file, all copies verified."""
    plan = write_plan(
        tmp_path / "plan.csv",
        [
            ("MOVE", "PDS4/A/new", "pds4/A/new", 10, "e1", ""),
            ("RENAME", "PDS4/A/old", "pds4/A/old_v1.0", 10, "e2", "PDS4/A/new"),
            ("GARBAGE", "PDS4/A/g", "potential-garbage/A/g", 10, "e3", ""),
            ("MOVE", "PDS4/A/lost", "pds4/A/lost", 10, "e4", ""),  # its copy has disappeared
            ("DROP", "PDS4/A/dup", "", 10, "e1", "PDS4/A/new"),
            ("DROP", "PDS4/A/dup_chain", "", 10, "e1", "PDS4/A/dup"),  # keeps its content through another DROP
            ("DROP", "PDS4/A/liar", "", 10, "e1", "PDS4/A/new"),  # planned as identical but is not
            ("JUNK", "PDS4/A/.DS_Store", "", 6, "ej", ""),
        ],
    )
    s3 = FakeS3(
        {
            "PDS4/A/new": head(etag="e1"),
            "pds4/A/new": head(etag="e1"),
            "PDS4/A/old": head(etag="e2"),
            "pds4/A/old_v1.0": head(etag="e2"),
            "PDS4/A/g": head(etag="e3"),
            "potential-garbage/A/g": head(etag="e3"),
            "PDS4/A/lost": head(etag="e4"),
            "PDS4/A/dup": head(etag="e1"),
            "PDS4/A/dup_chain": head(etag="e1"),
            "PDS4/A/liar": head(etag="DIFFERENT"),
            "PDS4/A/.DS_Store": head(size=6, etag="ej"),
        }
    )
    monkeypatch.setattr(dm, "s3_client", lambda session: s3)
    db = ex.ExecDb(":memory:")
    db.load_plan(plan)
    db.load_removals(plan)
    db.set_states([(ex.VERIFIED, None, k) for k in ("PDS4/A/new", "PDS4/A/old", "PDS4/A/g", "PDS4/A/lost")])
    dm.list_destinations(None, "bkt", ["pds4/A/", "potential-garbage/A/"], db)
    return s3, db


def test_the_listing_records_what_is_at_the_destination(scenario):
    _, db = scenario
    assert db.seen_at_destination("pds4/A/new", 10, "e1")
    assert not db.seen_at_destination("pds4/A/lost", 10, "e4")
    assert not db.seen_at_destination("pds4/A/new", 11, "e1")  # wrong size
    assert not db.seen_at_destination("pds4/A/new", 10, "other")  # wrong ETag


def test_a_verified_copy_missing_from_the_destination_is_blocked_not_deleted(scenario):
    s3, db = scenario
    assert db.copy_counts_by_deletability() == {"deletable": 3, "blocked": 1}
    out = io.StringIO()
    deleted = dm.delete_copied_sources(None, "bkt", db, csv.writer(out), out, workers=2)
    assert deleted == 3
    assert sorted(s3.deleted) == ["PDS4/A/g", "PDS4/A/new", "PDS4/A/old"]
    assert "PDS4/A/lost" in s3.objects  # the copy is gone, so the source is kept
    assert db.counts()[ex.SOURCE_DELETED] == (3, 30)
    assert db.counts()[ex.VERIFIED] == (1, 10)
    assert "PDS4/A/new,COPIED,pds4/A/new,10,e1" in out.getvalue().replace("\r", "")


def test_a_limit_deletes_only_a_small_slice_and_a_later_run_continues(scenario):
    s3, db = scenario
    out = io.StringIO()
    assert dm.delete_copied_sources(None, "bkt", db, csv.writer(out), out, workers=2, limit=1) == 1
    assert s3.deleted == ["PDS4/A/g"]  # the first source in key order
    assert db.counts()[ex.SOURCE_DELETED] == (1, 10)
    assert dm.delete_copied_sources(None, "bkt", db, csv.writer(out), out, workers=2, limit=0) == 0
    assert dm.delete_copied_sources(None, "bkt", db, csv.writer(out), out, workers=2) == 2  # the rest
    assert sorted(s3.deleted) == ["PDS4/A/g", "PDS4/A/new", "PDS4/A/old"]


def test_a_limit_applies_to_duplicates_and_junk_too(scenario):
    s3, db = scenario
    out = io.StringIO()
    dm.delete_copied_sources(None, "bkt", db, csv.writer(out), out, workers=2)
    deleted, _ = dm.delete_duplicates(None, "bkt", db, csv.writer(out), out, workers=2, limit=1)
    assert deleted == 1
    assert dm.delete_junk(None, "bkt", db, csv.writer(out), out, limit=0) == 0
    assert "PDS4/A/.DS_Store" in s3.objects
    assert dm.delete_junk(None, "bkt", db, csv.writer(out), out, limit=5) == 1


def test_nothing_is_recorded_as_deleted_twice(scenario):
    s3, db = scenario
    out = io.StringIO()
    dm.delete_copied_sources(None, "bkt", db, csv.writer(out), out, workers=2)
    assert dm.delete_copied_sources(None, "bkt", db, csv.writer(out), out, workers=2) == 0
    assert len(s3.deleted) == 3


def test_duplicates_are_deleted_only_when_the_kept_copy_is_verified_and_the_bytes_agree(scenario):
    s3, db = scenario
    out = io.StringIO()
    dm.delete_copied_sources(None, "bkt", db, csv.writer(out), out, workers=2)
    deleted, skipped = dm.delete_duplicates(None, "bkt", db, csv.writer(out), out, workers=2)
    assert (deleted, skipped) == (2, 1)
    assert "PDS4/A/dup" not in s3.objects
    assert "PDS4/A/dup_chain" not in s3.objects  # resolved through PDS4/A/dup to the copy of PDS4/A/new
    assert "PDS4/A/liar" in s3.objects  # content differs from what the kept copy holds
    assert db.removal_counts()[("DROP", "skipped")] == 1
    assert "PDS4/A/dup,DROP,pds4/A/new,10,e1" in out.getvalue().replace("\r", "")


def test_a_duplicate_is_kept_when_what_keeps_its_content_is_not_verified(scenario):
    s3, db = scenario
    db.set_states([(ex.COPIED, None, "PDS4/A/new")])  # copied, but not verified
    out = io.StringIO()
    deleted, skipped = dm.delete_duplicates(None, "bkt", db, csv.writer(out), out, workers=2)
    assert deleted == 0
    assert skipped == 3
    assert {"PDS4/A/dup", "PDS4/A/dup_chain", "PDS4/A/liar"} <= set(s3.objects)


def test_junk_is_deleted_and_recorded(scenario):
    s3, db = scenario
    out = io.StringIO()
    assert dm.delete_junk(None, "bkt", db, csv.writer(out), out) == 1
    assert s3.deleted == ["PDS4/A/.DS_Store"]
    assert db.removal_counts()[("JUNK", "removed")] == 1


def test_the_summary_counts_copies_that_are_not_verified(scenario):
    _, db = scenario
    db.set_states([(ex.COPY_FAILED, "boom", "PDS4/A/lost")])
    summary = dm.summarize_plan(db)
    assert summary["unverified"] == 1
    assert summary["deletable"] == 3
    assert summary["blocked"] == 0


def test_delete_keys_reports_refused_deletes():
    class Refusing(FakeS3):
        def delete_objects(self, Bucket, Delete):
            return {"Errors": [{"Key": "PDS4/A/x", "Code": "AccessDenied"}]}

    assert dm.delete_keys(Refusing({}), "bkt", ["PDS4/A/x", "PDS4/A/y"]) == ["PDS4/A/x"]
