"""
Version Manager — reads local version, compares semver strings.
"""

from pathlib import Path


def get_version(version_file=None):
    """
    Read current version from version.txt in the app root.
    Returns version string like '1.0.0' or '0.0.0' if not found.
    """
    if version_file is None:
        version_file = Path(__file__).parent.parent / "version.txt"
    else:
        version_file = Path(version_file)

    if not version_file.exists():
        return "0.0.0"

    try:
        return version_file.read_text().strip()
    except Exception:
        return "0.0.0"


def _parse_version(v):
    """Parse a version string like '1.2.3' into a tuple of ints."""
    try:
        parts = v.strip().lstrip("vV").split(".")
        return tuple(int(p) for p in parts[:3])
    except (ValueError, AttributeError):
        return (0, 0, 0)


def compare_versions(v1, v2):
    """
    Compare two version strings.
    Returns -1 if v1 < v2, 0 if equal, 1 if v1 > v2.
    """
    t1 = _parse_version(v1)
    t2 = _parse_version(v2)
    if t1 < t2:
        return -1
    elif t1 > t2:
        return 1
    return 0


def should_update(current, latest, minimum=None):
    """
    Determine if an update is needed.
    Returns True if current < latest.
    If minimum is set, also returns True if current < minimum.
    """
    if compare_versions(current, latest) < 0:
        return True
    if minimum and compare_versions(current, minimum) < 0:
        return True
    return False
