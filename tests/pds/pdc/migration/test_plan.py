"""Tests for the PDS4 prefix migration planner (no AWS access)."""
from collections import defaultdict

import pytest
from botocore.exceptions import ClientError

from pds.pdc.migration import s3_common
from pds.pdc.migration.errors import MigrationPlanError
from pds.pdc.migration.errors import MigrationStoppedError
from pds.pdc.migration.plan_prefix_migration import apply_head
from pds.pdc.migration.plan_prefix_migration import collect_label_keys
from pds.pdc.migration.plan_prefix_migration import DROP
from pds.pdc.migration.plan_prefix_migration import find_label
from pds.pdc.migration.plan_prefix_migration import GARBAGE
from pds.pdc.migration.plan_prefix_migration import is_listed
from pds.pdc.migration.plan_prefix_migration import JUNK
from pds.pdc.migration.plan_prefix_migration import label_key_candidates
from pds.pdc.migration.plan_prefix_migration import MERGE
from pds.pdc.migration.plan_prefix_migration import MOVE
from pds.pdc.migration.plan_prefix_migration import parse_file_names
from pds.pdc.migration.plan_prefix_migration import parse_s3cmd_attrs
from pds.pdc.migration.plan_prefix_migration import parse_version_id
from pds.pdc.migration.plan_prefix_migration import plan_group
from pds.pdc.migration.plan_prefix_migration import plan_non_merge
from pds.pdc.migration.plan_prefix_migration import PlanDb
from pds.pdc.migration.plan_prefix_migration import RENAME
from pds.pdc.migration.plan_prefix_migration import REVIEW
from pds.pdc.migration.plan_prefix_migration import route_object
from pds.pdc.migration.plan_prefix_migration import select_families
from pds.pdc.migration.plan_prefix_migration import same_content
from pds.pdc.migration.plan_prefix_migration import SourceObject
from pds.pdc.migration.plan_prefix_migration import SUPERSEDE
from pds.pdc.migration.plan_prefix_migration import WINNER_MTIME
from pds.pdc.migration.plan_prefix_migration import WINNER_PREFIX
from pds.pdc.migration.rules import classify_prefix
from pds.pdc.migration.rules import split_source_key
from pds.pdc.migration.s3_common import wait_for_recovery


def make(key, size=10, mtime=None, md5=None, crc=None, etag=None, modified="2025-01-01T00:00:00+00:00"):
    top, rel = split_source_key(key)
    return SourceObject(
        key=key,
        top=top,
        rel=rel,
        size=size,
        # Objects built with the same crc/md5 share an ETag, mimicking single-part content hashes.
        etag=etag or f"h:{crc or md5 or key}",
        last_modified=modified,
        order=classify_prefix(top).order if top else 0.0,
        mtime=mtime,
        md5=md5,
        crc64nvme=crc,
    )


def plan(objs, rule):
    """Route objects and plan them the same way main() does, returning (rows, number of collision groups)."""
    rows = []
    groups = defaultdict(list)
    for o in objs:
        category, dst = route_object(o)
        if category == MERGE:
            groups[dst].append(o)
        else:
            rows.append(plan_non_merge(o, category, dst))
    for dst, members in groups.items():
        # Every prefix is its own label version, so differing older copies are real earlier versions.
        rows.extend(plan_group(dst, members, rule, lambda d: d in groups, lambda o: f"{int(o.order)}.0"))
    return rows, sum(1 for m in groups.values() if len(m) > 1)


def by_src(rows):
    return {r.src_key: r for r in rows}


def test_parse_s3cmd_attrs():
    attrs = parse_s3cmd_attrs("uid:1000/gname:pds/mtime:1456789012/md5:0123456789abcdef0123456789abcdef")
    assert attrs["mtime"] == "1456789012"
    assert attrs["md5"] == "0123456789abcdef0123456789abcdef"
    assert parse_s3cmd_attrs(None) == {}


def test_apply_head():
    obj = make("PDS4/MAVEN/a")
    head = {"Metadata": {"s3cmd-attrs": "mtime:1456789012/md5:0123456789abcdef0123456789abcdef"}, "ChecksumCRC64NVME": "AAA="}
    apply_head(obj, head)
    assert (obj.mtime, obj.md5, obj.crc64nvme) == (1456789012, "0123456789abcdef0123456789abcdef", "AAA=")


def test_apply_head_with_no_metadata():
    obj = make("PDS4/MAVEN/a")
    apply_head(obj, {})
    assert (obj.mtime, obj.md5, obj.crc64nvme) == (None, None, None)


def test_same_content_requires_agreeing_hashes():
    assert same_content(make("PDS4/A/x", crc="c"), make("PDS4/B/x", crc="c"))
    assert not same_content(make("PDS4/A/x", crc="c"), make("PDS4/B/x", crc="d"))
    assert not same_content(make("PDS4/A/x"), make("PDS4/B/x"))  # different ETags
    assert not same_content(make("PDS4/A/x", size=1, crc="c"), make("PDS4/B/x", size=2, crc="c"))
    # Same ETag but conflicting CRC: not provably identical.
    assert not same_content(make("PDS4/A/x", etag="e", crc="c"), make("PDS4/B/x", etag="e", crc="d"))


def test_multipart_etags_are_not_a_content_hash():
    a = make("PDS4/MAVEN11/f", etag="0123abcd-5", mtime=1)
    b = make("PDS4/MAVEN12/f", etag="0123abcd-5", mtime=2)
    assert not same_content(a, b)
    got = by_src(plan([a, b], WINNER_MTIME)[0])
    assert got["PDS4/MAVEN11/f"].action == RENAME


def test_single_part_etag_alone_identifies_identical_content():
    a = make("PDS4/MAVEN11/f", etag="0123abcd")
    b = make("PDS4/MAVEN12/f", etag="0123abcd")
    assert same_content(a, b)
    got = by_src(plan([a, b], WINNER_PREFIX)[0])
    assert got["PDS4/MAVEN12/f"].action == MOVE
    assert got["PDS4/MAVEN11/f"].action == DROP


def test_no_collision_is_a_plain_move():
    rows, collisions = plan([make("PDS4/MAVEN11/a/f.xml")], WINNER_MTIME)
    assert collisions == 0
    assert rows[0].action == MOVE
    assert rows[0].dst_key == "pds4/MAVEN/a/f.xml"


def test_newest_mtime_keeps_the_plain_name_and_older_versions_are_renamed_beside_it():
    objs = [
        make("PDS4/MAVEN/a/f.xml", mtime=100, crc="old"),
        make("PDS4/MAVEN43/a/f.xml", mtime=50, crc="mid"),
        make("PDS4/MAVEN11/a/f.xml", mtime=300, crc="new"),
    ]
    rows, collisions = plan(objs, WINNER_MTIME)
    got = by_src(rows)
    assert collisions == 1
    assert (got["PDS4/MAVEN11/a/f.xml"].action, got["PDS4/MAVEN11/a/f.xml"].dst_key) == (MOVE, "pds4/MAVEN/a/f.xml")
    assert (got["PDS4/MAVEN/a/f.xml"].action, got["PDS4/MAVEN/a/f.xml"].dst_key) == (RENAME, "pds4/MAVEN/a/f_v0.0.xml")
    assert (got["PDS4/MAVEN43/a/f.xml"].action, got["PDS4/MAVEN43/a/f.xml"].dst_key) == (RENAME, "pds4/MAVEN/a/f_v43.0.xml")


def test_prefix_number_rule_ignores_mtime():
    objs = [make("PDS4/MAVEN11/f", mtime=900, crc="a"), make("PDS4/MAVEN43/f", mtime=1, crc="b")]
    got = by_src(plan(objs, WINNER_PREFIX)[0])
    assert got["PDS4/MAVEN43/f"].action == MOVE
    assert got["PDS4/MAVEN11/f"].dst_key == "pds4/MAVEN/f_v11.0"


def test_missing_mtime_holds_the_whole_group_for_review():
    objs = [make("PDS4/MAVEN11/f", mtime=900, crc="a"), make("PDS4/MAVEN43/f", mtime=None, crc="b")]
    rows = plan(objs, WINNER_MTIME)[0]
    assert {r.action for r in rows} == {REVIEW}
    assert all(r.dst_key == "" for r in rows)


def test_missing_mtime_does_not_matter_when_content_is_identical():
    objs = [make("PDS4/MAVEN11/f", crc="same"), make("PDS4/MAVEN43/f", crc="same")]
    got = by_src(plan(objs, WINNER_MTIME)[0])
    assert got["PDS4/MAVEN43/f"].action == MOVE
    assert got["PDS4/MAVEN11/f"].action == DROP


def test_identical_content_is_dropped():
    objs = [
        make("PDS4/MAVEN/f", mtime=1, crc="same"),
        make("PDS4/MAVEN11/f", mtime=2, crc="same"),
        make("PDS4/MAVEN12/f", mtime=3, crc="same"),
    ]
    got = by_src(plan(objs, WINNER_MTIME)[0])
    assert got["PDS4/MAVEN12/f"].action == MOVE
    assert got["PDS4/MAVEN11/f"].action == DROP
    assert got["PDS4/MAVEN/f"].action == DROP


def test_identical_older_versions_keep_only_the_newest():
    objs = [
        make("PDS4/MAVEN13/f", mtime=4, crc="win"),
        make("PDS4/MAVEN12/f", mtime=3, crc="old"),
        make("PDS4/MAVEN11/f", mtime=2, crc="old"),
    ]
    got = by_src(plan(objs, WINNER_MTIME)[0])
    assert got["PDS4/MAVEN12/f"].action == RENAME
    assert got["PDS4/MAVEN12/f"].dst_key == "pds4/MAVEN/f_v12.0"
    assert got["PDS4/MAVEN11/f"].action == DROP
    assert got["PDS4/MAVEN11/f"].reference_key == "PDS4/MAVEN12/f"


def test_rename_falls_back_to_superseded_when_the_name_is_taken():
    objs = [
        make("PDS4/MAVEN11/f.xml", mtime=2, crc="new"),
        make("PDS4/MAVEN/f.xml", mtime=1, crc="old"),
        make("PDS4/MAVEN12/f_v0.0.xml", crc="unrelated"),  # already occupies the name the base copy would get
    ]
    got = by_src(plan(objs, WINNER_MTIME)[0])
    assert got["PDS4/MAVEN/f.xml"].action == SUPERSEDE
    assert got["PDS4/MAVEN/f.xml"].dst_key == "pds4/superseded/MAVEN/f.xml"
    assert got["PDS4/MAVEN12/f_v0.0.xml"].dst_key == "pds4/MAVEN/f_v0.0.xml"


def test_junk_and_garbage_are_routed_before_collision_handling():
    objs = [
        make("PDS4/MAVEN11/a/.DS_Store"),
        make("PDS4/MAVEN12/a/.DS_Store"),
        make("PDS4/MAVEN13/ngims_bundleD13/x.csv"),
        make("PDS4/MAVEN12/ngims_bundle/x.csv"),
    ]
    rows, collisions = plan(objs, WINNER_MTIME)
    got = by_src(rows)
    assert collisions == 0
    assert got["PDS4/MAVEN11/a/.DS_Store"].action == JUNK
    assert got["PDS4/MAVEN12/a/.DS_Store"].action == JUNK
    assert got["PDS4/MAVEN13/ngims_bundleD13/x.csv"].action == GARBAGE
    assert got["PDS4/MAVEN13/ngims_bundleD13/x.csv"].dst_key == "potential-garbage/MAVEN13/ngims_bundleD13/x.csv"
    assert got["PDS4/MAVEN12/ngims_bundle/x.csv"].dst_key == "pds4/MAVEN/ngims_bundle/x.csv"


def test_garbage_identical_to_kept_content_is_dropped():
    obj = make("PDS4/MAVEN13/ngims_bundleD13/x.csv")
    row = plan_non_merge(obj, GARBAGE, "potential-garbage/MAVEN13/ngims_bundleD13/x.csv", "PDS4/MAVEN42/ngims_bundle/x.csv")
    assert row.action == DROP
    assert row.reference_key == "PDS4/MAVEN42/ngims_bundle/x.csv"


def test_insight_1b_sorts_between_v1_and_v2_and_each_older_version_gets_its_own_name():
    objs = [make("PDS4/InSightv1/f", crc="a"), make("PDS4/InSight1B/f", crc="b"), make("PDS4/InSightv2/f", crc="c")]
    versions = {"PDS4/InSightv1/f": "1.0", "PDS4/InSight1B/f": "2.0", "PDS4/InSightv2/f": "3.0"}
    got = by_src(plan_group("pds4/InSight/f", objs, WINNER_PREFIX, lambda d: False, lambda o: versions[o.key]))
    assert got["PDS4/InSightv2/f"].dst_key == "pds4/InSight/f"
    assert got["PDS4/InSight1B/f"].dst_key == "pds4/InSight/f_v2.0"
    assert got["PDS4/InSightv1/f"].dst_key == "pds4/InSight/f_v1.0"


def test_every_destination_is_unique():
    objs = [
        make("PDS4/MAVEN/f", mtime=1, crc="a"),
        make("PDS4/MAVEN11/f", mtime=2, crc="b"),
        make("PDS4/MAVEN12/f", mtime=3, crc="c"),
        make("PDS4/MAVEN12/g", mtime=3, crc="c"),
        make("PDS4/MAVEN13/ngims_bundleD13/g", mtime=3, crc="c"),
    ]
    rows, _ = plan(objs, WINNER_MTIME)
    dsts = [r.dst_key for r in rows if r.dst_key]
    assert len(dsts) == len(set(dsts))


def test_wait_for_recovery_resumes_once_requests_succeed(monkeypatch):
    monkeypatch.setattr(s3_common.time, "sleep", lambda s: None)
    outcomes = iter([("k", ClientError({"Error": {"Code": "403"}}, "HeadObject")), ("k", 5, None, None)])
    wait_for_recovery(lambda: next(outcomes), "403")  # returns without raising


def test_wait_for_recovery_gives_up_after_the_deadline(monkeypatch):
    monkeypatch.setattr(s3_common.time, "sleep", lambda s: None)
    failing = ("k", ClientError({"Error": {"Code": "403"}}, "HeadObject"))
    with pytest.raises(MigrationStoppedError):
        wait_for_recovery(lambda: failing, "403")


LABEL = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<Product_Observational xmlns="http://pds.nasa.gov/pds4/pds/v1">\n'
    "<Identification_Area><logical_identifier>urn:nasa:pds:x:y</logical_identifier><version_id>1.1</version_id>"
    "<title>T</title><Modification_History><Modification_Detail><modification_date>2020-01-01</modification_date>"
    "<version_id>1.0</version_id><description>d</description></Modification_Detail></Modification_History>"
    "</Identification_Area></Product_Observational>"
)


def test_parse_version_id_takes_the_product_version_not_a_history_entry():
    assert parse_version_id(LABEL) == "1.1"


def test_parse_version_id_handles_prefixed_tags_and_whitespace():
    text = "<pds:Identification_Area><pds:logical_identifier>u</pds:logical_identifier><pds:version_id> 2.1 </pds:version_id>"
    assert parse_version_id(text) == "2.1"


def test_parse_version_id_returns_none_when_absent():
    assert parse_version_id("<Product_Observational></Product_Observational>") is None
    assert parse_version_id("") is None


def test_label_key_candidates():
    assert label_key_candidates(make("PDS4/LADEE/a/f.xml")) == ["PDS4/LADEE/a/f.xml"]
    assert label_key_candidates(make("PDS4/LADEE/a/UVS.XML")) == ["PDS4/LADEE/a/UVS.XML"]
    assert label_key_candidates(make("PDS4/LADEE/a/f.tab")) == ["PDS4/LADEE/a/f.xml", "PDS4/LADEE/a/f.XML"]
    assert label_key_candidates(make("PDS4/MAVEN/c/collection_x_inventory.csv")) == [
        "PDS4/MAVEN/c/collection_x_inventory.xml",
        "PDS4/MAVEN/c/collection_x_inventory.XML",
        "PDS4/MAVEN/c/collection_x.xml",
        "PDS4/MAVEN/c/collection_x.XML",
    ]


def versioned(rows_for, versions):
    objs, dst = rows_for
    return by_src(plan_group(dst, objs, WINNER_MTIME, lambda d: False, lambda o: versions.get(o.key)))


def test_same_version_id_means_in_place_update_so_the_older_copy_goes_to_garbage():
    objs = [make("PDS4/LADEE/a/f.xml", mtime=1, crc="old"), make("PDS4/LADEEv1/a/f.xml", mtime=2, crc="new")]
    versions = {"PDS4/LADEE/a/f.xml": "1.0", "PDS4/LADEEv1/a/f.xml": "1.0"}
    got = versioned((objs, "pds4/LADEE/a/f.xml"), versions)
    assert got["PDS4/LADEEv1/a/f.xml"].action == MOVE
    assert got["PDS4/LADEE/a/f.xml"].action == GARBAGE
    assert got["PDS4/LADEE/a/f.xml"].dst_key == "potential-garbage/LADEE/a/f.xml"
    assert got["PDS4/LADEE/a/f.xml"].reason == "same_version_id_in_place_update"


def test_different_version_id_means_a_real_earlier_version_so_it_is_renamed_beside_the_newest():
    objs = [make("PDS4/LADEE/a/f.xml", mtime=1, crc="old"), make("PDS4/LADEEv1/a/f.xml", mtime=2, crc="new")]
    versions = {"PDS4/LADEE/a/f.xml": "1.0", "PDS4/LADEEv1/a/f.xml": "2.0"}
    got = versioned((objs, "pds4/LADEE/a/f.xml"), versions)
    assert got["PDS4/LADEE/a/f.xml"].action == RENAME
    assert got["PDS4/LADEE/a/f.xml"].dst_key == "pds4/LADEE/a/f_v1.0.xml"
    assert got["PDS4/LADEE/a/f.xml"].version_id == "1.0"
    assert got["PDS4/LADEEv1/a/f.xml"].version_id == "2.0"


def test_only_the_newest_object_of_each_older_version_id_is_kept():
    objs = [
        make("PDS4/MAVEN13/f.xml", mtime=4, crc="win"),
        make("PDS4/MAVEN12/f.xml", mtime=3, crc="b"),
        make("PDS4/MAVEN11/f.xml", mtime=2, crc="c"),
        make("PDS4/MAVEN/f.xml", mtime=1, crc="d"),
    ]
    versions = {
        "PDS4/MAVEN13/f.xml": "3.0",
        "PDS4/MAVEN12/f.xml": "2.0",
        "PDS4/MAVEN11/f.xml": "2.0",  # an in-place update of the 2.0 kept just above it in time
        "PDS4/MAVEN/f.xml": "1.0",
    }
    got = versioned((objs, "pds4/MAVEN/f.xml"), versions)
    assert got["PDS4/MAVEN13/f.xml"].action == MOVE
    assert (got["PDS4/MAVEN12/f.xml"].action, got["PDS4/MAVEN12/f.xml"].dst_key) == (RENAME, "pds4/MAVEN/f_v2.0.xml")
    assert got["PDS4/MAVEN11/f.xml"].action == GARBAGE
    assert (got["PDS4/MAVEN/f.xml"].action, got["PDS4/MAVEN/f.xml"].dst_key) == (RENAME, "pds4/MAVEN/f_v1.0.xml")


def test_with_no_labels_at_all_the_newest_stays_live_and_older_copies_are_kept_in_superseded():
    objs = [
        make("PDS4/MAVEN/d/f.pdf", mtime=1, crc="a"),
        make("PDS4/MAVEN28/d/f.pdf", mtime=2, crc="b"),
        make("PDS4/MAVEN42/d/f.pdf", mtime=3, crc="c"),
    ]
    got = versioned((objs, "pds4/MAVEN/d/f.pdf"), {})
    assert (got["PDS4/MAVEN42/d/f.pdf"].action, got["PDS4/MAVEN42/d/f.pdf"].dst_key) == (MOVE, "pds4/MAVEN/d/f.pdf")
    assert got["PDS4/MAVEN28/d/f.pdf"].action == SUPERSEDE
    assert got["PDS4/MAVEN28/d/f.pdf"].dst_key == "pds4/superseded/MAVEN28/d/f.pdf"
    assert got["PDS4/MAVEN28/d/f.pdf"].reason == "no_label_version_id"
    assert got["PDS4/MAVEN/d/f.pdf"].dst_key == "pds4/superseded/MAVEN/d/f.pdf"


def test_an_older_version_without_a_label_is_kept_in_superseded_and_the_rest_are_classified():
    objs = [
        make("PDS4/MAVEN33/f.tab", mtime=1, crc="partial"),  # this prefix has no label for the file
        make("PDS4/MAVEN13/f.tab", mtime=2, crc="old"),
        make("PDS4/MAVEN42/f.tab", mtime=3, crc="new"),
    ]
    versions = {"PDS4/MAVEN13/f.tab": "13.0", "PDS4/MAVEN42/f.tab": "14.0"}
    got = versioned((objs, "pds4/MAVEN/f.tab"), versions)
    assert got["PDS4/MAVEN42/f.tab"].action == MOVE
    assert (got["PDS4/MAVEN13/f.tab"].action, got["PDS4/MAVEN13/f.tab"].dst_key) == (RENAME, "pds4/MAVEN/f_v13.0.tab")
    assert (got["PDS4/MAVEN33/f.tab"].action, got["PDS4/MAVEN33/f.tab"].dst_key) == (
        SUPERSEDE,
        "pds4/superseded/MAVEN33/f.tab",
    )


def test_when_the_newest_has_no_label_older_versions_are_compared_with_each_other():
    objs = [
        make("PDS4/MAVEN42/f.tab", mtime=4, crc="new"),  # newest, no label
        make("PDS4/MAVEN33/f.tab", mtime=3, crc="b"),
        make("PDS4/MAVEN13/f.tab", mtime=2, crc="c"),
        make("PDS4/MAVEN12/f.tab", mtime=1, crc="d"),
    ]
    versions = {"PDS4/MAVEN33/f.tab": "13.0", "PDS4/MAVEN13/f.tab": "13.0", "PDS4/MAVEN12/f.tab": "7.0"}
    got = versioned((objs, "pds4/MAVEN/f.tab"), versions)
    assert got["PDS4/MAVEN42/f.tab"].action == MOVE
    assert got["PDS4/MAVEN33/f.tab"].dst_key == "pds4/MAVEN/f_v13.0.tab"
    assert got["PDS4/MAVEN13/f.tab"].action == GARBAGE  # an in-place update of the 13.0 above it
    assert got["PDS4/MAVEN12/f.tab"].dst_key == "pds4/MAVEN/f_v7.0.tab"


def test_copies_identical_to_a_version_kept_in_superseded_are_dropped():
    objs = [
        make("PDS4/MAVEN42/f.tab", mtime=4, crc="new"),
        make("PDS4/MAVEN33/f.tab", mtime=3, crc="old"),  # no label, kept in superseded
        make("PDS4/MAVEN13/f.tab", mtime=2, crc="old"),  # identical to it, so a copy is retained
    ]
    got = versioned((objs, "pds4/MAVEN/f.tab"), {"PDS4/MAVEN42/f.tab": "14.0", "PDS4/MAVEN13/f.tab": "13.0"})
    assert got["PDS4/MAVEN33/f.tab"].action == SUPERSEDE
    assert got["PDS4/MAVEN13/f.tab"].action == DROP
    assert got["PDS4/MAVEN13/f.tab"].reference_key == "PDS4/MAVEN33/f.tab"


def test_copies_identical_to_a_classified_older_version_are_dropped():
    objs = [
        make("PDS4/MAVEN42/f.tab", mtime=4, crc="new"),
        make("PDS4/MAVEN33/f.tab", mtime=3, crc="old"),
        make("PDS4/MAVEN13/f.tab", mtime=2, crc="old"),
    ]
    versions = {"PDS4/MAVEN42/f.tab": "14.0", "PDS4/MAVEN33/f.tab": "13.0", "PDS4/MAVEN13/f.tab": "13.0"}
    got = versioned((objs, "pds4/MAVEN/f.tab"), versions)
    assert got["PDS4/MAVEN33/f.tab"].action == RENAME
    assert got["PDS4/MAVEN13/f.tab"].action == DROP
    assert got["PDS4/MAVEN13/f.tab"].reference_key == "PDS4/MAVEN33/f.tab"


def test_identical_copies_never_need_a_version_id():
    objs = [make("PDS4/LADEE/f.txt", mtime=1, crc="same"), make("PDS4/LADEEv1/f.txt", mtime=2, crc="same")]
    got = versioned((objs, "pds4/LADEE/f.txt"), {})
    assert got["PDS4/LADEEv1/f.txt"].action == MOVE
    assert got["PDS4/LADEE/f.txt"].action == DROP


def test_db_queues_the_label_of_each_object_with_differing_content_and_stores_versions():
    db = PlanDb(":memory:")
    db.insert_objects(
        [
            make("PDS4/LADEE/a/f.tab", size=1, etag="e1"),
            make("PDS4/LADEEv1/a/f.tab", size=2, etag="e2"),
            make("PDS4/LADEE/a/f.xml", size=3, etag="e3"),
            make("PDS4/LADEEv1/a/f.xml", size=4, etag="e4"),
            make("PDS4/LADEE/a/solo.tab", size=1, etag="e5"),
            make("PDS4/LADEE/a/solo.xml", size=1, etag="e6"),
        ]
    )
    db.mark_needs_head()
    # f.tab and f.xml share one label per prefix, so only two labels are queued; solo.* are not in a collision.
    assert collect_label_keys(db) == 2
    assert db.next_label_batch("", 10) == ["PDS4/LADEE/a/f.xml", "PDS4/LADEEv1/a/f.xml"]

    db.save_versions([("PDS4/LADEE/a/f.xml", "1.0", []), ("PDS4/LADEEv1/a/f.xml", None, [])])
    assert db.pending_label_count() == 0
    assert db.label_version("PDS4/LADEE/a/f.xml") == "1.0"
    assert db.label_version("PDS4/LADEEv1/a/f.xml") is None
    assert find_label(make("PDS4/LADEE/a/f.tab"), db) == "PDS4/LADEE/a/f.xml"
    assert find_label(make("PDS4/LADEE/a/missing.tab"), db) is None


def test_parse_file_names_lists_each_referenced_file_once():
    text = (
        "<File_Area_Inventory><File><file_name>collection_x_inventory.csv</file_name></File>"
        "<pds:file_name> other.tab </pds:file_name><file_name>collection_x_inventory.csv</file_name>"
        "<file_name></file_name></File_Area_Inventory>"
    )
    assert parse_file_names(text) == ["collection_x_inventory.csv", "other.tab"]


def test_a_data_file_without_a_same_named_label_is_matched_through_file_name():
    db = PlanDb(":memory:")
    db.insert_objects(
        [
            # collection_x.xml has no data file of its own name: it describes collection_x_inventory.csv.
            make("PDS4/MAVEN/c/collection_x.xml", size=1, etag="a1"),
            make("PDS4/MAVEN11/c/collection_x.xml", size=2, etag="a2"),
            make("PDS4/MAVEN/c/other_data.dat", size=3, etag="b1"),
            make("PDS4/MAVEN11/c/other_data.dat", size=4, etag="b2"),
            # product.xml has product.fits beside it, so it is not a candidate for the file_name search.
            make("PDS4/MAVEN/c/product.xml", size=5, etag="c1"),
            make("PDS4/MAVEN/c/product.fits", size=6, etag="d1"),
            make("PDS4/MAVEN/c/data_v1.csv", size=7, etag="e1"),
            make("PDS4/MAVEN11/c/data_v1.csv", size=8, etag="e2"),
        ]
    )
    db.mark_needs_head()
    assert db.has_sibling_data_file("PDS4/MAVEN/c/product.xml")
    assert not db.has_sibling_data_file("PDS4/MAVEN/c/collection_x.xml")
    assert db.xml_keys_in_dir("PDS4/MAVEN/c/") == ["PDS4/MAVEN/c/collection_x.xml", "PDS4/MAVEN/c/product.xml"]

    collect_label_keys(db)
    queued = db.next_label_batch("", 100)
    assert "PDS4/MAVEN/c/collection_x.xml" in queued
    assert "PDS4/MAVEN/c/product.xml" not in queued
    assert db.labels_wanting_files(queued) == {"PDS4/MAVEN/c/collection_x.xml", "PDS4/MAVEN11/c/collection_x.xml"}

    # Reading the label in full finds the data files it describes.
    db.save_versions(
        [
            ("PDS4/MAVEN/c/collection_x.xml", "1.0", ["data_v1.csv"]),
            ("PDS4/MAVEN11/c/collection_x.xml", "2.0", ["data_v1.csv"]),
        ]
    )
    assert find_label(make("PDS4/MAVEN/c/data_v1.csv"), db) == "PDS4/MAVEN/c/collection_x.xml"
    assert find_label(make("PDS4/MAVEN11/c/data_v1.csv"), db) == "PDS4/MAVEN11/c/collection_x.xml"
    assert find_label(make("PDS4/MAVEN/c/other_data.dat"), db) is None  # no label names it: held for review


def test_a_label_already_read_without_files_is_requeued_when_the_file_names_are_needed():
    db = PlanDb(":memory:")
    db.add_labels(["PDS4/MAVEN/c/collection_x.xml"])
    db.save_versions([("PDS4/MAVEN/c/collection_x.xml", "1.0", [])])
    assert db.pending_label_count() == 0
    db.queue_file_labels(["PDS4/MAVEN/c/collection_x.xml"])
    assert db.pending_label_count() == 1
    assert db.labels_wanting_files(["PDS4/MAVEN/c/collection_x.xml"]) == {"PDS4/MAVEN/c/collection_x.xml"}


AVAILABLE = {"MAVEN": ["MAVEN", "MAVEN11"], "LADEE": ["LADEE", "LADEEv1"], "Huygens": ["Huygens"], "PHX": ["PHX"]}


def test_select_families_for_named_families():
    assert select_families(AVAILABLE, ["LADEE", "PHX"], False, []) == ["LADEE", "PHX"]


def test_select_families_for_all_can_leave_out_the_ones_already_migrated():
    assert select_families(AVAILABLE, None, True, []) == ["Huygens", "LADEE", "MAVEN", "PHX"]
    assert select_families(AVAILABLE, None, True, ["MAVEN", "LADEE"]) == ["Huygens", "PHX"]


def test_select_families_rejects_unknown_names():
    with pytest.raises(MigrationPlanError):
        select_families(AVAILABLE, ["Nope"], False, [])
    with pytest.raises(MigrationPlanError):
        select_families(AVAILABLE, None, True, ["Nope"])


def test_is_listed_covers_descendants_of_listed_prefixes():
    done = {"PDS4/LADEEv1/uvs_bundle/"}
    assert is_listed("PDS4/LADEEv1/uvs_bundle/", done)
    assert is_listed("PDS4/LADEEv1/uvs_bundle/data/0843L/", done)
    assert not is_listed("PDS4/LADEEv1/", done)
    assert not is_listed("PDS4/LADEEv1/ldex_bundle/", done)


def test_db_flags_only_colliding_groups_whose_objects_differ():
    db = PlanDb(":memory:")
    db.insert_objects(
        [
            make("PDS4/MAVEN/a/f.xml", size=1, etag="e1"),
            make("PDS4/MAVEN11/a/f.xml", size=2, etag="e2"),  # differs: needs mtime
            make("PDS4/MAVEN/a/same.xml", size=5, etag="s"),
            make("PDS4/MAVEN11/a/same.xml", size=5, etag="s"),  # identical single-part: no mtime needed
            make("PDS4/MAVEN/a/multi.xml", size=5, etag="m-3"),
            make("PDS4/MAVEN11/a/multi.xml", size=5, etag="m-3"),  # multipart ETag is not a hash: needs mtime
            make("PDS4/MAVEN11/a/only_here.xml"),
            make("PDS4/MAVEN11/.DS_Store"),
        ]
    )
    assert db.mark_needs_head() == 4
    batch = db.next_head_batch("", 100)
    assert batch == [
        "PDS4/MAVEN/a/f.xml",
        "PDS4/MAVEN/a/multi.xml",
        "PDS4/MAVEN11/a/f.xml",
        "PDS4/MAVEN11/a/multi.xml",
    ]
    db.save_heads([(k, 5, None, None) for k in batch])
    assert db.pending_head_count() == 0
    assert db.next_head_batch("", 100) == []


def test_db_head_batches_resume_after_a_key():
    db = PlanDb(":memory:")
    db.insert_objects([make(f"PDS4/MAVEN{n}/f", size=n) for n in (11, 12, 13)])
    db.mark_needs_head()
    assert db.next_head_batch("PDS4/MAVEN12/f", 10) == ["PDS4/MAVEN13/f"]


def test_db_streams_groups_and_non_merge_objects():
    db = PlanDb(":memory:")
    db.insert_objects(
        [make("PDS4/MAVEN/a/f.xml"), make("PDS4/MAVEN11/a/f.xml"), make("PDS4/MAVEN11/a/g.xml"), make("PDS4/MAVEN11/.DS_Store")]
    )
    groups = {dst: members for dst, members in db.iter_merge_groups()}
    assert sorted(groups) == ["pds4/MAVEN/a/f.xml", "pds4/MAVEN/a/g.xml"]
    assert len(groups["pds4/MAVEN/a/f.xml"]) == 2
    assert [(o.key, c) for o, c, _ in db.iter_non_merge()] == [("PDS4/MAVEN11/.DS_Store", JUNK)]
    assert db.dst_exists("pds4/MAVEN/a/g.xml")
    assert not db.dst_exists("pds4/MAVEN/a/g_11.xml")


def test_db_insert_keeps_existing_head_results():
    db = PlanDb(":memory:")
    db.insert_objects([make("PDS4/MAVEN/f", size=1), make("PDS4/MAVEN11/f", size=2)])
    db.mark_needs_head()
    db.save_heads([("PDS4/MAVEN/f", 5, None, "c"), ("PDS4/MAVEN11/f", 6, None, "c")])
    db.insert_objects([make("PDS4/MAVEN/f", size=1), make("PDS4/MAVEN11/f", size=2)])  # re-listing on resume
    assert db.pending_head_count() == 0


def test_db_remembers_listing_position_within_a_unit():
    db = PlanDb(":memory:")
    unit = "PDS4/MAVEN42/iuvs_raw_bundle/"
    assert db.unit_resume_key(unit) is None
    db.insert_objects([make(f"{unit}a"), make(f"{unit}b")], unit=unit)
    assert db.unit_resume_key(unit) == f"{unit}b"
    db.mark_unit_listed(unit)
    assert db.unit_resume_key(unit) is None
    assert unit in db.listed_units()


def test_db_finds_a_kept_duplicate_by_content_within_the_family():
    db = PlanDb(":memory:")
    db.insert_objects(
        [
            make("PDS4/MAVEN42/ngims_bundle/x.csv", size=7, etag="abc"),
            make("PDS4/Huygens/y.csv", size=7, etag="abc"),
            make("PDS4/MAVEN13/ngims_bundleD13/x.csv", size=7, etag="abc"),
        ]
    )
    assert db.find_kept_duplicate(7, "abc", ["MAVEN", "MAVEN13", "MAVEN42"]) == "PDS4/MAVEN42/ngims_bundle/x.csv"
    assert db.find_kept_duplicate(7, "abc", ["MAVEN13"]) is None  # the garbage object itself is not "kept"
    assert db.find_kept_duplicate(8, "abc", ["MAVEN42"]) is None
    assert db.find_kept_duplicate(7, "abc-2", ["MAVEN42"]) is None  # multipart ETags are never trusted
