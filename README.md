# Creator Scout

Finds small, niche gaming and tech creators on TikTok and YouTube in a given
country and language, filters them on hard data (registration region or
channel country, follower band, activity), judges fit with an LLM, and
expands the pool through the following lists of accepted creators. The result
is an outreach-ready spreadsheet with one row per creator.

## How a run works

1. **Harvest** gaming-specific sources: local-language gaming hashtags,
   YouTube searches in the market (`region` set), keyword searches, then
   global game hashtags through a proxy in the market (useful in big markets,
   last resort in small ones).
2. **Hard filters** (code): market bucket from the registration region, the
   YouTube channel country, caption language and local signals; follower or
   subscriber band; private and inactive accounts. Nothing is discarded: every
   account keeps its status and reason.
3. **Pre-judge** (free LLM) on free data only: yes / unsure / no. Only a
   confident "no" is skipped before paying for enrichment.
4. **Enrich**: TikTok profile and recent videos; YouTube uploads and views
   from the official Data API (free quota), long videos and Shorts separately.
5. **Judge** (free LLM or Claude): would a young PC-gaming audience watch
   this creator? Niche, games, risks and an evidence quote per creator.
6. **Snowball**: once two creators are accepted, their following lists are
   expanded, alternating with new harvest pages.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env    # SC_KEY, GROQ_API_KEY, OPENCODE_API_KEY, YOUTUBE_API_KEY
scout markets
scout run --market ee --preset hidden-gems --budget 300
scout check
```

Size presets: `default` (TikTok 4k-500k followers, YouTube 50k-250k
subscribers) and `hidden-gems` (TikTok 1k+, YouTube 5k+). Override with
`--band` and `--yt-band`.

Useful commands:

| Command | Purpose |
|---|---|
| `scout resume --run N --budget B` | continue a run from its stored state |
| `scout yield --run N` | accepted creators per 100 credits, by source |
| `scout recall --market ee` | which existing partners a run found by itself, and why others were missed |
| `scout export --run N` | XLSX (incl. a sheet in the client's own layout) and CSV |
| `scout candidates` / `decide` | hand judging to Claude in skill mode (`--judge claude`) |
| `scout pitches --run N` | optional outreach drafts |

Every API response is cached locally in `data/scout.db`, so reruns cost no
credits. Each run has a hard credit budget and stops cleanly when it is spent.

## Tests

```bash
pytest
```

Tests run offline against saved API responses in `tests/fixtures/`, with
email addresses replaced by placeholders.

## Data and privacy

Only public profile data is used, fetched logged-out through a data provider.
Only fields needed for outreach are kept. The tool drafts messages; a person
reviews and sends them.
