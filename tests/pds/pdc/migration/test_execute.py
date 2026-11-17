"""Tests for the copy-and-verify step of the PDS4 prefix migration (no AWS access)."""
import csv

import pytest
from botocore.exceptions import ClientError

from pds.pdc.migration import execute_prefix_migration as ex
from pds.pdc.migration.errors import MigrationStateError

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
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        for action, src, dst, size, etag in rows:
            writer.writerow([action, "F", src, dst, size, etag, "t", "", "r", "", ""])
    return str(path)


def obj(size=10, etag="e1", crc="c1", metadata=None, content_type="text/xml"):
    return {
        "ContentLength": size,
        "ETag": f'"{etag}"',
        "ChecksumCRC64NVME": crc,
        "Metadata": metadata if metadata is not None else {"s3cmd-attrs": "mtime:1"},
        "ContentType": content_type,
    }


class FakeS3:
    """Just enough of an S3 client for the copy and verify steps."""

    def __init__(self, objects, error=None):
        self.objects = objects
        self.error = error
        self.copies = []

    def copy_object(self, **kwargs):
        if self.error:
            raise self.error
        self.copies.append(kwargs)
        source = kwargs["CopySource"]["Key"]
        if source not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "CopyObject")
        self.objects[kwargs["Key"]] = dict(self.objects[source])

    def head_object(self, Bucket, Key, ChecksumMode):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return self.objects[Key]

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        return {"KeyCount": min(MaxKeys, sum(1 for k in self.objects if k.startswith(Prefix)))}


@pytest.mark.parametrize(
    "dst,root",
    [
        ("pds4/MAVEN/a/b/f.xml", "pds4/MAVEN/"),
        ("pds4/superseded/MAVEN11/a/f.xml", "pds4/superseded/MAVEN11/"),
        ("potential-garbage/LADEE/a/f.xml", "potential-garbage/LADEE/"),
        ("pds4/readme.txt", "pds4/readme.txt"),
    ],
)
def test_destination_root(dst, root):
    assert ex.destination_root(dst) == root


def test_compare_heads_accepts_an_identical_copy():
    assert ex.compare_heads(obj(), obj(), 10, "e1") is None


@pytest.mark.parametrize(
    "src,dst,size,etag,problem",
    [
        (obj(), obj(), 11, "e1", "source_changed_since_plan"),
        (obj(), obj(), 10, "other", "source_changed_since_plan"),
        (obj(), obj(size=9), 10, "e1", "size_mismatch"),
        (obj(), obj(etag="e2"), 10, "e1", "etag_mismatch"),
        (obj(), obj(crc="c2"), 10, "e1", "crc64nvme_mismatch"),
        (obj(), obj(metadata={}), 10, "e1", "metadata_mismatch"),
        (obj(), obj(content_type="text/plain"), 10, "e1", "content_type_mismatch"),
        (obj(etag="m-3", crc=None), obj(etag="m-3", crc=None), 10, "m-3", "no_comparable_hash"),
    ],
)
def test_compare_heads_reports_the_first_problem(src, dst, size, etag, problem):
    assert ex.compare_heads(src, dst, size, etag) == problem


def test_compare_heads_can_rely_on_the_checksum_when_the_etag_is_multipart():
    src, dst = obj(etag="m-3"), obj(etag="m-5")
    assert ex.compare_heads(src, dst, 10, "m-3") is None


def test_iter_copy_rows_skips_rows_that_do_not_copy(tmp_path):
    plan = write_plan(
        tmp_path / "plan.csv",
        [
            ("MOVE", "PDS4/A/f", "pds4/A/f", 1, "e"),
            ("RENAME", "PDS4/A/g", "pds4/A/g_v1.0", 2, "e"),
            ("GARBAGE", "PDS4/A/h", "potential-garbage/A/h", 3, "e"),
            ("SUPERSEDE", "PDS4/A/i", "pds4/superseded/A/i", 4, "e"),
            ("DROP", "PDS4/A/j", "", 5, "e"),
            ("JUNK", "PDS4/A/.DS_Store", "", 6, "e"),
        ],
    )
    assert [r[2] for r in ex.iter_copy_rows(plan)] == ["MOVE", "RENAME", "GARBAGE", "SUPERSEDE"]


def test_state_loads_the_plan_once_and_tracks_progress(tmp_path):
    plan = write_plan(
        tmp_path / "plan.csv",
        [("MOVE", "PDS4/A/f", "pds4/A/f", 1, "e"), ("MOVE", "PDS4/B/g", "pds4/B/g", 2, "e")],
    )
    db = ex.ExecDb(":memory:")
    assert db.load_plan(plan) == {"pds4/A/", "pds4/B/"}
    db.load_plan(plan)  # loading again keeps the rows as they are
    assert db.counts() == {ex.PENDING: (2, 3)}
    assert not db.has_progress()

    db.set_states([(ex.COPIED, None, "PDS4/A/f"), (ex.COPY_FAILED, "boom", "PDS4/B/g")])
    assert db.has_progress()
    assert db.next_keys(ex.COPIED, "", 10) == ["PDS4/A/f"]
    assert db.failures() == [("PDS4/B/g", ex.COPY_FAILED, "boom")]
    assert db.rows_for(["PDS4/A/f"]) == {"PDS4/A/f": ("pds4/A/f", 1, "e")}

    assert db.retry_failed() == 1
    assert db.counts() == {ex.COPIED: (1, 1), ex.PENDING: (1, 2)}


def test_copy_one_copies_with_metadata_and_checksum_kept():
    s3 = FakeS3({"PDS4/A/f": obj()})
    assert ex.copy_one(s3, "bkt", "PDS4/A/f", "pds4/A/f", 10) == ("PDS4/A/f", "copied", "")
    call = s3.copies[0]
    assert call["Key"] == "pds4/A/f"
    assert call["CopySource"] == {"Bucket": "bkt", "Key": "PDS4/A/f"}
    assert call["MetadataDirective"] == "COPY"
    assert call["ChecksumAlgorithm"] == "CRC64NVME"
    assert "PDS4/A/f" in s3.objects  # the source is untouched


def test_copy_one_reports_a_missing_source():
    assert ex.copy_one(FakeS3({}), "bkt", "PDS4/A/f", "pds4/A/f", 10) == ("PDS4/A/f", "failed", "source_missing")


def test_copy_one_refuses_objects_over_5_gib():
    s3 = FakeS3({"PDS4/A/f": obj()})
    result = ex.copy_one(s3, "bkt", "PDS4/A/f", "pds4/A/f", ex.MAX_COPY_OBJECT_BYTES + 1)
    assert result[1] == "failed"
    assert s3.copies == []


def test_copy_one_hands_other_aws_errors_to_the_batch_runner():
    error = ClientError({"Error": {"Code": "403"}}, "CopyObject")
    result = ex.copy_one(FakeS3({"PDS4/A/f": obj()}, error=error), "bkt", "PDS4/A/f", "pds4/A/f", 10)
    assert result == ("PDS4/A/f", error)


def test_verify_one():
    s3 = FakeS3({"PDS4/A/f": obj(), "pds4/A/f": obj()})
    assert ex.verify_one(s3, "bkt", "PDS4/A/f", "pds4/A/f", 10, "e1") == ("PDS4/A/f", "verified", "")
    s3.objects["pds4/A/f"] = obj(etag="changed")
    assert ex.verify_one(s3, "bkt", "PDS4/A/f", "pds4/A/f", 10, "e1") == ("PDS4/A/f", "failed", "etag_mismatch")
    del s3.objects["pds4/A/f"]
    assert ex.verify_one(s3, "bkt", "PDS4/A/f", "pds4/A/f", 10, "e1")[2] == "source_or_copy_missing"


def test_destinations_must_be_empty_on_a_fresh_start(monkeypatch):
    monkeypatch.setattr(ex, "s3_client", lambda session: FakeS3({"pds4/A/old": obj()}))
    ex.check_destinations_empty(None, "bkt", {"pds4/B/"})  # nothing there: fine
    with pytest.raises(MigrationStateError):
        ex.check_destinations_empty(None, "bkt", {"pds4/A/", "pds4/B/"})


def test_copy_then_verify_end_to_end(tmp_path, monkeypatch):
    plan = write_plan(
        tmp_path / "plan.csv",
        [
            ("MOVE", "PDS4/A/f", "pds4/A/f", 10, "e1"),
            ("RENAME", "PDS4/A/g", "pds4/A/g_v1.0", 10, "e1"),
            ("DROP", "PDS4/A/h", "", 10, "e1"),
        ],
    )
    s3 = FakeS3({"PDS4/A/f": obj(), "PDS4/A/g": obj(), "PDS4/A/h": obj()})
    monkeypatch.setattr(ex, "s3_client", lambda session: s3)
    db = ex.ExecDb(":memory:")
    db.load_plan(plan)

    ex.run_copy_phase(None, "bkt", db, workers=2)
    assert db.counts() == {ex.COPIED: (2, 20)}
    assert {"pds4/A/f", "pds4/A/g_v1.0"} <= set(s3.objects)
    assert "pds4/A/h" not in s3.objects  # a DROP row is never copied

    s3.objects["pds4/A/g_v1.0"] = obj(crc="corrupted")
    ex.run_verify_phase(None, "bkt", db, workers=2)
    assert db.counts() == {ex.VERIFIED: (1, 10), ex.VERIFY_FAILED: (1, 10)}
    assert db.failures() == [("PDS4/A/g", ex.VERIFY_FAILED, "crc64nvme_mismatch")]
    assert sorted(s3.objects) == sorted(["PDS4/A/f", "PDS4/A/g", "PDS4/A/h", "pds4/A/f", "pds4/A/g_v1.0"])


def test_a_changed_plan_is_detected_by_its_fingerprint(tmp_path):
    first = write_plan(tmp_path / "a.csv", [("MOVE", "PDS4/A/f", "pds4/A/f", 1, "e")])
    second = write_plan(tmp_path / "b.csv", [("MOVE", "PDS4/A/f", "pds4/A/other", 1, "e")])
    assert ex.file_sha256(first) != ex.file_sha256(second)
    assert ex.file_sha256(first) == ex.file_sha256(first)
