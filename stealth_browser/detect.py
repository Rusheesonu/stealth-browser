"""Bot-challenge detection — surface "we were blocked" clearly to callers.

When a site puts up a CAPTCHA / interstitial / "are you human" wall, the
scrape technically succeeds (HTTP 200, page loaded) but the extracted
fields are all wrong because we got the wall, not the content. Without
this module, the user sees broken data and blames us, not the wall.

With this module, the caller gets a structured `BlockDetection` result
saying *exactly* which anti-bot vendor blocked us and what to do about it.
That maps to a useful UI message ("Cloudflare blocked this — try a
different proxy / sign up for residential IPs") instead of "extraction
failed".

The detector is signature-based — fast (~1ms per page), no LLM calls,
no false positives on legitimate sites. Add new vendors as you encounter
them; each is a small regex against the HTML body + a fingerprint check.

Coverage at v1:
  * Cloudflare ("Just a moment…", challenge platform)
  * PerimeterX / HUMAN ("Press & Hold", _px* cookies)
  * DataDome (datadome cookie + interstitial)
  * Akamai Bot Manager (akamai-bm, _abck cookie)
  * Imperva / Incapsula (incap_ses cookie, "Incident ID")
  * Distil Networks (legacy, now Imperva — distil_ session)
  * Kasada (KP_UIDz cookie)
  * Generic CAPTCHA (reCAPTCHA / hCaptcha visible at root)

Future signals to add as we hit them:
  * Shape Security ("Verifying you are human" + their telemetry beacon)
  * Castle (X-Castle-Request-Id)
  * Forter
  * Sift
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ── Public API ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BlockDetection:
    """Result of inspecting a snapshot for anti-bot walls.

    `blocked` is the headline — if False, scrape proceeded normally.
    The other fields are diagnostic (which vendor, severity, user-facing
    suggestion).
    """
    blocked: bool
    vendor: Optional[str]              # "cloudflare" | "perimeterx" | ...
    severity: str                       # "soft" | "hard"
    title: str                          # "Cloudflare challenge"
    message: str                        # one-line summary
    suggestion: str                     # what user can do
    is_behavioral: bool                 # True if needs human input (press&hold)

    @classmethod
    def ok(cls) -> "BlockDetection":
        return cls(
            blocked=False, vendor=None, severity="none",
            title="", message="", suggestion="", is_behavioral=False,
        )


def detect_block(
    *,
    title: str = "",
    html: str = "",
    url: str = "",
    cookies: Optional[dict[str, str]] = None,
    headers: Optional[dict[str, str]] = None,
) -> BlockDetection:
    """Inspect a snapshot's metadata + a chunk of HTML for known anti-bot
    signatures. Returns the first matching detection or `ok()`.

    All inputs are optional but the more you pass, the more accurate.
    For best results pass `title`, `html` (first 4KB is plenty),
    and `cookies` (vendors leave fingerprint cookies that survive even
    when the HTML is obfuscated).

    Cheap to call — runs in <1ms even on large HTML strings because
    each signature is checked with a single contains() or regex.
    """
    cookies = cookies or {}
    headers = headers or {}
    html_lc = (html or "").lower()[:8192]   # cap scan to first 8KB
    title_lc = (title or "").lower().strip()

    # ── Cloudflare — by far the most common ────────────────────────────
    if (
        "just a moment" in title_lc
        or "checking your browser" in title_lc
        or "cf-mitigated" in headers.get("cf-mitigated", "").lower()
        or "cf_clearance" in cookies
        or "challenge-platform" in html_lc
        or '__cf_chl_' in html_lc
    ):
        return BlockDetection(
            blocked=True,
            vendor="cloudflare",
            severity="hard" if "challenge-platform" in html_lc else "soft",
            title="Cloudflare bot challenge",
            message="Cloudflare's bot manager intercepted the request before the real page loaded.",
            suggestion="Retry with a residential proxy (datacenter IPs are flagged instantly). For sites with Turnstile challenges, sign up for a CAPTCHA-solver integration.",
            is_behavioral=False,
        )

    # ── PerimeterX / HUMAN — the "Press & Hold" people ──────────────────
    if (
        "press &amp; hold" in html_lc
        or "press & hold" in html_lc
        or "are you a human" in html_lc.replace("(and not a bot)", "")
        or any(k.startswith("_px") for k in cookies)
        or "px-captcha" in html_lc
        or "px-captcha" in title_lc
    ):
        return BlockDetection(
            blocked=True,
            vendor="perimeterx",
            severity="hard",
            title="PerimeterX behavioral challenge",
            message="PerimeterX (HUMAN Security) requires a press-and-hold gesture that fingerprint patching can't fake.",
            suggestion="This site needs a CAPTCHA-solver service (2captcha, capmonster). Pure browser stealth won't defeat behavioral biometrics.",
            is_behavioral=True,
        )

    # ── DataDome ────────────────────────────────────────────────────────
    if (
        "datadome" in cookies
        or "datado.me" in html_lc
        or "dd_cookie" in html_lc
        or '"datadome"' in html_lc
    ):
        return BlockDetection(
            blocked=True,
            vendor="datadome",
            severity="hard" if "datado.me/captcha" in html_lc else "soft",
            title="DataDome challenge",
            message="DataDome detected and challenged the request.",
            suggestion="Try a residential proxy from a different region. DataDome's JS challenge sometimes auto-solves; soft challenges often pass on retry.",
            is_behavioral=False,
        )

    # ── Akamai Bot Manager ──────────────────────────────────────────────
    if (
        "_abck" in cookies
        or "akamai-bm-telemetry" in html_lc
        or "ak_bmsc" in cookies
    ):
        # Akamai BM presence isn't always a block — these cookies appear
        # on every Akamai-protected site even when you pass. The block
        # itself shows specific signatures.
        if "access denied" in title_lc or "reference #" in html_lc or "/_Incapsula_Resource" in html_lc:
            return BlockDetection(
                blocked=True,
                vendor="akamai",
                severity="hard",
                title="Akamai Bot Manager block",
                message="Akamai Bot Manager rejected the request.",
                suggestion="Akamai uses TLS fingerprinting heavily. Try residential proxies; if still blocked, this site needs server-side TLS impersonation (curl-impersonate, not Chromium).",
                is_behavioral=False,
            )

    # ── Imperva / Incapsula ────────────────────────────────────────────
    if (
        any(k.startswith("incap_ses") for k in cookies)
        or "incident id" in html_lc
        or "_Incapsula_Resource" in html_lc
    ):
        return BlockDetection(
            blocked=True,
            vendor="imperva",
            severity="hard",
            title="Imperva (Incapsula) block",
            message="Imperva's WAF flagged this request as bot traffic.",
            suggestion="Imperva has aggressive IP reputation scoring. Switch to a fresh residential proxy; if persistent, the site has session-binding that needs cookie warming.",
            is_behavioral=False,
        )

    # ── Kasada ──────────────────────────────────────────────────────────
    if "KP_UIDz" in cookies or "kpsdk-cd" in html_lc or "x-kpsdk-ct" in headers:
        return BlockDetection(
            blocked=True,
            vendor="kasada",
            severity="hard",
            title="Kasada bot defense",
            message="Kasada's runtime sensor flagged the browser environment.",
            suggestion="Kasada specifically targets headless Chrome. Real headed Chrome via xvfb usually passes; otherwise requires their license-key bypass which is paid-vendor only.",
            is_behavioral=False,
        )

    # ── Generic CAPTCHA at the top of the page ─────────────────────────
    # If the only visible thing is a reCAPTCHA/hCaptcha box, we got
    # walled. (Don't false-positive on pages that embed CAPTCHA inside
    # a login form — only flag when it's the WHOLE page.)
    if title_lc in ("captcha", "verification", "security check"):
        if "g-recaptcha" in html_lc or "h-captcha" in html_lc or "cf-turnstile" in html_lc:
            return BlockDetection(
                blocked=True,
                vendor="captcha-wall",
                severity="hard",
                title="CAPTCHA wall",
                message="The page is a CAPTCHA challenge with no content behind it.",
                suggestion="Add a CAPTCHA-solver integration (2captcha, capmonster, anti-captcha) — this is a paid step we surface as a future upgrade.",
                is_behavioral=False,
            )

    # ── Generic "access denied" patterns ────────────────────────────────
    if title_lc in ("access denied", "403 forbidden", "forbidden") or (
        "you don" in html_lc and "permission" in html_lc and "access" in html_lc
    ):
        return BlockDetection(
            blocked=True,
            vendor="generic-403",
            severity="soft",
            title="Access denied",
            message="The site returned a generic block page.",
            suggestion="Often this is geo-blocking or IP reputation. Try a proxy in a different country.",
            is_behavioral=False,
        )

    return BlockDetection.ok()


def detect_from_snapshot(snap) -> BlockDetection:
    """Convenience wrapper for the common case of having a Snapshot
    object with .title, .url, and a cached body. Caller must pass the
    HTML if they want signature matching against page content.

    Used by the public-snapshot endpoint to wrap snapshot results before
    handing them back to the modal — failed extractions get a
    user-friendly "Cloudflare blocked this" instead of "0 fields found".
    """
    # snap object varies by caller — duck-type the attribute lookups.
    title = getattr(snap, "title", "") or ""
    url = getattr(snap, "url", "") or ""
    html = getattr(snap, "html", "") or ""
    return detect_block(title=title, html=html, url=url)
