"""Pure-Python Debian version comparison (dpkg --compare-versions semantics).

Implemented from Debian Policy 5.6.12 so we don't depend on python3-apt.
"""


def _split(version: str) -> tuple[int, str, str]:
    """Split a version into (epoch, upstream, revision)."""
    epoch = 0
    if ":" in version:
        head, _, version = version.partition(":")
        epoch = int(head)
    upstream, sep, revision = version.rpartition("-")
    if not sep:
        return epoch, revision, ""
    return epoch, upstream, revision


def _char_order(c: str) -> int:
    if c == "~":
        return -1
    if c.isalpha():
        return ord(c)
    return ord(c) + 256


def _verrevcmp(a: str, b: str) -> int:
    i = j = 0
    while i < len(a) or j < len(b):
        # Non-digit prefix: '~' sorts before end-of-string, letters before
        # other characters.
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = _char_order(a[i]) if i < len(a) else 0
            bc = _char_order(b[j]) if j < len(b) else 0
            if ac != bc:
                return ac - bc
            i += 1
            j += 1
        # Numeric part: skip leading zeroes, then longest run wins, else
        # first differing digit.
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        first_diff = 0
        while i < len(a) and j < len(b) and a[i].isdigit() and b[j].isdigit():
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first_diff:
            return first_diff
    return 0


def compare(v1: str, v2: str) -> int:
    """Return <0 if v1 < v2, 0 if equal, >0 if v1 > v2."""
    e1, u1, r1 = _split(v1)
    e2, u2, r2 = _split(v2)
    if e1 != e2:
        return e1 - e2
    rc = _verrevcmp(u1, u2)
    if rc:
        return rc
    return _verrevcmp(r1, r2)


def newer(v1: str, v2: str) -> bool:
    """True if v1 is strictly newer than v2."""
    return compare(v1, v2) > 0


def has_ubuntu_delta(version: str) -> bool:
    """True if the Ubuntu version carries a delta against Debian."""
    return "ubuntu" in version
