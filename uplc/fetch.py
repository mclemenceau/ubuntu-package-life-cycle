"""Polite cached HTTP fetching.

Every fetch is a conditional GET (ETag / If-Modified-Since) against an
on-disk cache, so re-running the ingester downloads nothing that hasn't
changed. If a source is unreachable, the cached copy is served stale with
a warning — the pipeline degrades instead of failing.
"""

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("uplc.fetch")

USER_AGENT = "uplc/0.1 (Ubuntu package life cycle observer)"


class SourceUnavailable(Exception):
    """The URL could not be fetched and no cached copy exists."""


def cache_dir() -> Path:
    override = os.environ.get("UPLC_CACHE")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
    return Path(xdg) / "uplc"


def _cache_paths(url: str) -> tuple[Path, Path]:
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    base = cache_dir()
    return base / f"{key}.body", base / f"{key}.meta"


def fetch(url: str, *, timeout: int = 60, allow_stale: bool = True) -> bytes:
    """Fetch *url* with conditional-GET caching.

    Returns the response body (possibly from cache on 304 or network
    failure). Raises SourceUnavailable if the network fails and there is
    no cached copy.
    """
    body_path, meta_path = _cache_paths(url)
    meta = {}
    if meta_path.exists() and body_path.exists():
        meta = json.loads(meta_path.read_text())

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if meta.get("etag"):
        req.add_header("If-None-Match", meta["etag"])
    if meta.get("last_modified"):
        req.add_header("If-Modified-Since", meta["last_modified"])

    log.info("fetching %s", url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            body_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_bytes(body)
            meta_path.write_text(json.dumps({
                "url": url,
                "etag": resp.headers.get("ETag"),
                "last_modified": resp.headers.get("Last-Modified"),
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }))
            log.info("fetched %s (%d bytes)", url, len(body))
            return body
    except urllib.error.HTTPError as err:
        if err.code == 304:
            log.info("not modified, using cache: %s", url)
            return body_path.read_bytes()
        if allow_stale and body_path.exists():
            log.warning("HTTP %d for %s, using stale cache", err.code, url)
            return body_path.read_bytes()
        raise SourceUnavailable(f"{url}: HTTP {err.code}") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        if allow_stale and body_path.exists():
            log.warning("unreachable (%s), using stale cache: %s", err, url)
            return body_path.read_bytes()
        raise SourceUnavailable(f"{url}: {err}") from err


def fetch_first(urls: list[str], *, timeout: int = 60) -> bytes:
    """Try each URL in order, returning the first success."""
    last: Exception | None = None
    for url in urls:
        try:
            return fetch(url, timeout=timeout)
        except SourceUnavailable as err:
            last = err
    raise SourceUnavailable(f"all mirrors failed: {last}")
