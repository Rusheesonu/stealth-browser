# Contributing to stealth-browser

Thanks for considering a contribution. The project is small, opinionated,
and bench-first. Read the rules below before opening a PR — they save us
both time.

## Quick start

```bash
git clone https://github.com/Rusheesonu/stealth-browser
cd stealth-browser
python -m venv venv && source venv/bin/activate
pip install -e '.[dev,all]'    # core + curl_cffi + camoufox + dev tools
pytest tests/
```

`camoufox` will download a ~350MB patched Firefox binary on first use
into your user cache directory.

## Bench-first culture

> Numbers are the only truth. No claim of improvement without a benchmark
> delta in the commit message.

That rule comes from the parent product's iteration log. It applies here
too. PRs that claim "this should be faster" or "this defeats Cloudflare
better" but ship no measured delta will not merge.

The reference bench lives in the parent monorepo at
[`Rusheesonu/Stealth-Scraper/bench`](https://github.com/Rusheesonu/Stealth-Scraper/tree/master/bench).
You can run it against this package's engines by installing this package
as the engine layer in that monorepo's checkout.

Commit message format:

```
fix(camoufox): tune humanize for PerimeterX
antibot 77.8% → 81.4% (+3.6pp), perimeterx 0/3 → 1/3
```

## Adding a new engine

Engines implement the `Engine` Protocol in `stealth_browser/engines/base.py`.
Minimum stub:

```python
from stealth_browser.engines.base import (
    Engine, Capability, Requirements, EngineSnapshotResult, EngineFailedError,
)

class MyEngine:
    name = "my-engine"
    capabilities = (
        Capability.JS_EXEC
        | Capability.SCREENSHOT
        | Capability.DOM_QUERY
        # declare honestly — router uses these for filtering
    )
    cost_per_request_cents = 3   # ordinal rank for cheapest-first

    async def is_available(self) -> bool:
        try:
            import my_underlying_package  # noqa: F401
            return True
        except ImportError:
            return False

    async def snapshot(self, url, *, requirements: Requirements) -> EngineSnapshotResult:
        # Raise EngineFailedError(retriable_on_other_engine=True) on
        # engine-level failures so the router escalates to the next engine.
        return EngineSnapshotResult(url=url, title=..., elements=[...], ...)
```

Register in `stealth_browser/engines/__init__.py:_register_default_engines()`.

For credibility, include:
- Capability declaration matching what the engine REALLY does
- A bench run showing improvement on at least one vendor

## Adding an anti-bot signature

`stealth_browser/detect.py` is a long if/elif chain matching titles +
cookies + HTML markers. Adding a new vendor:

1. Add a `BlockDetection` branch in `detect_block()`
2. Use a verbatim title/marker quote as the signature
3. Include an actionable `suggestion` field
4. Add a test case in `tests/test_imports.py` (or new test file)

1 PR = 1 vendor.

## What we will NOT merge

- **Site-specific parsers.** Detection is vendor-shaped, not site-shaped.
- **PRs without a bench delta.**
- **PRs that regress any bench number.** Revert your own regression before merging.
- **New required deps without justification.** Anything that bloats the
  core install goes behind an optional extra.

## Style

- Python 3.10+ syntax
- Ruff for lint/format (`ruff check . && ruff format .`)
- Type-hint public APIs (we ship `py.typed`)
- Logging via `logging.getLogger(__name__)` — no bare `print()`
- Async-first

## Questions

- Bug? Open an issue.
- Security? See `SECURITY.md`.
- New engine or vendor signature idea? Open a discussion first.
- General? `support@stealthscraper.dev` or hit Rushi on X (@rushikeshsonu).
