"""Tests for the PDS4 prefix migration naming rules."""
import pytest

from pds.pdc.migration.errors import MigrationPlanError
from pds.pdc.migration.rules import classify_prefix
from pds.pdc.migration.rules import destination_key
from pds.pdc.migration.rules import garbage_key
from pds.pdc.migration.rules import is_garbage
from pds.pdc.migration.rules import is_junk
from pds.pdc.migration.rules import split_source_key
from pds.pdc.migration.rules import superseded_key
from pds.pdc.migration.rules import versioned_key


@pytest.mark.parametrize(
    "name,family,order",
    [
        ("MAVEN", "MAVEN", 0),
        ("MAVEN11", "MAVEN", 11),
        ("MAVEN43", "MAVEN", 43),
        ("InSightv1", "InSight", 1),
        ("InSight1B", "InSight", 1.5),
        ("InSightv17", "InSight", 17),
        ("Mars2020v14", "Mars2020", 14),
        ("juno_jiram_bundle", "juno_jiram_bundle", 0),
        ("juno_jiram_bundlev21", "juno_jiram_bundle", 21),
        ("lab.hydrocarbon_spectraV5", "lab.hydrocarbon_spectra", 5),
        ("mgs_tes_recalib_atmosV3", "mgs_tes_recalib_atmos", 3),
        ("pitms_bundlev2", "pitms_bundle", 2),
        ("LADEEv1", "LADEE", 1),
    ],
)
def test_family_members(name, family, order):
    m = classify_prefix(name)
    assert (m.family, m.order) == (family, order)


@pytest.mark.parametrize(
    "name",
    ["messmas_1001v16", "messmas_2101v1", "vo_3001", "vo_3002", "InSight_vortex_bundle", "insight_edl_bundle", "Huygens"],
)
def test_standalone_prefixes_are_never_split(name):
    m = classify_prefix(name)
    assert (m.family, m.order) == (name, 0)


def test_split_source_key():
    assert split_source_key("PDS4/MAVEN11/a/b.xml") == ("MAVEN11", "a/b.xml")
    assert split_source_key("PDS4/readme.txt") == ("", "readme.txt")
    with pytest.raises(MigrationPlanError):
        split_source_key("pds3/x")


@pytest.mark.parametrize(
    "rel,expected",
    [
        ("a/.DS_Store", True),
        ("a/._collection.tab", True),
        ("._.DS_Store", True),
        ("bundle/.reconstruction.xml.swp", True),
        ("a/file.swp", False),
        ("a/collection.xml", False),
        ("a/.hidden", False),
    ],
)
def test_is_junk(rel, expected):
    assert is_junk(rel) is expected


def test_garbage_dirs():
    assert is_garbage("MAVEN13", "ngims_bundleD13/l2/x.csv")
    assert is_garbage("LADEE", "nms_data_cal.save/y")
    assert not is_garbage("MAVEN13", "ngims_bundle/l2/x.csv")
    assert not is_garbage("MAVEN12", "ngims_bundleD13/x")


@pytest.mark.parametrize(
    "prefix,rel,version,expected",
    [
        ("MAVEN", "a/b/f.xml", "1.0", "pds4/MAVEN/a/b/f_v1.0.xml"),
        ("MAVEN12", "a/f.XML", "2.1", "pds4/MAVEN/a/f_v2.1.XML"),
        ("InSightv16", "ps/x.csv", "3", "pds4/InSight/ps/x_v3.csv"),
        ("juno_jiram_bundlev2", "d/readme", "1.0", "pds4/juno_jiram_bundle/d/readme_v1.0"),  # no extension
        ("MAVEN11", "f.tar.gz", "1.0", "pds4/MAVEN/f.tar_v1.0.gz"),
        ("MAVEN11", "top.xml", "1.0/beta 2", "pds4/MAVEN/top_v1.0_beta_2.xml"),  # unsafe characters are replaced
    ],
)
def test_versioned_key(prefix, rel, version, expected):
    assert versioned_key(classify_prefix(prefix), prefix, rel, version) == expected


def test_versioned_key_has_nothing_to_rename_for_directory_placeholders():
    assert versioned_key(classify_prefix("MAVEN12"), "MAVEN12", "a/b/", "1.0") is None


def test_destination_keys():
    m = classify_prefix("MAVEN11")
    assert destination_key(m, "MAVEN11", "acc_bundle/f.xml") == "pds4/MAVEN/acc_bundle/f.xml"
    assert destination_key(classify_prefix("Huygens"), "Huygens", "f") == "pds4/Huygens/f"
    assert destination_key(classify_prefix(""), "", "readme.txt") == "pds4/readme.txt"
    assert superseded_key("MAVEN11", "acc_bundle/f.xml") == "pds4/superseded/MAVEN11/acc_bundle/f.xml"
    assert garbage_key("LADEE", "nms_data_cal.save/y") == "potential-garbage/LADEE/nms_data_cal.save/y"
