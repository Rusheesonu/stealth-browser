"""Residential / datacenter proxy pool — picks proxies for the browser to route through.

Two independent tiers:

  - Datacenter pool: configure via PROXIES_JSON env var (or a `proxies.json`
    file under the package data dir) AND set PROXIES_ENABLED=true. Cheap,
    fast, but easily flagged on vendors that gate on IP reputation.

  - Residential pool: configure via RESIDENTIAL_PROXIES_JSON env var.
    No global enable flag — independent of the datacenter pool. Routed
    automatically by `pick_for_vendor()` for IP-rep-sensitive vendors
    (cloudflare / imperva / akamai / datadome).

JSON shape (both tiers):
    {"credentials": [{"user": "...", "pass": "..."}],
     "endpoints":  [{"host": "...", "port": 1234}]}

Pure helpers — no side effects beyond reading env / a single optional
file. The browser pool (`browser.py`) calls these.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

# Optional on-disk config. When the package is pip-installed, this path
# usually points inside site-packages where users won't write — the env
# var (PROXIES_JSON) is the expected production path. The file is a dev
# convenience and a no-op when missing.
DATA_DIR = Path(__file__).resolve().parent / "data"
PROXIES_PATH = DATA_DIR / "proxies.json"


@lru_cache(maxsize=1)
def _config() -> dict:
    """Load proxy config. Env var `PROXIES_JSON` wins (for production /
    container deploys where the file isn't shipped), then falls back to
    the local file (dev convenience). Returns empty schema if neither
    exists."""
    env_json = os.getenv("PROXIES_JSON", "").strip()
    if env_json:
        try:
            return json.loads(env_json)
        except json.JSONDecodeError as e:
            log.warning("PROXIES_JSON env var invalid JSON: %s", e)
    if PROXIES_PATH.exists():
        try:
            return json.loads(PROXIES_PATH.read_text())
        except json.JSONDecodeError as e:
            log.warning("%s invalid JSON: %s", PROXIES_PATH, e)
    return {"credentials": [], "endpoints": []}


def available() -> bool:
    """True only when both a proxy config exists AND PROXIES_ENABLED=true.

    Opt-in by design: local dev should never accidentally route through a
    residential proxy that won't work from the dev's network. Production
    flips PROXIES_ENABLED=true on the deployment secret panel."""
    if os.getenv("PROXIES_ENABLED", "").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    cfg = _config()
    return bool(cfg.get("credentials")) and bool(cfg.get("endpoints"))


def pick_random() -> str | None:
    """A random `http://user:pass@host:port` URL, or None if pool empty."""
    cfg = _config()
    creds = cfg.get("credentials", [])
    endpoints = cfg.get("endpoints", [])
    if not creds or not endpoints:
        return None
    c = random.choice(creds)
    e = random.choice(endpoints)
    return f"http://{c['user']}:{c['pass']}@{e['host']}:{e['port']}"


def pick_for_user(user_id: str) -> str | None:
    """Deterministic per-user proxy endpoint (same user → same egress IP).
    Credentials still rotate per session to avoid rate-limit attribution
    across long-lived sessions."""
    cfg = _config()
    creds = cfg.get("credentials", [])
    endpoints = cfg.get("endpoints", [])
    if not creds or not endpoints:
        return None
    h = int(hashlib.sha256(user_id.encode("utf-8")).hexdigest(), 16)
    e = endpoints[h % len(endpoints)]
    c = random.choice(creds)
    return f"http://{c['user']}:{c['pass']}@{e['host']}:{e['port']}"


def host_port_user_pass() -> tuple[str, int, str, str] | None:
    """Same as pick_random but returns the 4-tuple unpacked — useful when
    nodriver wants the host+port for --proxy-server and the user+pass for
    a CDP `Fetch.authRequired` handler."""
    cfg = _config()
    creds = cfg.get("credentials", [])
    endpoints = cfg.get("endpoints", [])
    if not creds or not endpoints:
        return None
    c = random.choice(creds)
    e = random.choice(endpoints)
    return e["host"], e["port"], c["user"], c["pass"]


def all_endpoints() -> list[dict]:
    """Endpoints without credentials — for health/status views."""
    return list(_config().get("endpoints", []))


# ── Residential proxy tier ───────────────────────────────────────────────
# When a Bright Data / Oxylabs / Webshare-Residential plan is wired in,
# set RESIDENTIAL_PROXIES_JSON to the same shape as PROXIES_JSON.
#
# Engines call pick_for_vendor() when vendor_hint is in a residential-
# preferred set (cloudflare/imperva/akamai/datadome). Falls back to None
# (caller uses the datacenter pool or no proxy) when not configured.
#
# Cost note: residential = 50-100× datacenter, so we DON'T always use it.
# Only when the vendor specifically flags datacenter IPs.


_RESIDENTIAL_VENDORS = frozenset({
    "cloudflare",
    "cloudflare-turnstile",
    "imperva",
    "akamai",
    "datadome",
    # PerimeterX is behavioral, not IP-rep — residential won't help.
    # Kasada targets the headless engine itself — same.
})


@lru_cache(maxsize=1)
def _residential_config() -> dict:
    """Load residential proxy config. Mirrors _config() but reads
    RESIDENTIAL_PROXIES_JSON (separate env var → separate plan)."""
    env_json = os.getenv("RESIDENTIAL_PROXIES_JSON", "").strip()
    if env_json:
        try:
            return json.loads(env_json)
        except json.JSONDecodeError as e:
            log.warning("RESIDENTIAL_PROXIES_JSON invalid JSON: %s", e)
    return {"credentials": [], "endpoints": []}


def residential_available() -> bool:
    """True iff a residential plan is configured. Independent of the
    datacenter pool's PROXIES_ENABLED gate — operators may want to
    enable residential without flipping the global proxy toggle."""
    cfg = _residential_config()
    return bool(cfg.get("credentials")) and bool(cfg.get("endpoints"))


def pick_residential() -> str | None:
    """Random `http://user:pass@host:port` from the residential pool,
    or None if pool empty. Same shape as pick_random() so engines can
    swap pools transparently."""
    cfg = _residential_config()
    creds = cfg.get("credentials", [])
    endpoints = cfg.get("endpoints", [])
    if not creds or not endpoints:
        return None
    c = random.choice(creds)
    e = random.choice(endpoints)
    return f"http://{c['user']}:{c['pass']}@{e['host']}:{e['port']}"


def pick_for_vendor(vendor: str | None) -> str | None:
    """Returns a residential proxy URL when the vendor is in the
    IP-reputation-sensitive set AND a residential plan is configured.
    Falls back to pick_random() (datacenter) otherwise. Returns None
    if no pool at all.

    Engines pass their `requirements.vendor_hint` to this function so
    the right tier wins per scrape — datacenter for vendors that don't
    care about IP reputation (perimeterx, kasada), residential for those
    that gate hard on it (cloudflare, imperva, akamai, datadome)."""
    if vendor and vendor in _RESIDENTIAL_VENDORS and residential_available():
        return pick_residential()
    if available():
        return pick_random()
    return None
