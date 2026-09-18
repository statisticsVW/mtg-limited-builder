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

This covers `scripts/match.py` and `scripts/build.py` too. They live in `scripts/` per the repo
layout but are copied into `skill/` when the bundle is built, so they carry the same constraint
and say so in their header comment. The suite runs on a bare interpreter to keep them honest:

    py -3.12 -m unittest discover -s tests -v

`scripts/fetch_data.py` is the only exception: it runs locally, and may use the packages in
`requirements-dev.txt` (pandas, requests).

## Pipeline contract

`match.py` does **not** do OCR. Claude reads the pool photos with vision and emits candidate card
names, one per line; `match.py` resolves those noisy strings against the set list and reports what
it could not place. It emits JSON (`--format json`) for `build.py` to consume.

Mis-resolving a card is worse than admitting failure, because it silently puts a card in the pool
that the player does not own. The matcher therefore refuses close calls -- short names clear a
higher bar, and a near-tie between two cards is reported as ambiguous rather than guessed.

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
