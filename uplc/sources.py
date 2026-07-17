"""Clients for the bulk-published archive artifacts uplc consumes.

None of these touch the Launchpad API. Each returns plain dicts so the
state engine stays testable without any network access.
"""

import json
import logging
import lzma
from dataclasses import dataclass, field

import yaml

try:
    from yaml import CSafeLoader as _YamlLoader
except ImportError:  # libyaml not available
    from yaml import SafeLoader as _YamlLoader

from .fetch import SourceUnavailable, fetch, fetch_first

log = logging.getLogger("uplc.sources")

TEAM_MAPPING_URLS = [
    # Canonical location (redirects to static-reports.ubuntu.com).
    "https://ubuntu-archive-team.ubuntu.com/package-team-mapping.json",
    # Sibling copy served directly from the archive-team host; useful when
    # the static-reports ingress is unreachable (occasionally flaky, not a
    # VPN/access-control issue).
    "https://ubuntu-archive-team.ubuntu.com/package-team-mapping.json.apw",
]
EXCUSES_URL = "https://ubuntu-archive-team.ubuntu.com/proposed-migration/update_excuses.yaml.xz"
EXCUSES_HTML = "https://ubuntu-archive-team.ubuntu.com/proposed-migration/update_excuses.html"
MOM_URL = "https://merges.ubuntu.com/{component}.json"
# sru-report (ubuntu-archive-tools) publishes this alongside pending-sru.html.
# Same static-reports.ubuntu.com ingress as MoM (occasionally flaky, no VPN
# required) -> optional source.
PENDING_SRU_URL = "https://static-reports.ubuntu.com/pending-sru/sru_report.yaml"
PENDING_SRU_HTML = "https://ubuntu-archive-team.ubuntu.com/pending-sru.html"
UBUNTU_SOURCES_URL = "http://archive.ubuntu.com/ubuntu/dists/devel/{component}/source/Sources.xz"
DEBIAN_SOURCES_URL = "https://deb.debian.org/debian/dists/unstable/main/source/Sources.xz"

UBUNTU_COMPONENTS = ["main", "universe", "restricted", "multiverse"]


def team_packages(team: str) -> list[str]:
    """Source packages a team is subscribed to, from package-team-mapping.

    Fallback only: lpbugs.subscribed_packages is the authoritative source.
    The .apw copy this usually resolves to froze in May 2025, and the
    primary redirects to static-reports.ubuntu.com (occasionally flaky).
    """
    mapping = json.loads(fetch_first(TEAM_MAPPING_URLS, timeout=30))
    if team not in mapping:
        raise KeyError(
            f"team {team!r} not in package-team-mapping "
            f"(known: {', '.join(sorted(mapping))})")
    return sorted(mapping[team])


def parse_sources_index(raw_xz: bytes) -> dict[str, str]:
    """Parse a Sources.xz index into {source_package: highest_version}."""
    from .debversion import newer

    versions: dict[str, str] = {}
    package = version = None
    for line in lzma.decompress(raw_xz).decode("utf-8", "replace").splitlines():
        if line.startswith("Package:"):
            package = line.split(":", 1)[1].strip()
        elif line.startswith("Version:"):
            version = line.split(":", 1)[1].strip()
        elif not line.strip():
            if package and version:
                if package not in versions or newer(version, versions[package]):
                    versions[package] = version
            package = version = None
    if package and version:
        if package not in versions or newer(version, versions[package]):
            versions[package] = version
    return versions


def ubuntu_devel_versions() -> dict[str, str]:
    """Source versions in the devel series release pocket, all components."""
    versions: dict[str, str] = {}
    for component in UBUNTU_COMPONENTS:
        raw = fetch(UBUNTU_SOURCES_URL.format(component=component), timeout=120)
        versions.update(parse_sources_index(raw))
    return versions


def debian_unstable_versions() -> dict[str, str]:
    """Source versions in Debian unstable main."""
    raw = fetch(DEBIAN_SOURCES_URL, timeout=120)
    return parse_sources_index(raw)


@dataclass
class Excuse:
    """Aggregated proposed-migration status for one source package.

    britney emits one entry per migration item (often per architecture);
    this collapses them to package level, keeping the worst outcome.
    """

    source: str
    old_version: str = ""
    new_version: str = ""
    is_candidate: bool = True
    reasons: set = field(default_factory=set)
    verdicts: set = field(default_factory=set)
    blocked_by: set = field(default_factory=set)
    regressions: list = field(default_factory=list)   # "tested-pkg/ver (arch)"
    missing_builds: list = field(default_factory=list)
    age_days: float | None = None
    bugs: list = field(default_factory=list)          # update-excuse / block bugs
    detail: list = field(default_factory=list)        # raw excuse text lines


def _merge_excuse_item(agg: Excuse, item: dict) -> None:
    agg.old_version = item.get("old-version", agg.old_version)
    agg.new_version = item.get("new-version", agg.new_version)
    agg.is_candidate = agg.is_candidate and bool(item.get("is-candidate"))
    agg.reasons.update(item.get("reason") or [])
    if item.get("migration-policy-verdict"):
        agg.verdicts.add(item["migration-policy-verdict"])
    deps = item.get("dependencies") or {}
    agg.blocked_by.update(deps.get("blocked-by") or [])
    agg.detail.extend(item.get("excuses") or [])

    policy = item.get("policy_info") or {}
    tests = policy.get("autopkgtest") or {}
    for tested, arches in tests.items():
        if tested == "verdict" or not isinstance(arches, dict):
            continue
        for arch, result in arches.items():
            status = result[0] if isinstance(result, (list, tuple)) and result else result
            if status == "REGRESSION":
                agg.regressions.append(f"{tested} ({arch})")
    age = policy.get("age") or {}
    if isinstance(age.get("current-age"), (int, float)):
        agg.age_days = max(agg.age_days or 0, age["current-age"])
    for key in ("update-excuse", "block-bugs"):
        bugs = policy.get(key)
        if isinstance(bugs, dict):
            agg.bugs.extend(b for b in bugs if str(b).isdigit())

    build = item.get("missing-builds") or {}
    for arch in build.get("on-architectures") or []:
        if arch not in agg.missing_builds:
            agg.missing_builds.append(arch)


def load_excuses() -> tuple[str, dict[str, Excuse]]:
    """Parse update_excuses.yaml.xz into per-source aggregates.

    Returns (generated_date, {source: Excuse}).
    """
    raw = fetch(EXCUSES_URL, timeout=120)
    log.info("parsing update_excuses.yaml (%.1f MiB compressed) ...",
             len(raw) / 2**20)
    data = yaml.load(lzma.decompress(raw), Loader=_YamlLoader)
    excuses: dict[str, Excuse] = {}
    for item in data.get("sources", []):
        source = item.get("source")
        if not source:
            continue
        agg = excuses.setdefault(source, Excuse(source=source))
        _merge_excuse_item(agg, item)
    generated = str(data.get("generated-date", ""))
    return generated, excuses


def mom_merges() -> dict[str, dict]:
    """Merge-o-Matic outstanding merges, {source: entry}.

    MoM (merges.ubuntu.com) is only reachable from some networks; when it
    isn't, we return {} and the Sources-index comparison still covers the
    'outdated vs Debian' signal.
    """
    merges: dict[str, dict] = {}
    for component in UBUNTU_COMPONENTS:
        try:
            raw = fetch(MOM_URL.format(component=component), timeout=15)
        except SourceUnavailable as err:
            log.warning("Merge-o-Matic unavailable (%s); "
                        "falling back to Sources comparison", err)
            return {}
        for entry in json.loads(raw):
            name = entry.get("source_package")
            if name:
                merges[name] = entry
    return merges


# cls tokens sru-report writes into each bug link's class attribute, in the
# priority order verification actually resolves in (see sru_report.yaml).
_SRU_STATUS_ORDER = (
    ("broken", "broken"),
    ("verificationfailed", "verification-failed"),
    ("verified", "verified"),
    ("removal", "removal-candidate"),
    ("incomplete", "incomplete"),
)


def bug_verification_status(cls: str) -> str:
    """One bug's SRU verification status for the series it was seen under.

    `cls` is sru-report's own per-row CSS class string (already computed
    against that specific series' verification-done-<series> tag), so this
    just reads it back rather than re-deriving anything from tags.
    """
    tokens = set((cls or "").split())
    for token, status in _SRU_STATUS_ORDER:
        if token in tokens:
            return status
    return "pending"


def parse_sru_report(raw_yaml: bytes) -> list[dict]:
    """Flatten sru_report.yaml into one dict per (package, series) row.

    Top level is {series: [package entries]}; each entry's `bugs` list
    carries per-bug verification status via `cls` (see
    bug_verification_status). Returns every series/package in the report —
    callers filter to their own team's packages.
    """
    data = yaml.load(raw_yaml, Loader=_YamlLoader) or {}
    rows = []
    for series, packages in data.items():
        for pkg in packages or []:
            rows.append({
                "package": pkg.get("pkg", ""),
                "series": series,
                "proposed_version": pkg.get("proposed_version", ""),
                "release_version": pkg.get("release_version", ""),
                "update_version": pkg.get("update_version", ""),
                "uploaders": pkg.get("uploaders", ""),
                "age_days": pkg.get("age"),
                "url": pkg.get("url", ""),
                "bugs": [{
                    "id": b.get("id"),
                    "description": b.get("description", ""),
                    "cls": b.get("cls", ""),
                    "tags": b.get("tags") or [],
                    "url": b.get("url", ""),
                } for b in pkg.get("bugs") or []],
            })
    return rows


def pending_sru() -> list[dict]:
    """The full pending-SRU report, across every series and package.

    Optional like mom_merges(): static-reports.ubuntu.com shares MoM's
    occasionally-flaky ingress (no VPN required, just unreliable at
    times), so an unreachable host degrades to [] rather than failing the
    run — SRU digest events then fall back to the stable-series-task
    approximation.
    """
    try:
        raw = fetch(PENDING_SRU_URL, timeout=30)
    except SourceUnavailable as err:
        log.warning("pending-sru report unavailable (%s)", err)
        return []
    return parse_sru_report(raw)
