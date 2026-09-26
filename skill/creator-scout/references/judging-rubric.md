# Judging rubric

The question: **would a young audience that plays PC games watch this creator?**
Gaming and tech are the core; gaming gear, gaming news and entertainment
creators with a young gamer audience also fit.

## Scores

- `gaming_pc_relevance` (0-5): 5 PC hardware, builds or setups are the focus;
  4 mostly gameplay, streaming or game content; 3 gaming is a regular part;
  2 occasional; 1 a single mention; 0 none.
- `young_gamer_appeal` (0-5): how likely a young PC-gaming audience watches.
- `trust_content_score` (0-5): ability to make trust content (builds,
  benchmarks, setup tours, upgrade stories, honest reviews). A ranking score,
  not a requirement.
- `fit_score` (0-100): overall.

## Decision

- **accept**: a real creator (own content, not a repost or clip account) with
  relevance >= 3, or appeal >= 4 for entertainment/tech creators, and a
  verbatim evidence quote.
- **maybe**: borderline (relevance 2 or appeal 3), thin evidence, organisation
  accounts, or unclear market.
- **reject**: no gaming or young-gamer angle, not a real creator, shop/brand
  (`is_business_account: true`), or brand-unsafe.

The code enforces the thresholds: a "maybe" whose scores meet the accept rule
(with fit >= 50 and a verified quote) becomes accept, and an "accept" below the
rule becomes maybe.

## Fields

`niche_category`: gaming, tech review, gaming gear, gaming news,
entertainment, lifestyle or other. `games`: titles played or covered.
`market_resolution`: the market code, another country code, or `unclear`.
`content_language`: ISO 639-1 code. `brand_safety_flags`: real risks only
(gambling, adult content, hate speech, firearms, mostly children on camera).

## Examples (patterns seen in real runs)

- Minecraft or Roblox YouTube channel in the local language, 100k subs,
  weekly uploads → accept, gaming, relevance 5.
- TikTok CS2 clips with a Twitch link → accept, gaming, relevance 5.
- Youth sketch comedy, no games but a school-age audience → accept,
  entertainment, relevance 1, appeal 4.
- Fan account reposting a streamer's clips → reject (not a real creator).
- Esports team, retailer, game studio, restaurant → reject, business account.
- Parent-run account of a 12-year-old player → maybe, brand safety "mostly
  children on camera".
- Lifestyle creator whose only gaming signal is one hashtag → reject.
