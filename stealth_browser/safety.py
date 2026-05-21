"""Safety primitives — robots.txt + per-domain rate limiter.

Win-condition v2 §8 requires both ON BY DEFAULT. This module owns both
so we have one canonical place to gate every outbound scrape.

  - `robots_check(url)` returns whether the URL is allowed for our UA.
    Honors robots.txt by default. Caller can opt out via `override=True`
    (mainly for first-party scraping where you own the site). Caches
    parsed robots.txt per host for 1 hour.

  - `RateLimiter.acquire(host)` waits until the host's per-second token
    budget allows another request. Default: 1 req/sec/host, burst 3.
    Configurable per call. Async-safe. Prevents accidentally DDoSing a
    target by running 100 concurrent scrapes against the same domain.

Both fail-safe: if robots.txt can't be fetched, we ALLOW (don't block on
flake). If the rate limiter sees no config, it uses defaults. Production
will set tighter limits via env vars.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx


log = logging.getLogger(__name__)

# User-agent we identify as when fetching robots.txt + scraping. Matches
# what stealth.py spoofs at the JS level so the two layers agree.
SCRAPER_UA = os.getenv(
    "SCRAPER_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

# Defaults — opinionated, can DoS NOBODY at these settings.
DEFAULT_QPS = float(os.getenv("SCRAPER_DEFAULT_QPS", "1.0"))      # 1 request per second per host
DEFAULT_BURST = int(os.getenv("SCRAPER_DEFAULT_BURST", "3"))      # allow 3 in-flight before rate-limiting kicks in
ROBOTS_TIMEOUT_S = 5.0                                              # don't block forever if a slow site has slow robots.txt
ROBOTS_TTL_S = 3600.0                                               # re-fetch hourly


# ── robots.txt ───────────────────────────────────────────────────────────


@dataclass
class _RobotsEntry:
    parser: RobotFileParser
    expires_at: float


_robots_cache: dict[str, _RobotsEntry] = {}
_robots_lock = asyncio.Lock()


async def _fetch_robots(host_root: str) -> RobotFileParser:
    """Fetch + parse robots.txt for the given http(s)://host root.

    Returns a RobotFileParser. On any error we return an empty parser
    that allows everything — safer to scrape than to crash the whole
    pipeline because a target's robots.txt 503'd."""
    rp = RobotFileParser()
    url = host_root.rstrip("/") + "/robots.txt"
    try:
        async with httpx.AsyncClient(
            timeout=ROBOTS_TIMEOUT_S,
            headers={"User-Agent": SCRAPER_UA},
            follow_redirects=True,
        ) as c:
            r = await c.get(url)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            elif r.status_code in (401, 403):
                # RFC 9309: 401/403 means "everything disallowed"
                rp.parse(["User-agent: *", "Disallow: /"])
            # 404 / 5xx / network errors — empty parser = allow all.
    except Exception as e:
        log.debug("robots.txt fetch failed for %s: %r — treating as allow-all", host_root, e)
    return rp


async def robots_check(url: str, *, override: bool = False) -> tuple[bool, str]:
    """Is `url` allowed by robots.txt for our user-agent?

    Returns (allowed, reason). `reason` is a short string suitable for
    logging or a "you tried to scrape a disallowed URL" error.

    `override=True` bypasses the check entirely (returns allowed=True).
    Reserved for first-party scraping where the caller owns the site.

    Cheap — per-host robots.txt cached for 1 hour."""
    if override:
        return True, "override"
    p = urlparse(url)
    if not p.hostname:
        return False, "no hostname"
    host_root = f"{p.scheme}://{p.hostname}"
    async with _robots_lock:
        now = time.time()
        entry = _robots_cache.get(host_root)
        if entry is None or entry.expires_at < now:
            parser = await _fetch_robots(host_root)
            _robots_cache[host_root] = _RobotsEntry(
                parser=parser, expires_at=now + ROBOTS_TTL_S
            )
            entry = _robots_cache[host_root]
    try:
        allowed = entry.parser.can_fetch(SCRAPER_UA, url)
    except Exception:
        # Malformed robots.txt — allow.
        return True, "robots.txt unparseable, allowing"
    if allowed:
        return True, "allowed"
    return False, "disallowed by robots.txt"


# ── Per-host rate limiter ────────────────────────────────────────────────


class RateLimiter:
    """Token-bucket rate limiter, one bucket per hostname.

    `acquire(host)` returns when the host's bucket has at least one token
    free. Tokens refill at `qps` per second. `burst` is the bucket size
    (max in-flight before throttling kicks in).

    The bucket math:
      bucket[host] = deque of recent acquire timestamps within 1/qps window
      acquire(host):
        - prune timestamps older than 1/qps from the deque
        - if len(deque) < burst: append now, return immediately
        - else: sleep until oldest entry expires, retry

    Async-safe: a single asyncio.Lock guards the deque dict. The actual
    SLEEP happens outside the lock so other hosts can proceed in parallel.
    """

    def __init__(self, qps: float = DEFAULT_QPS, burst: int = DEFAULT_BURST) -> None:
        self.qps = qps
        self.burst = burst
        self._window_s = 1.0 / qps if qps > 0 else 0.0
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def acquire(self, host_or_url: str) -> None:
        """Block until the host/URL's bucket has a free token. URL works
        too — we hash by hostname."""
        host = host_or_url
        if "://" in host_or_url:
            try:
                host = urlparse(host_or_url).hostname or host_or_url
            except Exception:
                pass
        if not host or self.qps <= 0:
            return

        # Spin-loop with bounded sleeps so we don't hold the lock during sleep.
        while True:
            async with self._lock:
                now = time.monotonic()
                bucket = self._buckets[host]
                cutoff = now - max(self._window_s, 1.0)
                while bucket and bucket[0] < cutoff:
                    bucket.popleft()
                if len(bucket) < self.burst:
                    bucket.append(now)
                    return
                wait = max(0.0, bucket[0] + self._window_s - now)
            # Sleep outside the lock so other hosts aren't blocked.
            if wait > 0:
                await asyncio.sleep(wait)
            else:
                # Defensive: shouldn't happen, but avoid tight loop.
                await asyncio.sleep(0.001)

    def stats(self) -> dict:
        """For observability: how many in-flight slots per host right now."""
        return {h: len(b) for h, b in self._buckets.items()}


# Module-level singleton — the production scraper imports this.
limiter = RateLimiter()


# ── Convenience: combined gate ───────────────────────────────────────────


@dataclass
class SafetyCheck:
    """One-call helper: 'can I scrape this URL right now?' Combines
    robots check + rate limit acquire. Returns when both are satisfied,
    or raises if robots disallows."""
    url: str
    override_robots: bool = False

    async def __aenter__(self) -> "SafetyCheck":
        allowed, reason = await robots_check(self.url, override=self.override_robots)
        if not allowed:
            raise PermissionError(
                f"Scrape blocked: {reason} for {self.url} "
                f"(set override_robots=True to bypass — and read your law)"
            )
        await limiter.acquire(self.url)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # No release — rate limiter uses time-window decay, not token return.
        # robots.txt cache is hourly.
        return None
