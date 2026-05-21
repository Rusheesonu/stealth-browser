"""Engine protocol + shared types.

Every concrete engine (nodriver, curl_cffi, patchright, camoufox, …)
implements this protocol and gets registered with the router. The
router treats them as interchangeable except for the metadata they
expose (capabilities, cost, availability).

Design choices defended:

  - Protocol > ABC. Plugin authors can ship third-party engines without
    importing our package. Type-checkers still flag missing methods.

  - `EngineSnapshotResult` (not the production SnapshotResult): isolates
    the engine layer from the production data model. Each engine returns
    EnSR; the router converts to production SnapshotResult at the edge.
    Keeps engine devs from accidentally depending on production shape.

  - Capabilities as an IntFlag: cheap bitwise comparison for "this engine
    has the capabilities I need". Easier than a set of strings.

  - Cost in cents (int). Avoids float precision games when summing
    per-iter cost telemetry. 1.5¢/page = 150 (in hundredths of a cent).

  - Requirements is a dataclass not a TypedDict because Python 3.10
    runtime needs the type. Backwards-compat with our 3.10+ floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntFlag, auto
from typing import Any, Optional, Protocol, runtime_checkable


# ── Capabilities ─────────────────────────────────────────────────────────


class Capability(IntFlag):
    """What an engine can do. Bitwise — engines OR these together to
    declare their feature set, requirements AND them to filter."""

    # Core
    JS_EXEC = auto()           # runs page JavaScript (not curl/HTTP-only)
    SCREENSHOT = auto()        # can produce a PNG/JPEG of the page
    DOM_QUERY = auto()         # can return element bbox/selector data

    # Anti-detection
    TLS_IMPERSONATION = auto() # sends a real-Chrome JA3/JA4 TLS handshake
    HTTP2_FINGERPRINT = auto() # h2 SETTINGS matches real-Chrome
    CDP_NATIVE = auto()        # uses CDP without exposing automation flags
    BEHAVIORAL = auto()        # mouse/keyboard simulation with timing
    FIREFOX_ENGINE = auto()    # NOT chromium — defeats chromium-targeted detectors

    # Performance / cost
    LIGHTWEIGHT = auto()       # <100MB RAM, <500ms per request
    HEADED = auto()            # runs headed (xvfb on Linux); minor stealth bonus
    MOBILE_EMULATION = auto()  # can present as iOS Safari / Android Chrome

    # Practical
    PROXY_SUPPORT = auto()     # honors proxy_url param
    COOKIE_PERSISTENCE = auto()# can reuse cookies across calls in a session


# ── Inputs ───────────────────────────────────────────────────────────────


@dataclass
class Requirements:
    """What the caller needs from the snapshot.

    The router uses this to filter candidate engines + rank them by
    fit. Defaults match the most common case (logged-out, generic site,
    just want JS-rendered DOM + screenshot).
    """
    needs_js: bool = True
    needs_screenshot: bool = True
    needs_dom: bool = True

    # Vendor hint — what we already know is blocking this URL. Comes
    # from prior detect.py result OR per-host history. Router uses it
    # to pick engines that have track record on this vendor.
    vendor_hint: Optional[str] = None      # "cloudflare" | "perimeterx" | ...

    # Behavioral requirements
    needs_interaction: bool = False        # mouse-clicks, form-fills
    needs_mobile_ui: bool = False           # iOS/Android-rendered page

    # Cost / latency budget (hard caps)
    max_cost_cents: int = 100              # 1$ per request hard cap
    max_latency_s: float = 30.0

    # Hints
    prefer_lightweight: bool = False       # prefer curl_cffi over browser
    require_tls_impersonation: bool = False # known TLS-fingerprinting vendor

    # Per-call session key — same key → router prefers same engine
    # (cookie continuity wins for Cloudflare/Akamai).
    session_key: Optional[str] = None

    def required_caps(self) -> Capability:
        """Build the minimum-capability mask the router will filter by."""
        c = Capability(0)
        if self.needs_js:               c |= Capability.JS_EXEC
        if self.needs_screenshot:       c |= Capability.SCREENSHOT
        if self.needs_dom:              c |= Capability.DOM_QUERY
        if self.needs_interaction:      c |= Capability.BEHAVIORAL
        if self.needs_mobile_ui:        c |= Capability.MOBILE_EMULATION
        if self.require_tls_impersonation: c |= Capability.TLS_IMPERSONATION
        return c


# ── Outputs ──────────────────────────────────────────────────────────────


@dataclass
class EngineSnapshotResult:
    """What every engine returns. Cross-engine normalized shape.

    Mirrors production SnapshotResult but with extra engine telemetry
    so the router can learn which engine works on which URL."""
    url: str
    title: str
    screenshot_base64: str = ""
    elements: list[dict[str, Any]] = field(default_factory=list)
    viewport: dict[str, int] = field(default_factory=dict)
    page: dict[str, int] = field(default_factory=dict)

    # Engine telemetry — populated by router on every call
    engine_name: str = ""
    elapsed_s: float = 0.0
    cost_cents: int = 0
    proxy_used: Optional[str] = None
    cookies_carried: int = 0
    notes: str = ""


# ── Errors ───────────────────────────────────────────────────────────────


class EngineUnavailableError(RuntimeError):
    """Raised when an engine's `is_available()` returns False (missing
    dependency, broken installation, etc.). Router skips and tries next."""


class EngineFailedError(RuntimeError):
    """Raised when an engine attempted a snapshot but failed in a way
    that suggests we should escalate to a DIFFERENT engine, not retry the
    same one. (Plain transient errors stay as Exception so retry logic
    in the engine itself can catch them.)"""

    def __init__(self, msg: str, *, engine: str, retriable_on_other_engine: bool = True):
        super().__init__(msg)
        self.engine = engine
        self.retriable_on_other_engine = retriable_on_other_engine


# ── The Engine protocol ──────────────────────────────────────────────────


@runtime_checkable
class Engine(Protocol):
    """A scraping engine. Implementations register with the router."""

    name: str                   # short identifier — "nodriver" | "curl_cffi" | ...
    capabilities: Capability    # what this engine can do (OR of Capability bits)
    cost_per_request_cents: int # rough — for router cost-aware ranking

    async def is_available(self) -> bool:
        """Quick check: can this engine actually run on this host?
        (missing pip dep, missing system binary, etc.)"""
        ...

    async def snapshot(
        self,
        url: str,
        *,
        requirements: Requirements,
    ) -> EngineSnapshotResult:
        """Drive ONE scrape against `url`. Raise EngineFailedError on
        engine-level failure to signal the router to try a different
        engine. Raise normal exceptions for caller-fault errors (bad
        URL, etc.)."""
        ...
