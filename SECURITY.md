# Security Policy

## Supported versions

`stealth-browser` is at v0.2.x. We backport security fixes to the latest
v0.x line only.

| Version | Supported |
|---------|-----------|
| 0.2.x   | ✅        |
| 0.1.x   | ❌ — please upgrade |

## Reporting a vulnerability

If you've found a vulnerability that affects users of this package
(not the underlying `nodriver` / `curl_cffi` / `camoufox` projects),
email **`support@stealthscraper.dev`**.

Include:
- A clear description
- Reproduction steps or PoC
- Affected version(s)
- Suggested fix (if any)

### Timeline

- **Acknowledge**: within 7 days
- **Patch confirmed issue**: within 30 days
- **Public disclosure**: coordinated; default 14 days after patch ships to PyPI

No bug bounty program. We credit you in the CHANGELOG + release notes
unless you'd prefer anonymity.

## Out of scope

Issues that belong upstream:

- `nodriver` → https://github.com/ultrafunkamsterdam/nodriver/issues
- `curl_cffi` → https://github.com/lexiforest/curl_cffi/issues
- `camoufox` → https://github.com/daijro/camoufox/issues
- `playwright` → https://github.com/microsoft/playwright/issues

Not sure where it belongs? Email us, we'll route it.

## A note on threat models

The package is designed for the **scraping-target threat model**:
untrusted URLs go in, the engine fetches them, structured data comes
out. We assume the target site is adversarial. We do NOT assume the user
OR the runtime is adversarial — if you run this package as a service
that accepts URLs from third parties, you're responsible for:

- **Sandboxing** the browser process (Linux seccomp/AppArmor or Docker
  user namespacing)
- **SSRF guards** on the URL — block private/loopback/link-local
  addresses before passing to `router.snapshot()`
- **Rate-limiting** per caller — Chromium is RAM-heavy; an attacker can
  OOM your box with a fast loop

The parent product (Stealth-Scraper SaaS) addresses these at the API
layer. Build your service the same way.
