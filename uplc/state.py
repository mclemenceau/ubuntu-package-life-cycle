"""Derive a lifecycle state per package from the ingested facts.

Pure functions — no network, no database — so the pipeline logic is unit
testable in isolation.
"""

from dataclasses import dataclass, field

from .debversion import has_ubuntu_delta, newer
from .sources import Excuse

# Funnel order: earlier means "more stuck / more actionable".
STATES = [
    "blocked-build",      # missing builds / FTBFS in -proposed
    "blocked-tests",      # autopkgtest regressions
    "blocked-depends",    # waiting on another package's migration
    "blocked-other",      # freeze block, block bug, unsatisfiable depends...
    "waiting-age",        # only waiting for the age policy
    "ready-to-migrate",   # valid candidate, migrates on next britney run
    "merge-needed",       # Debian is newer, Ubuntu delta to remerge
    "sync-available",     # Debian is newer, no Ubuntu delta
    "ubuntu-only",        # no Debian counterpart
    "in-sync",            # devel is current vs Debian
    "not-in-devel",       # team package absent from the devel series
]

BLOCKED_STATES = {"blocked-build", "blocked-tests", "blocked-depends", "blocked-other"}
PROPOSED_STATES = BLOCKED_STATES | {"waiting-age", "ready-to-migrate"}


@dataclass
class PackageState:
    package: str
    state: str
    ubuntu_version: str = ""
    debian_version: str = ""
    proposed_version: str = ""
    blocked_by: list = field(default_factory=list)
    regressions: list = field(default_factory=list)
    missing_builds: list = field(default_factory=list)
    bugs: list = field(default_factory=list)
    age_days: float | None = None
    summary: str = ""


def _proposed_state(excuse: Excuse) -> tuple[str, str]:
    """Map an aggregated excuse to (state, summary)."""
    reasons = excuse.reasons
    if excuse.is_candidate and not reasons:
        return "ready-to-migrate", "valid candidate, migrates on next britney run"
    if "missingbuild" in reasons:
        arches = ", ".join(excuse.missing_builds) or "some architectures"
        return "blocked-build", f"missing builds on {arches}"
    if "autopkgtest" in reasons:
        n = len(excuse.regressions)
        sample = "; ".join(excuse.regressions[:3])
        more = f" (+{n - 3} more)" if n > 3 else ""
        return "blocked-tests", f"{n} autopkgtest regression(s): {sample}{more}"
    if "depends" in reasons or excuse.blocked_by:
        by = ", ".join(sorted(excuse.blocked_by)) or "dependencies"
        return "blocked-depends", f"blocked by {by}"
    if reasons == {"age"}:
        return "waiting-age", "waiting for age policy"
    return "blocked-other", "blocked: " + (", ".join(sorted(reasons)) or "see excuses")


def derive_state(
    package: str,
    excuse: Excuse | None,
    ubuntu_version: str | None,
    debian_version: str | None,
    mom_entry: dict | None = None,
) -> PackageState:
    """Combine all facts about one package into a lifecycle state."""
    ps = PackageState(
        package=package,
        state="in-sync",
        ubuntu_version=ubuntu_version or "",
        debian_version=debian_version or "",
    )

    # The excuses file and the archive indexes are generated at different
    # times; if the release pocket already carries the proposed version,
    # the migration completed after excuses were generated.
    already_migrated = (
        excuse is not None and excuse.new_version and ubuntu_version
        and not newer(excuse.new_version, ubuntu_version))

    if excuse is not None and not already_migrated:
        ps.state, ps.summary = _proposed_state(excuse)
        ps.proposed_version = excuse.new_version
        ps.blocked_by = sorted(excuse.blocked_by)
        ps.regressions = sorted(set(excuse.regressions))
        ps.missing_builds = list(excuse.missing_builds)
        ps.bugs = sorted(set(excuse.bugs))
        ps.age_days = excuse.age_days
        return ps

    if not ubuntu_version:
        ps.state = "not-in-devel"
        ps.summary = "not published in the devel series"
        return ps

    if debian_version and newer(debian_version, ubuntu_version):
        if has_ubuntu_delta(ubuntu_version):
            ps.state = "merge-needed"
            ps.summary = f"Debian has {debian_version}, Ubuntu delta needs a merge"
        else:
            ps.state = "sync-available"
            ps.summary = f"Debian has {debian_version}, syncable"
        if mom_entry:
            ps.summary += f" (MoM base {mom_entry.get('base_version', '?')})"
        return ps

    if not debian_version:
        ps.state = "ubuntu-only"
        ps.summary = "no counterpart in Debian unstable"
        return ps

    ps.summary = "devel is current vs Debian unstable"
    return ps


def derive_all(
    packages: list[str],
    excuses: dict[str, Excuse],
    ubuntu_versions: dict[str, str],
    debian_versions: dict[str, str],
    mom: dict[str, dict] | None = None,
) -> list[PackageState]:
    mom = mom or {}
    return [
        derive_state(
            pkg,
            excuses.get(pkg),
            ubuntu_versions.get(pkg),
            debian_versions.get(pkg),
            mom.get(pkg),
        )
        for pkg in packages
    ]
