# mtg-limited-builder

Proof-of-concept Claude Skill that reads photos of a Magic: The Gathering sealed pool and
recommends a 40-card deck. Current target set: **Secrets of Strixhaven (SOS)**.

## Layout

| Path | Purpose |
|---|---|
| `data/` | Per-set CSVs. `data/sos_pool_universe.csv` is the joined card + ratings table. |
| `data/raw/` | Cached 17Lands dataset downloads. Gitignored, re-downloadable. |
| `scripts/` | Dev-time pipeline: `fetch_data.py`, `match.py`, `build.py`. |
| `skill/` | `SKILL.md` plus bundled scripts and data. This is the zip target. |
| `tests/` | Sample pool photos and their answer keys. |

## Rules

**Scripts under `skill/` must use the Python standard library only.**

The skill runs in claude.ai's code sandbox, not on a developer machine. Package installs there may
be blocked or slow, and import cost is paid on every run. `difflib` handles name matching and
`itertools` handles deck search at this scale — roughly 90 pool cards against a ~350-card set.

`scripts/fetch_data.py` is the exception: it runs locally, and may use the packages in
`requirements-dev.txt` (pandas, requests).

## Data sources

- **Scryfall** for card data. Send a `User-Agent`, and sleep 100 ms between requests including
  `next_page` hops.
- **17Lands** for performance data, via the public S3 datasets at
  `https://17lands-public.s3.amazonaws.com/analysis_data/`. The `card_ratings/data` JSON API is
  **not** usable — it returns the card roster with every statistic zeroed for anonymous callers.
  Ratings are aggregated locally from the per-game CSVs instead.

## Set specifics

`SOS` introduces the `prepare` layout: two faces, top-level `cmc` covers the front face only, and
**there is no top-level `oracle_text`** — it exists per face. Read `card_faces[]` for these.
