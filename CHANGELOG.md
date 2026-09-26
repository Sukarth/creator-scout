# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Project skeleton: `scout` package, `pyproject.toml`, `.env.example`, MIT licence.
- SQLite store for creators, per-market screenings, edges, videos, metrics,
  snowball seeds, runs and the API response cache. Screenings are per market,
  so an account filtered for one market remains available to others.
- ScrapeCreators client: cache-first, per-run credit budget, records
  `credits_charged` and `credits_remaining` from every live response. Client
  errors (e.g. unknown handle) are cached so a miss is never paid twice.
- Market configs for Estonia, Finland and Germany: languages, market signals,
  seed hashtags and keywords, retailer and competitor seed accounts.
- TikTok response normalisation for hashtag, keyword, following, user search,
  profile, region and recent-videos endpoints.
- Hard filters: market bucket (sure / unsure / other) from registration region,
  region lookup, app and caption language, video region and bio/caption
  signals; follower band; private; activity. Every filtered account keeps its
  reason; out-of-market accounts registered in another supported market are
  queued for that market.
- Enrichment: profile, recent videos, median views, engagement rate, posting
  cadence, ad markers (platform paid-partnership flag and caption markers),
  sponsor handles, competitor matching, price estimate and deal suggestion.
  Bio-link pages are fetched over plain HTTP for extra emails and social links.
- Snowball through following lists of qualified creators, business accounts
  and retailer seeds, prioritised by new in-market accounts per credit.
  Transient failures skip a seed or candidate for the current run only.
- XLSX export (Shortlist, Maybe, Other markets pool, Seeds and sources, All
  screened, Run log) with an outreach status dropdown, plus one CSV per sheet.
- `scout check` quality gates: band, market resolution, no duplicates from
  earlier runs, evidence quotes, contact path, funnel totals.
- CLI: `markets`, `run`, `export`, `check`, `credits`, `api`, `cache stats|clear`.

### Fixed
- Market words matched as prefixes (Estonian "tere" inside "Terezinha");
  they now match whole words only.
- Social links scraped from bio-link pages could pick up asset paths such as
  `instagram.com/rsrc.php`. Reserved paths and file names are ignored, and
  login-walled bio links (Instagram, YouTube, Twitch, X) are read from the URL
  without fetching the page.
- Hashtag and keyword harvest stops after a short page instead of paying for
  the empty page that usually follows.

### Notes
- Keyword search results carry no author region, but each video has its own
  `region`; it is used as a market hint so out-of-market authors cost nothing.
- Estonian keyword search returns mostly unrelated results; hashtags are the
  primary harvest source for small-language markets.
