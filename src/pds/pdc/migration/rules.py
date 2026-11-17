"""Pure naming rules for the PDS4/ -> pds4/ rename and family consolidation.

Nothing in this module talks to AWS, so every rule can be unit tested.

Layout produced from a source key ``PDS4/<top>/<rel>``:

  * ``pds4/<family>/<rel>``                    winner of a key, or an object with no collision
  * ``pds4/<family>/<dir>/<stem>_v<version_id><ext>``
                                               older object whose content differs from the winner, kept
                                               beside it (version_id comes from the object's PDS4 label)
  * ``pds4/superseded/<top>/<rel>``            fallback when that renamed key is unavailable
  * ``potential-garbage/<top>/<rel>``          directories set aside for review
  * (deleted, not copied)                      junk files and byte-identical duplicates
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from typing import Tuple

from pds.pdc.migration.errors import MigrationPlanError

SOURCE_ROOT = "PDS4/"
DEST_ROOT = "pds4/"
SUPERSEDED_PREFIX = "pds4/superseded/"
GARBAGE_PREFIX = "potential-garbage/"

# Families whose versioned prefixes (e.g. MAVEN11, InSightv3) are merged into one base prefix.
# Anything not listed here is treated as a standalone prefix, which is deliberate: names such as
# messmas_1001v16 or vo_3001 look versioned but are distinct bundles.
FAMILIES = frozenset(
    {
        "MAVEN",
        "InSight",
        "Mars2020",
        "juno_jiram_bundle",
        "lab.hydrocarbon_spectra",
        "mgs_tes_recalib_atmos",
        "mgsa_bundle",
        "mroa_bundle",
        "odya_bundle",
        "pitms_bundle",
        "LADEE",
    }
)

# Families whose version token is bare digits (MAVEN11) rather than v<N> / V<N>.
DIGIT_SUFFIX_FAMILIES = frozenset({"MAVEN"})

# InSight1B is a release between InSightv1 and InSightv2.
_SPECIAL_MEMBERS = {"InSight1B": ("InSight", 1.5)}

# Directories (relative to PDS4/) set aside for review instead of being merged.
GARBAGE_DIRS = ("MAVEN13/ngims_bundleD13/", "LADEE/nms_data_cal.save/")

_VERSION_RE = re.compile(r"^(?P<family>.*[A-Za-z0-9])[vV](?P<number>\d+)$")
_DIGIT_RE = re.compile(r"^(?P<family>[A-Za-z_]+?)(?P<number>\d+)$")


@dataclass(frozen=True)
class Membership:
    """Family a top-level prefix belongs to and its position within that family.

    Attributes:
        family: Name of the destination prefix under ``pds4/``.
        order: 0 for the base prefix, otherwise the version number (InSight1B is 1.5).
    """

    family: str
    order: float


def classify_prefix(name: str) -> Membership:
    """Return the family and version order for a top-level prefix name under ``PDS4/``."""
    if name in _SPECIAL_MEMBERS:
        family, order = _SPECIAL_MEMBERS[name]
        return Membership(family, order)
    if name in FAMILIES:
        return Membership(name, 0.0)

    digits = _DIGIT_RE.match(name)
    if digits and digits.group("family") in DIGIT_SUFFIX_FAMILIES:
        return Membership(digits.group("family"), float(digits.group("number")))

    versioned = _VERSION_RE.match(name)
    if versioned and versioned.group("family") in FAMILIES:
        return Membership(versioned.group("family"), float(versioned.group("number")))

    return Membership(name, 0.0)


def split_source_key(key: str) -> Tuple[str, str]:
    """Split ``PDS4/<top>/<rel>`` into ``(top, rel)``.

    Objects directly under ``PDS4/`` return an empty ``top``.

    Raises:
        MigrationPlanError: if the key is not under ``PDS4/``.
    """
    if not key.startswith(SOURCE_ROOT):
        raise MigrationPlanError(f"Key is not under {SOURCE_ROOT}: {key}")
    rest = key[len(SOURCE_ROOT):]
    top, sep, rel = rest.partition("/")
    if not sep:
        return "", rest
    return top, rel


def is_junk(rel: str) -> bool:
    """Return True for OS and editor droppings that are removed rather than migrated."""
    name = rel.rsplit("/", 1)[-1]
    return name == ".DS_Store" or name.startswith("._") or (name.startswith(".") and name.endswith(".swp"))


def is_garbage(top: str, rel: str) -> bool:
    """Return True if the object lives in a directory set aside as potential garbage."""
    return f"{top}/{rel}".startswith(GARBAGE_DIRS)


def destination_key(membership: Membership, top: str, rel: str) -> str:
    """Return the ``pds4/`` key for an object that wins (or does not collide)."""
    if not top:
        return f"{DEST_ROOT}{rel}"
    return f"{DEST_ROOT}{membership.family}/{rel}"


_UNSAFE_VERSION_CHARS = re.compile(r"[^\w.\-]")


def versioned_key(membership: Membership, top: str, rel: str, version_id: str) -> Optional[str]:
    """Return the key for an older version kept next to the current one: ``<stem>_v<version_id><ext>``.

    The file stays in the same directory, for example ``f.XML`` with version_id ``1.0`` becomes ``f_v1.0.XML``.
    Characters other than letters, digits, ``.``, ``_`` and ``-`` in the version_id are replaced by ``_``.

    Returns None when the key has no filename to rename (a directory placeholder).
    """
    directory, sep, name = rel.rpartition("/")
    if not name:
        return None
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, dot, ext = name, "", ""
    renamed = f"{stem}_v{_UNSAFE_VERSION_CHARS.sub('_', version_id)}{dot}{ext}"
    return destination_key(membership, top, f"{directory}{sep}{renamed}")


def superseded_key(top: str, rel: str) -> str:
    """Return the ``pds4/superseded/`` key for an older object that cannot be renamed in place."""
    return f"{SUPERSEDED_PREFIX}{top}/{rel}"


def garbage_key(top: str, rel: str) -> str:
    """Return the ``potential-garbage/`` key for an object in a garbage directory."""
    return f"{GARBAGE_PREFIX}{top}/{rel}"
