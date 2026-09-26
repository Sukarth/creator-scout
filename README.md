# Creator Scout

Finds small, niche creators on TikTok in a given country and language, filters
them on hard data (registration region, follower band, activity), expands the
pool through the following lists of good creators and local retailers, and
exports an outreach-ready spreadsheet.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env        # set SC_KEY (ScrapeCreators API key)
scout markets
scout run --market ee --band 2000-50000 --target 40 --budget 300 --judge none
```

Every API response is cached locally in `data/scout.db`, so reruns cost no
credits. Each run has a hard credit budget and stops cleanly when it is spent.

## Tests

```bash
pytest
```

Tests run offline against saved API responses in `tests/fixtures/`.
