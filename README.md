# stealth-browser

**A multi-engine browser router that beats modern anti-bot vendors.**

[![PyPI](https://img.shields.io/pypi/v/stealth-browser.svg)](https://pypi.org/project/stealth-browser/)
[![Python](https://img.shields.io/pypi/pyversions/stealth-browser.svg)](https://pypi.org/project/stealth-browser/)

```python
import asyncio
from stealth_browser.engines import router, Requirements

async def main():
    snap, decision = await router.snapshot(
        "https://www.crunchbase.com/",
        requirements=Requirements(needs_js=True, vendor_hint="cloudflare"),
    )
    print(f"engine used: {snap.engine_name}")    # → "nodriver"
    print(f"elements:    {len(snap.elements)}")  # → 717
    print(f"router:      {decision.reason}")
    # → "chose 'nodriver'; vendor-affinity rank 1/2 for cloudflare; cost 1c"

asyncio.run(main())
```

No single browser-automation library beats every anti-bot vendor. The
router ships three engines + decides per-request which to use, falling
through to the next when the first fails.

| Engine | What it is | Wins on |
|---|---|---|
| **`nodriver`** | Chromium via patched CDP (no Playwright shim) | Cloudflare basic, Turnstile invisible, most generic sites |
| **`curl_cffi`** | Real Chrome-131 TLS fingerprint over plain HTTP — no JS | DataDome, Akamai (TLS-fingerprint-gated). 50-100× faster than browsers. |
| **`camoufox`** | Patched Firefox via Playwright | PerimeterX, Kasada (target Chromium specifically). **The only engine that scores clean on creepjs.** |

---

## What you get vs. competition (May 2026)

| Scenario | `playwright-stealth` | `undetected-chromedriver` | `nodriver` solo | **stealth-browser** |
|---|:---:|:---:|:---:|:---:|
| `bot.sannysoft.com` | 8/14 | 11/14 | 13/14 | **34/34** |
| `creepjs` "headless?" | flagged | flagged 31% | flagged 31% | **0% (Firefox via camoufox)** |
| `fingerprint.com` bot-detect | bot | bot | bot | **`{"bot":"not_detected"}`** |
| Cloudflare Turnstile invisible | ❌ | ✅ | ✅ | ✅ |
| Cloudflare Turnstile visible | ❌ | ❌ | ❌ | ✅ (via camoufox) |
| DataDome TLS-fingerprint mode | ❌ | ❌ | ❌ | ✅ (via curl_cffi) |
| Auto-escalate on engine failure | n/a | n/a | n/a | ✅ |
| Per-host engine learning | n/a | n/a | n/a | ✅ |
| Honest cost ranking per engine | n/a | n/a | n/a | ✅ |

Numbers come from real bench runs in the parent repo — see
[Rusheesonu/Stealth-Scraper#bench](https://github.com/Rusheesonu/Stealth-Scraper/tree/master/bench)
for the methodology and reproducible commands.

---

## Install

```bash
# Core: just the router + nodriver engine
pip install stealth-browser

# Add the curl_cffi engine (real-Chrome TLS impersonation, no JS)
pip install 'stealth-browser[tls]'

# Add the camoufox engine (patched Firefox; 350MB binary download on first use)
pip install 'stealth-browser[firefox]'

# Everything
pip install 'stealth-browser[all]'
```

Requires Python 3.10+. `nodriver` needs a real Chrome/Chromium installed
locally (`brew install --cask google-chrome` on macOS). `camoufox`
downloads its patched Firefox binary on first use into your user cache
(`~/Library/Caches/camoufox/` on macOS, `~/.cache/camoufox/` on Linux).

---

## Quickstart

### Basic — router picks per-request

```python
import asyncio
from stealth_browser.engines import router, Requirements

async def main():
    # Router uses cost-ordered candidates by default (cheapest engine
    # that satisfies the requirements).
    snap, decision = await router.snapshot(
        "https://news.ycombinator.com/",
        requirements=Requirements(needs_js=True),
    )
    print(f"got {len(snap.elements)} elements via {snap.engine_name}")

asyncio.run(main())
```

### With vendor hint — tilt the router

```python
# Already know the target is behind CF Turnstile? Tell the router so
# it picks camoufox (Firefox engine) first.
snap, decision = await router.snapshot(
    "https://chess.com/",
    requirements=Requirements(
        needs_js=True,
        vendor_hint="cloudflare-turnstile",
    ),
)
# router.reason → "chose 'camoufox'; vendor-affinity rank 1/2 for cloudflare-turnstile; cost 2c"
```

Supported `vendor_hint` values: `cloudflare`, `cloudflare-turnstile`,
`datadome`, `perimeterx`, `akamai`, `imperva`, `kasada`,
`fingerprint-test`. See `engines/router.py:VENDOR_AFFINITY` for the
mapping (each hint maps to an ordered list of engine names).

### Lightweight (no JS, no browser)

```python
# For static HTML behind TLS-fingerprint walls (Akamai, DataDome on
# non-SPA pages): curl_cffi sends the byte-exact Chrome 131 ClientHello.
snap, decision = await router.snapshot(
    "https://www.petsmart.com/",
    requirements=Requirements(
        needs_js=False,                # static HTML is fine
        needs_screenshot=False,         # no rendering pipeline
        prefer_lightweight=True,        # pick curl_cffi over browsers
        vendor_hint="datadome",
    ),
)
# Returns in ~200ms (vs ~10s for a browser engine).
```

### Honest failure reporting

```python
from stealth_browser.engines.base import EngineFailedError
from stealth_browser.detect import detect_block

try:
    snap, decision = await router.snapshot("https://www.zillow.com/")
except EngineFailedError as e:
    print(f"router gave up: {e}")
    print(f"escalation path: {decision.escalation_path}")
    # → ["nodriver→fail: ...", "camoufox→fail: ..."]

# Or check the snapshot for an anti-bot wall after the fact:
block = detect_block(title=snap.title, html=snap.elements[0]["text"])
if block.blocked:
    print(f"blocked by {block.vendor}: {block.suggestion}")
    # → "blocked by perimeterx: This site needs a CAPTCHA-solver service..."
```

The included `detect.py` catches all six major vendors (Cloudflare,
PerimeterX, DataDome, Akamai, Imperva, Kasada) and returns a structured
`BlockDetection` with vendor name + actionable suggestion. **Saves you
from silently returning broken extraction.**

---

## The Engine protocol — bring your own engine

Engines are Python protocols. If you have a different bypass approach
(commercial solver, your own patched fork, headed browser via xvfb),
plug it in:

```python
from stealth_browser.engines.base import (
    Engine, Capability, Requirements, EngineSnapshotResult, EngineFailedError,
)
from stealth_browser.engines import router

class MyCustomEngine:
    name = "my-engine"
    capabilities = Capability.JS_EXEC | Capability.SCREENSHOT
    cost_per_request_cents = 5   # used for router ranking

    async def is_available(self) -> bool:
        return True

    async def snapshot(self, url: str, *, requirements: Requirements) -> EngineSnapshotResult:
        # ... your impl ...
        return EngineSnapshotResult(url=url, title="...", elements=[...], ...)

router.register(MyCustomEngine())
# Router now considers your engine alongside nodriver / curl_cffi / camoufox.
```

Capabilities the router filters by:
`JS_EXEC`, `SCREENSHOT`, `DOM_QUERY`, `TLS_IMPERSONATION`,
`HTTP2_FINGERPRINT`, `CDP_NATIVE`, `BEHAVIORAL`, `FIREFOX_ENGINE`,
`LIGHTWEIGHT`, `HEADED`, `MOBILE_EMULATION`, `PROXY_SUPPORT`,
`COOKIE_PERSISTENCE`.

---

## Proxy auth

Standard `--proxy-server` doesn't carry inline auth. The package wires
up a CDP `Fetch.authRequired` handler for `nodriver`, native Playwright
auth for `camoufox`, and direct URL-embedded auth for `curl_cffi`.

```python
# Datacenter pool (Webshare / etc.)
import os
os.environ["PROXIES_JSON"] = '''{
  "credentials": [{"user": "ru", "pass": "..."}],
  "endpoints":   [{"host": "p.webshare.io", "port": 80}]
}'''
os.environ["PROXIES_ENABLED"] = "true"

# Residential pool (Bright Data / Oxylabs / Smartproxy) — auto-routed
# for IP-rep-sensitive vendors only (cloudflare/imperva/akamai/datadome),
# never wasted on PX or Kasada where IP rep doesn't matter.
os.environ["RESIDENTIAL_PROXIES_JSON"] = '''{
  "credentials": [{"user": "...", "pass": "..."}],
  "endpoints":   [{"host": "brd.superproxy.io", "port": 22225}]
}'''
```

The pool is share-then-pick: every engine pulls from the same proxy
pool (`proxies.pick_for_vendor(vendor)`), so the target can't
fingerprint your "engine swap" by detecting an IP change.

---

## What it doesn't claim to do

Be honest: **no JS-level stealth defeats every site, period.** The
remaining boss-level walls:

- **PerimeterX press-and-hold** (zillow.com) — behavioral biometric.
  Needs a CAPTCHA-solver service (CapSolver / 2captcha) — pure browser
  stealth never wins this.
- **Imperva IP-reputation** (hyatt.com) — flags your IP regardless of
  fingerprint. Needs residential proxies with rotation.
- **Cloudflare Enterprise with managed challenge** — sometimes
  unkillable without a paid CF-bypass-as-a-service.

The router won't pretend. It calls `detect_block()` on the result and
surfaces the exact vendor + the recommended workaround, instead of
quietly returning an empty page.

---

## Architecture

```
stealth_browser/
├── engines/
│   ├── base.py           # Engine Protocol, Capability flags, Requirements,
│   │                       EngineSnapshotResult, EngineFailedError
│   ├── router.py         # EngineRouter + VENDOR_AFFINITY + SuccessTracker
│   ├── nodriver_engine.py    # Chromium via patched CDP
│   ├── curl_cffi_engine.py   # Real-Chrome TLS impersonation, no JS
│   └── camoufox_engine.py    # Patched Firefox via Playwright
├── browser.py            # nodriver browser pool with CDP proxy auth +
│                           transient-error retry markers
├── snapshot.py           # high-level take_snapshot() — what the engines
│                           wrap
├── stealth.py            # 20+ fingerprint patches as a single JS init
│                           script (for use with vanilla nodriver too)
├── actions.py            # click / fill / scroll / wait helpers
├── extract_js.py         # in-page element-catalog JS payload
├── proxies.py            # datacenter + residential proxy pool helpers
├── safety.py             # robots.txt + per-host token-bucket rate limit
└── detect.py             # Anti-bot wall signature library (6 vendors)
```

Each module is independently usable. If you just want the fingerprint
JS for your own Playwright stack:

```python
from stealth_browser.stealth import ULTRA_STEALTH_JS
await page.add_init_script(ULTRA_STEALTH_JS)
```

---

## License notes

**This package**: Apache-2.0. Use commercially, fork freely, just keep
the notice.

**But your project may inherit copyleft via dependencies:**
- `nodriver` is **AGPL-3.0**. If you self-host a service using
  `nodriver`, AGPL §13 requires you to offer source code to your users
  (you can satisfy this by publishing your fork in a public repo and
  linking to it from your service's footer).
- `curl_cffi`: Apache-2.0 (compatible)
- `camoufox`: MIT (compatible)
- `playwright`: Apache-2.0 (compatible)

If AGPL is a non-starter for your use case, install with
`pip install 'stealth-browser[firefox]'` and never invoke the
nodriver engine — the router will just skip it.

See [LICENSES.md](https://github.com/Rusheesonu/Stealth-Scraper/blob/master/LICENSES.md)
in the parent repo for the full dependency audit.

---

## Contributing

PRs welcome — particularly:
- **New anti-bot signatures** for `detect.py` (you find one in the
  wild, we add it)
- **New engines** that implement the `Engine` protocol — commercial
  solvers (`capsolver`, `2captcha`), TLS-spoofing proxies
  (`curl-impersonate-as-CONNECT-proxy`), residential-network-aware
  engines
- **Fingerprint patches** as detection vendors evolve
- **VENDOR_AFFINITY entries** based on your real-world bench data

Bench-first culture: numbers are the only truth. PRs that don't
include a bench delta won't merge.

---

## See also

- [**Rusheesonu/Stealth-Scraper**](https://github.com/Rusheesonu/Stealth-Scraper) — the full hosted product (visual picker, AI assist, SDKs, billing). Uses this package as the engine layer.
- [**stealthscraper.dev**](https://stealthscraper.dev) — the SaaS frontend. Free tier, no card.

Built by [@rushikeshsonu](https://x.com/rushikeshsonu). Questions, paid integrations, hire me to scrape your target: `rushikeshsonu@gmail.com`.
