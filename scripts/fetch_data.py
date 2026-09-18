"""Build the Secrets of Strixhaven sealed pool universe.

Pulls every card that can appear in an SOS sealed pool from Scryfall, aggregates 17Lands
performance data for them, and writes the joined table to data/sos_pool_universe.csv.

Dev-time script: it may use pandas and requests (see requirements-dev.txt). Nothing under
skill/ may -- that code is standard library only.

Note on 17Lands: the card_ratings/data JSON API returns the card roster with every statistic
zeroed for anonymous callers, so ratings are aggregated here from the public per-game CSVs
instead. Those files are static, so data/raw/ caches them between runs.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"

USER_AGENT = "mtg-limited-builder/0.1 (github.com/statisticsVW/mtg-limited-builder)"

SCRYFALL_SEARCH = "https://api.scryfall.com/cards/search"
SCRYFALL_DELAY_S = 0.1  # Scryfall asks for 50-100ms between requests.

# The Special Guests set is a rolling product: each drop carries the release date of the set it
# accompanies, so this date isolates the 11 cards printed alongside SOS exactly.
SOS_RELEASE_DATE = "2026-04-24"

CARD_SOURCES = [
    ("sos", "set:sos", None),
    ("soa", "set:soa", None),
    ("spg", "set:spg", SOS_RELEASE_DATE),
]
EXPECTED_COUNTS = {"sos": 271, "soa": 65, "spg": 11}

S3_GAME_DATA = (
    "https://17lands-public.s3.amazonaws.com/analysis_data/game_data/"
    "game_data_public.SOS.{fmt}.csv.gz"
)
SEALED_FORMATS = ["Sealed", "TradSealed"]
FALLBACK_FORMAT = "PremierDraft"

WUBRG = "WUBRG"
CARD_COLUMN_PREFIXES = ("opening_hand_", "drawn_", "tutored_", "deck_")

OUTPUT_COLUMNS = [
    "name", "face_1_name", "face_2_name", "mana_cost", "cmc", "colors", "color_identity",
    "type_line", "rarity", "set", "collector_number", "oracle_text", "has_converge",
    "is_basic_land", "gih_wr", "oh_wr", "iwd", "gih_games", "oh_games", "alsa",
    "ratings_source", "sealed_gih_games", "premier_gih_games",
]


# --------------------------------------------------------------------------------------
# Name normalization
# --------------------------------------------------------------------------------------

def normalize_name(raw: str) -> str:
    """Reduce a card name to a stable join key.

    The 17Lands headers carry a doubled space after commas ("Abigale,  Poet Laureate"), so
    collapsing whitespace is what makes the two sides meet. Accents and curly apostrophes are
    folded too, since the sources do not agree on them.
    """
    text = unicodedata.normalize("NFKD", raw)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("—", "-").replace("–", "-")
    text = re.sub(r"\s+", " ", text)
    return text.strip().casefold()


# --------------------------------------------------------------------------------------
# Phase 1 -- Scryfall
# --------------------------------------------------------------------------------------

def scryfall_get(session: requests.Session, url: str, params: dict | None = None) -> dict:
    """GET with Scryfall's requested etiquette: identify ourselves, throttle, back off on 429."""
    for attempt in range(5):
        time.sleep(SCRYFALL_DELAY_S)
        response = session.get(url, params=params, timeout=30)
        if response.status_code == 200:
            return response.json()
        if response.status_code in (429, 500, 502, 503, 504):
            wait = 2 ** attempt
            print(f"  scryfall {response.status_code}, retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        response.raise_for_status()
    raise RuntimeError(f"Scryfall kept failing: {url}")


def search_cards(session: requests.Session, query: str) -> list[dict]:
    """Page through a Scryfall search, one printing per distinct card."""
    cards: list[dict] = []
    payload = scryfall_get(session, SCRYFALL_SEARCH,
                           {"q": query, "unique": "cards", "order": "set"})
    while True:
        cards.extend(payload["data"])
        if not payload.get("has_more"):
            return cards
        payload = scryfall_get(session, payload["next_page"])


def sort_colors(colors: list[str] | None) -> str:
    """Pipe-joined colors in WUBRG order, so the CSV diffs cleanly between runs."""
    if not colors:
        return ""
    return "|".join(sorted(colors, key=WUBRG.index))


def flatten_card(card: dict) -> dict:
    """Project a Scryfall card onto the columns we keep.

    SOS's `prepare` layout has no top-level oracle_text -- it only exists per face -- so rules
    text is assembled from card_faces when the top-level field is missing.
    """
    faces = card.get("card_faces") or []
    face_1 = faces[0]["name"] if faces else card["name"]
    face_2 = faces[1]["name"] if len(faces) > 1 else ""

    oracle_text = card.get("oracle_text")
    if oracle_text is None and faces:
        oracle_text = "\n//\n".join(face.get("oracle_text", "") for face in faces)
    oracle_text = oracle_text or ""

    type_line = card.get("type_line", "")
    mana_cost = card.get("mana_cost")
    if mana_cost is None and faces:
        mana_cost = " // ".join(face.get("mana_cost", "") for face in faces)

    return {
        "name": card["name"],
        "face_1_name": face_1,
        "face_2_name": face_2,
        "mana_cost": mana_cost or "",
        "cmc": card.get("cmc"),
        "colors": sort_colors(card.get("colors")),
        "color_identity": sort_colors(card.get("color_identity")),
        "type_line": type_line,
        "rarity": card.get("rarity", ""),
        "set": card.get("set", ""),
        "collector_number": card.get("collector_number", ""),
        "oracle_text": oracle_text,
        "has_converge": bool(re.search(r"\bconverge\b", oracle_text, re.IGNORECASE)),
        "is_basic_land": "Basic" in type_line and "Land" in type_line,
        "join_key": normalize_name(face_1),
    }


def fetch_card_universe() -> pd.DataFrame:
    """Every card printable in an SOS sealed pool: the main set, the Mystical Archive, and the
    Special Guests drop that shipped with it."""
    print("Phase 1: Scryfall")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    rows: list[dict] = []
    for set_code, query, release_filter in CARD_SOURCES:
        cards = search_cards(session, query)
        if release_filter:
            cards = [c for c in cards if c.get("released_at") == release_filter]
        print(f"  {set_code.upper():<4} {len(cards):>4} cards")
        rows.extend(flatten_card(card) for card in cards)

    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------
# Phase 2 -- 17Lands
# --------------------------------------------------------------------------------------

def download_dataset(fmt: str, refresh: bool) -> Path:
    """Fetch a 17Lands per-game dataset, reusing the cached copy when we already have it."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"game_data_public.SOS.{fmt}.csv.gz"
    if path.exists() and not refresh:
        print(f"  {fmt:<13} cached ({path.stat().st_size / 1e6:.1f} MB)")
        return path

    print(f"  {fmt:<13} downloading...", end="", flush=True)
    response = requests.get(S3_GAME_DATA.format(fmt=fmt),
                            headers={"User-Agent": USER_AGENT}, stream=True, timeout=120)
    response.raise_for_status()
    temp = path.with_suffix(".part")
    with temp.open("wb") as handle:
        for block in response.iter_content(chunk_size=1 << 20):
            handle.write(block)
    temp.replace(path)
    print(f" {path.stat().st_size / 1e6:.1f} MB")
    return path


class RatingCounters:
    """Raw game counters per card, accumulated across one or more 17Lands datasets.

    Counts are kept rather than rates so that datasets can be pooled by addition -- averaging
    two formats' win rates would weight an 18k-game queue the same as a 42k-game one.
    """

    def __init__(self) -> None:
        self.gih_games: dict[str, int] = defaultdict(int)
        self.gih_wins: dict[str, int] = defaultdict(int)
        self.oh_games: dict[str, int] = defaultdict(int)
        self.oh_wins: dict[str, int] = defaultdict(int)
        self.gns_games: dict[str, int] = defaultdict(int)
        self.gns_wins: dict[str, int] = defaultdict(int)
        self.games_seen = 0

    def rates(self, card: str) -> dict:
        gih_n, oh_n, gns_n = self.gih_games[card], self.oh_games[card], self.gns_games[card]
        gih_wr = self.gih_wins[card] / gih_n if gih_n else None
        gns_wr = self.gns_wins[card] / gns_n if gns_n else None
        return {
            "gih_wr": gih_wr,
            "oh_wr": self.oh_wins[card] / oh_n if oh_n else None,
            "iwd": (gih_wr - gns_wr) if (gih_wr is not None and gns_wr is not None) else None,
            "gih_games": gih_n,
            "oh_games": oh_n,
        }


def accumulate(path: Path, counters: RatingCounters, chunk_size: int) -> None:
    """Stream one dataset into the counters.

    These files are ~1,600 columns wide, so they are read in chunks and reduced immediately;
    a whole format is never held in memory at once.
    """
    reader = pd.read_csv(path, compression="gzip", chunksize=chunk_size, low_memory=False)
    card_names: list[str] | None = None
    groups: dict[str, list[str]] = {}

    for chunk in reader:
        if card_names is None:
            deck_cols = [c for c in chunk.columns if c.startswith("deck_")]
            raw_names = [c[len("deck_"):] for c in deck_cols]
            card_names = [normalize_name(n) for n in raw_names]
            groups = {prefix: [prefix + n for n in raw_names]
                      for prefix in CARD_COLUMN_PREFIXES}
            missing = [c for cols in groups.values() for c in cols if c not in chunk.columns]
            if missing:
                raise RuntimeError(f"{path.name}: expected columns absent, e.g. {missing[:3]}")

        won = chunk["won"].astype(str).str.lower().isin(["true", "1"]).to_numpy()

        in_opening = chunk[groups["opening_hand_"]].fillna(0).to_numpy() > 0
        seen = (in_opening
                | (chunk[groups["drawn_"]].fillna(0).to_numpy() > 0)
                | (chunk[groups["tutored_"]].fillna(0).to_numpy() > 0))
        in_deck = chunk[groups["deck_"]].fillna(0).to_numpy() > 0
        never_seen = in_deck & ~seen

        wins = won[:, None]
        for name, gih, gih_w, oh, oh_w, gns, gns_w in zip(
            card_names,
            seen.sum(axis=0), (seen & wins).sum(axis=0),
            in_opening.sum(axis=0), (in_opening & wins).sum(axis=0),
            never_seen.sum(axis=0), (never_seen & wins).sum(axis=0),
        ):
            counters.gih_games[name] += int(gih)
            counters.gih_wins[name] += int(gih_w)
            counters.oh_games[name] += int(oh)
            counters.oh_wins[name] += int(oh_w)
            counters.gns_games[name] += int(gns)
            counters.gns_wins[name] += int(gns_w)

        counters.games_seen += len(chunk)


def fetch_ratings(refresh: bool, chunk_size: int) -> tuple[RatingCounters, RatingCounters]:
    """Sealed counters (both sealed queues pooled) and the PremierDraft fallback counters."""
    print("Phase 2: 17Lands")
    sealed = RatingCounters()
    for fmt in SEALED_FORMATS:
        path = download_dataset(fmt, refresh)
        before = sealed.games_seen
        accumulate(path, sealed, chunk_size)
        print(f"  {fmt:<13} {sealed.games_seen - before:>7,} games")

    premier = RatingCounters()
    path = download_dataset(FALLBACK_FORMAT, refresh)
    accumulate(path, premier, chunk_size)
    print(f"  {FALLBACK_FORMAT:<13} {premier.games_seen:>7,} games")

    return sealed, premier


# --------------------------------------------------------------------------------------
# Phase 3 -- join and report
# --------------------------------------------------------------------------------------

def resolve_key(key: str, known: set[str], candidates: list[str]) -> tuple[str | None, bool]:
    """Exact match on the normalized name, else a tight fuzzy match. Returns (key, was_fuzzy)."""
    if key in known:
        return key, False
    close = difflib.get_close_matches(key, candidates, n=1, cutoff=0.92)
    return (close[0], True) if close else (None, False)


def join_ratings(cards: pd.DataFrame, sealed: RatingCounters, premier: RatingCounters,
                 min_games: int) -> tuple[pd.DataFrame, list[str], list[tuple[str, str]]]:
    """Left-join ratings onto the card list, preferring sealed data and falling back to
    PremierDraft where the sealed sample is too thin to say anything."""
    print("Phase 3: join")
    rated_keys = {k for k, n in sealed.gih_games.items() if n} \
        | {k for k, n in premier.gih_games.items() if n}
    candidates = sorted(rated_keys)

    matched_rating_keys: set[str] = set()
    fuzzy_hits: list[tuple[str, str]] = []
    rows = []

    for key in cards["join_key"]:
        resolved, was_fuzzy = resolve_key(key, rated_keys, candidates)
        if resolved is None:
            rows.append({"gih_wr": None, "oh_wr": None, "iwd": None, "gih_games": 0,
                         "oh_games": 0, "ratings_source": "none",
                         "sealed_gih_games": 0, "premier_gih_games": 0})
            continue
        matched_rating_keys.add(resolved)
        if was_fuzzy:
            fuzzy_hits.append((key, resolved))

        sealed_n = sealed.gih_games[resolved]
        premier_n = premier.gih_games[resolved]
        if sealed_n >= min_games:
            row = sealed.rates(resolved)
            row["ratings_source"] = "sealed"
        elif premier_n > 0:
            row = premier.rates(resolved)
            row["ratings_source"] = "premier"
        elif sealed_n > 0:
            # Below threshold but it is all we have; label it sealed so the thin count shows.
            row = sealed.rates(resolved)
            row["ratings_source"] = "sealed"
        else:
            row = {"gih_wr": None, "oh_wr": None, "iwd": None, "gih_games": 0, "oh_games": 0,
                   "ratings_source": "none"}
        row["sealed_gih_games"] = sealed_n
        row["premier_gih_games"] = premier_n
        rows.append(row)

    joined = pd.concat([cards.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
    # ALSA is a draft-only signal living in the 155MB draft_data file. The column is kept so the
    # schema stays stable if we ever pull it.
    joined["alsa"] = None

    unmatched_ratings = sorted(rated_keys - matched_rating_keys)
    # Fuzzy matches are surfaced rather than trusted -- at this scale a bad one is easy to spot.
    return joined, unmatched_ratings, fuzzy_hits


def report(joined: pd.DataFrame, unmatched_ratings: list[str],
           fuzzy_hits: list[tuple[str, str]], min_games: int) -> None:
    print("\n" + "=" * 62)
    print("ROW COUNTS PER SET")
    print("=" * 62)
    for set_code in ("sos", "soa", "spg"):
        actual = int((joined["set"] == set_code).sum())
        expected = EXPECTED_COUNTS[set_code]
        flag = "" if actual == expected else f"   <-- expected {expected}"
        print(f"  {set_code.upper():<4} {actual:>4}{flag}")
    print(f"  {'TOTAL':<4} {len(joined):>4}")

    print("\n" + "=" * 62)
    print("UNMATCHED: cards with no 17Lands data")
    print("=" * 62)
    missing = joined[joined["ratings_source"] == "none"]
    if missing.empty:
        print("  (none)")
    else:
        for set_code, group in missing.groupby("set"):
            print(f"  {set_code.upper()}: {len(group)}")
            for name in group["name"].head(12):
                print(f"      {name}")
            if len(group) > 12:
                print(f"      ... and {len(group) - 12} more")

    print("\n" + "=" * 62)
    print("UNMATCHED: 17Lands names with no card")
    print("=" * 62)
    if not unmatched_ratings:
        print("  (none)")
    else:
        for key in unmatched_ratings:
            print(f"      {key}")

    if fuzzy_hits:
        print("\n" + "=" * 62)
        print("FUZZY MATCHES (verify these)")
        print("=" * 62)
        for card_key, rating_key in fuzzy_hits:
            print(f"      {card_key!r} -> {rating_key!r}")

    print("\n" + "=" * 62)
    print("RATINGS COVERAGE")
    print("=" * 62)
    counts = joined["ratings_source"].value_counts()
    for source in ("sealed", "premier", "none"):
        print(f"  {source:<9} {int(counts.get(source, 0)):>4}")
    print(f"  converge cards: {int(joined['has_converge'].sum())}")

    # Falling back to PremierDraft does not guarantee a usable sample -- a card rare enough to be
    # thin in sealed is often thin everywhere. Name them so build.py's scoring can discount them.
    rated = joined[joined["ratings_source"] != "none"]
    thin = rated[rated["gih_games"] < min_games].sort_values("gih_games")
    print(f"\n  LOW CONFIDENCE -- {len(thin)} rated rows under {min_games} GIH games:")
    if thin.empty:
        print("      (none)")
    else:
        for _, row in thin.head(15).iterrows():
            print(f"      {row['gih_games']:>5} games  {row['gih_wr']:.3f}  "
                  f"{row['set'].upper()} {row['rarity'][:1].upper()}  {row['name']}")
        if len(thin) > 15:
            print(f"      ... and {len(thin) - 15} more")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the SOS sealed pool universe CSV.")
    parser.add_argument("--min-games", type=int, default=200,
                        help="sealed GIH games below which PremierDraft is used instead")
    parser.add_argument("--refresh", action="store_true",
                        help="re-download the 17Lands datasets even if cached")
    parser.add_argument("--out", type=Path,
                        default=REPO_ROOT / "data" / "sos_pool_universe.csv")
    parser.add_argument("--chunk-size", type=int, default=2000)
    args = parser.parse_args()

    cards = fetch_card_universe()
    sealed, premier = fetch_ratings(args.refresh, args.chunk_size)
    joined, unmatched_ratings, fuzzy_hits = join_ratings(cards, sealed, premier, args.min_games)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    joined[OUTPUT_COLUMNS].to_csv(args.out, index=False)

    report(joined, unmatched_ratings, fuzzy_hits, args.min_games)
    print(f"\nWrote {len(joined)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
