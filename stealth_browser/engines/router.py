"""EngineRouter — intelligent multi-engine dispatcher.

Decision flow (in order):

  1. Capability filter — drop engines that can't satisfy Requirements.
  2. Cost cap — drop engines whose per-request cost exceeds budget.
  3. Per-host history — if engine X succeeded on this host last time, prefer it.
  4. Per-vendor preference — if vendor_hint is set, use the VENDOR_AFFINITY map
     to bias toward engines known to beat that vendor.
  5. Cost ascending — among ties, pick cheapest.
  6. Escalate on failure — if chosen engine raises EngineFailedError with
     retriable_on_other_engine=True, try the next candidate. Up to 3 engines.

Learning: every successful + failed snapshot updates an in-process
SuccessTracker keyed by (host, engine_name). Cheap O(1) dict, persisted
to disk via `dump()` so it survives restarts. Router learns over time
which engine works best on which host.

NOT YET (future iters):
  - Sticky session per cookie jar (Phase 2.2)
  - Confidence scores ("85% sure curl_cffi works for this URL")
  - Concurrency caps per engine (multi-browser pool work)
  - Cost telemetry export to /usage dashboard
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .base import (
    Capability,
    Engine,
    EngineFailedError,
    EngineSnapshotResult,
    EngineUnavailableError,
    Requirements,
)


log = logging.getLogger(__name__)


# Per-vendor known-good engine preference. When a request comes in with
# vendor_hint set (from detect.py on a prior attempt, or from per-host
# history), the router boosts these engines to the front of the candidate
# list. Order = strongest first.
#
# Sourced from web research (May 2026) + bench data we'll accumulate.
# Update as we learn. Only engines that actually exist in this codebase
# go here — historic comments referenced "patchright" but that engine
# isn't implemented yet; left out to avoid silent affinity-list filtering
# that hid the absence.
VENDOR_AFFINITY: dict[str, list[str]] = {
    # Cloudflare: nodriver clears basic challenges; camoufox is the
    # fallback when Chromium-targeted detection (cdc_, navigator.webdriver
    # via Object.defineProperty introspection) kicks in.
    "cloudflare":           ["nodriver", "camoufox"],
    "cloudflare-turnstile": ["camoufox", "nodriver"],
    # DataDome: heavy on TLS fingerprint — curl_cffi (real-Chrome JA3)
    # actually beats it on static content. Browser fallback for SPAs.
    "datadome":             ["curl_cffi", "camoufox", "nodriver"],
    # PerimeterX: behavioral. Camoufox + humanize wins; 2captcha for hard.
    "perimeterx":           ["camoufox", "nodriver"],
    # Akamai BMP: TLS + H2 fingerprint critical. curl_cffi on static,
    # camoufox for JS-required pages.
    "akamai":               ["curl_cffi", "camoufox", "nodriver"],
    # Imperva: IP-reputation heavy; engine matters less than proxy quality.
    "imperva":              ["camoufox", "nodriver", "curl_cffi"],
    # Kasada: targets headless chromium specifically. Firefox via camoufox
    # is the canonical bypass.
    "kasada":               ["camoufox", "nodriver"],
    # Fingerprint test pages (creepjs, fingerprint.com, browserleaks):
    # camoufox is the only engine that scores clean on creepjs.
    "fingerprint-test":     ["camoufox", "nodriver"],
}


@dataclass
class EngineDecision:
    """Why the router picked the engine it did. Stored on the result for
    observability + future router-quality measurement."""
    chosen_engine: str
    candidate_engines: list[str]
    reason: str                  # human-readable explanation
    requirements: Requirements
    escalation_path: list[str] = field(default_factory=list)  # if we had to escalate


# ── Success tracker — per-host engine history ────────────────────────────


@dataclass
class HostStats:
    success: int = 0
    failure: int = 0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0

    @property
    def success_rate(self) -> float:
        n = self.success + self.failure
        return self.success / n if n else 0.5    # 50% prior


class SuccessTracker:
    """In-process (host, engine) → success/failure counts. Cheap O(1) lookups.
    Persists to disk on dump() so multi-process restarts retain learning."""

    def __init__(self, persist_path: Optional[Path] = None):
        self._stats: dict[tuple[str, str], HostStats] = defaultdict(HostStats)
        self._persist_path = persist_path
        if persist_path and persist_path.exists():
            self._load()

    def record(self, host: str, engine: str, *, success: bool) -> None:
        s = self._stats[(host, engine)]
        if success:
            s.success += 1
            s.last_success_at = time.time()
        else:
            s.failure += 1
            s.last_failure_at = time.time()

    def best_for(self, host: str, candidates: list[Engine]) -> Optional[Engine]:
        """Among `candidates`, return the one with the highest success rate
        on `host`, or None if no history exists for any candidate."""
        with_history = [
            (c, self._stats[(host, c.name)])
            for c in candidates
            if (host, c.name) in self._stats
        ]
        if not with_history:
            return None
        # Sort by success rate desc, then by total attempts desc as tiebreak
        with_history.sort(
            key=lambda x: (x[1].success_rate, x[1].success + x[1].failure),
            reverse=True,
        )
        return with_history[0][0]

    def stats_for(self, host: str, engine_name: str) -> Optional[HostStats]:
        """Public accessor — used by EngineRouter._explain_choice. Returns
        None if no history exists yet so callers can branch on presence."""
        return self._stats.get((host, engine_name))

    def dump(self) -> None:
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {f"{h}|{e}": asdict(s) for (h, e), s in self._stats.items()}
            self._persist_path.write_text(json.dumps(payload, indent=2))
        except Exception as e:
            log.warning("SuccessTracker dump failed: %r", e)

    def _load(self) -> None:
        try:
            data = json.loads(self._persist_path.read_text())
            for k, v in data.items():
                h, e = k.split("|", 1)
                self._stats[(h, e)] = HostStats(**v)
        except Exception as e:
            log.warning("SuccessTracker load failed: %r", e)


# ── Router ───────────────────────────────────────────────────────────────


class EngineRouter:
    """The brain. Pick + escalate engines per request."""

    MAX_ESCALATIONS = 3        # try at most N engines before giving up

    def __init__(self, persist_dir: Optional[Path] = None) -> None:
        self._engines: list[Engine] = []
        # Default persist location — created on first dump
        default = Path("/tmp/stealth-scraper-router-history.json")
        self._tracker = SuccessTracker(persist_path=persist_dir or default)

    # ── Engine registration ──────────────────────────────────────────────

    def register(self, engine: Engine) -> None:
        """Add an engine to the candidate pool. Order of registration
        doesn't matter — selection is rule-based, not list-order based."""
        # Avoid double-registration
        if any(e.name == engine.name for e in self._engines):
            log.info("router: engine %s already registered, skipping", engine.name)
            return
        self._engines.append(engine)
        log.info(
            "router: registered %s (caps=%s, cost=%dc)",
            engine.name, engine.capabilities, engine.cost_per_request_cents,
        )

    def list_engines(self) -> list[str]:
        """For observability."""
        return [e.name for e in self._engines]

    # ── Pick + escalate ──────────────────────────────────────────────────

    async def snapshot(
        self,
        url: str,
        *,
        requirements: Optional[Requirements] = None,
    ) -> tuple[EngineSnapshotResult, EngineDecision]:
        """Pick the best engine, run it, escalate on failure. Returns
        the result + the decision metadata so callers can log/analyze."""
        req = requirements or Requirements()
        candidates = await self._available_candidates(req)
        if not candidates:
            raise EngineUnavailableError(
                f"no engines satisfy requirements: caps={req.required_caps()!r}, "
                f"max_cost={req.max_cost_cents}c. Registered: {self.list_engines()}"
            )

        host = self._host_of(url)
        ordered = self._rank(candidates, host=host, vendor_hint=req.vendor_hint)

        decision = EngineDecision(
            chosen_engine=ordered[0].name,
            candidate_engines=[e.name for e in candidates],
            requirements=req,
            reason=self._explain_choice(ordered[0], host, req),
        )

        # Try up to MAX_ESCALATIONS engines in ranked order.
        # We dump tracker state once at the end — not on every record —
        # to keep the hot path off synchronous file I/O. The dump-on-exit
        # still survives crashes well enough for an in-process learner.
        last_err: Optional[Exception] = None
        try:
            for attempt, engine in enumerate(ordered[: self.MAX_ESCALATIONS]):
                try:
                    result = await engine.snapshot(url, requirements=req)
                    self._tracker.record(host, engine.name, success=True)
                    if attempt > 0:
                        decision.escalation_path.append(
                            f"{ordered[attempt-1].name}→fail, {engine.name}→success"
                        )
                    return result, decision
                except EngineFailedError as e:
                    self._tracker.record(host, engine.name, success=False)
                    last_err = e
                    if not e.retriable_on_other_engine:
                        decision.escalation_path.append(f"{engine.name}→non-retriable: {e}")
                        raise
                    decision.escalation_path.append(f"{engine.name}→fail: {str(e)[:80]}")
                    log.info("router: %s failed on %s — escalating", engine.name, url)
                    continue
                except Exception as e:
                    # Unexpected — log + escalate anyway, but mark in decision
                    self._tracker.record(host, engine.name, success=False)
                    last_err = e
                    decision.escalation_path.append(f"{engine.name}→exception: {type(e).__name__}")
                    log.warning("router: %s raised %r on %s", engine.name, e, url)
                    continue
            raise EngineFailedError(
                f"all {len(ordered[: self.MAX_ESCALATIONS])} candidate engines failed: "
                f"{decision.escalation_path}",
                engine="router",
                retriable_on_other_engine=False,
            ) from last_err
        finally:
            self._tracker.dump()

    # ── Internals ────────────────────────────────────────────────────────

    async def _available_candidates(self, req: Requirements) -> list[Engine]:
        """Filter to engines that (a) advertise required capabilities,
        (b) fit cost budget, (c) pass is_available() health check."""
        needed = req.required_caps()
        cands: list[Engine] = []
        for e in self._engines:
            if (e.capabilities & needed) != needed:
                continue
            if e.cost_per_request_cents > req.max_cost_cents:
                continue
            try:
                if not await e.is_available():
                    continue
            except Exception as ex:
                log.warning("router: %s is_available() raised %r", e.name, ex)
                continue
            cands.append(e)
        return cands

    def _rank(
        self,
        candidates: list[Engine],
        *,
        host: str,
        vendor_hint: Optional[str],
    ) -> list[Engine]:
        """Return candidates in best-first order."""
        # Step 1: per-host history wins if we have any
        historical_best = self._tracker.best_for(host, candidates)

        # Step 2: vendor-affinity ordering
        if vendor_hint and vendor_hint in VENDOR_AFFINITY:
            preferred_names = VENDOR_AFFINITY[vendor_hint]
            by_name = {e.name: e for e in candidates}
            ordered = [by_name[n] for n in preferred_names if n in by_name]
            # Append any candidates not in the affinity list, cheapest first
            remaining = sorted(
                [e for e in candidates if e.name not in preferred_names],
                key=lambda e: e.cost_per_request_cents,
            )
            ordered.extend(remaining)
        else:
            # Step 3: no vendor hint → cheapest first
            ordered = sorted(candidates, key=lambda e: e.cost_per_request_cents)

        # Step 4: per-host historical best floats to head if present
        if historical_best:
            ordered = [historical_best] + [e for e in ordered if e is not historical_best]

        return ordered

    def _explain_choice(self, engine: Engine, host: str, req: Requirements) -> str:
        """Human-readable rationale for the decision. Goes on EngineDecision."""
        parts = [f"chose '{engine.name}'"]
        s = self._tracker.stats_for(host, engine.name)
        if s is not None:
            parts.append(
                f"per-host history: {s.success}/{s.success + s.failure} = {s.success_rate:.0%}"
            )
        if req.vendor_hint and req.vendor_hint in VENDOR_AFFINITY:
            pref = VENDOR_AFFINITY[req.vendor_hint]
            if engine.name in pref:
                rank = pref.index(engine.name) + 1
                parts.append(f"vendor-affinity rank {rank}/{len(pref)} for {req.vendor_hint}")
        parts.append(f"cost {engine.cost_per_request_cents}c")
        return "; ".join(parts)

    @staticmethod
    def _host_of(url: str) -> str:
        try:
            return (urlparse(url).hostname or url).lower()
        except Exception:
            return url
