"""Optional package -> sub-team ("group") mapping for the dashboard.

uplc ships with no groups of its own -- this is a purely local, opt-in
overlay. Teams that split ownership across squads/pods/whatever can point
--groups at a YAML file mapping group name -> package list; packages.html
then gets a Group column and filter chips. Without the flag (or with a
missing/empty file) the dashboard renders exactly as it does without this
feature, same as any other optional source in this codebase (MoM,
pending-SRU).

The mapping itself is deliberately never bundled or fetched from a URL:
who owns what internally is the team's data, not uplc's.
"""

import logging
from pathlib import Path

import yaml

log = logging.getLogger("uplc.groups")


def load_groups(path) -> dict[str, list[str]]:
    """Parse {group: [package, ...]} YAML into {package: [group, ...]}.

    Returns {} when `path` is falsy or doesn't exist -- callers treat that
    as "no grouping configured" rather than an error.
    """
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        log.warning("groups file %s not found; dashboard will not show groups", p)
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    by_package: dict[str, list[str]] = {}
    for group, packages in data.items():
        for pkg in packages or []:
            by_package.setdefault(pkg, []).append(group)
    return {pkg: sorted(groups) for pkg, groups in by_package.items()}
