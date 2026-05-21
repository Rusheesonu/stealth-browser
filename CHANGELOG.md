# Changelog

## v0.2.0 — 2026-05-22

- **NEW**: Multi-engine router (nodriver + curl_cffi + camoufox)
- **NEW**: camoufox engine — the only one that scores clean on creepjs
- **NEW**: curl_cffi engine — TLS-impersonating HTTP, 50-100x faster on static
- **NEW**: Capability-based engine selection + per-vendor affinity + per-host learning + escalation on failure
- **NEW**: Optional extras: `[tls]`, `[firefox]`, `[all]`
- **NEW**: Residential proxy hook via `RESIDENTIAL_PROXIES_JSON` env (auto-routes IP-rep-sensitive vendors)
- **NEW**: `detect_block` covers 6 vendors with vendor + actionable suggestion
- **CHANGED**: Package is now fully self-contained (no parent-monorepo imports)
- **FIXED**: Image-rendering race in snapshot pipeline (MutationObserver-based lazy-image killer)

## v0.1.0 — 2026-05-13

- Initial release. Single-engine nodriver pool with 20+ fingerprint patches.
