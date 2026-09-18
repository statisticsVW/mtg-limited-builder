"""Resolve noisy card names read off pool photos to canonical Secrets of Strixhaven cards.

STANDARD LIBRARY ONLY. This script is bundled into skill/ and runs in the claude.ai sandbox,
where package installs may be blocked. Do not add third-party imports. See CLAUDE.md.

This does not do OCR. In the skill, Claude reads the pool photos with vision and emits candidate
names, one per line; this script turns those noisy strings into canonical cards with counts, and
flags the ones it could not resolve confidently. Restricting the search to a single set's ~419
aliases is what makes plain difflib good enough.

Input lines are forgiving:

    4 Rubble Rouser
    2x Pursue the Past
    Abigale, Poet Laureate
    - Eite Interceptr          <- misread, resolved by fuzzy match
    Emeritus of Truce          <- front face only, resolves to the full two-faced card

Usage:
    python match.py --pool pool.txt
    python match.py --pool pool.txt --format json > pool.json
    cat pool.txt | python match.py
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_CANDIDATES = [
    SCRIPT_DIR.parent / "data" / "sos_pool_universe.csv",  # repo layout
    SCRIPT_DIR / "data" / "sos_pool_universe.csv",         # bundled beside the script
    SCRIPT_DIR.parent / "sos_pool_universe.csv",
]

DEFAULT_CUTOFF = 0.80
# A four-letter name like "Daze" reaches a high difflib ratio against almost anything, so short
# names have to clear a higher bar before we believe the match.
SHORT_NAME_CHARS = 8
SHORT_NAME_PENALTY = 0.08
# If the runner-up is this close to the winner, the read is not decisive enough to pick for you.
AMBIGUITY_MARGIN = 0.04

QUANTITY_PATTERNS = [
    re.compile(r"^\s*(\d+)\s*[xX]\s*(.+)$"),   # "4x Rubble Rouser"
    re.compile(r"^\s*[xX]\s*(\d+)\s+(.+)$"),   # "x4 Rubble Rouser"
    re.compile(r"^\s*(\d+)\s+(.+)$"),          # "4 Rubble Rouser"
]
TRAILING_QUANTITY = re.compile(r"^(.+?)\s+[xX]\s*(\d+)\s*$")  # "Rubble Rouser x4"
# Set codes and collector numbers often ride along on a read; they are hints, not part of the
# name. They can arrive in either order ("Foo (SOS) #123"), so they are stripped repeatedly.
TRAILING_HINTS = [
    re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$"),  # (SOS), [foil]
    re.compile(r"\s*#\s*\d+[a-z]?\s*$"),         # #123, #123a
]
LEADING_BULLET = re.compile(r"^\s*[-*•–—]\s+")

# Characters a camera and a language model confuse for one another, folded to one representative.
SQUINT_TABLE = str.maketrans({
    "1": "i", "l": "i", "|": "i", "!": "i",
    "0": "o", "5": "s", "8": "b", "6": "g", "9": "g", "2": "z",
})


# --------------------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------------------

def normalize_name(raw: str) -> str:
    """Reduce a card name to a stable key.

    Kept byte-for-byte consistent with fetch_data.normalize_name so the two stages agree on
    what counts as the same card.
    """
    text = unicodedata.normalize("NFKD", raw)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("—", "-").replace("–", "-")
    text = re.sub(r"\s+", " ", text)
    return text.strip().casefold()


def squint_key(raw: str) -> str:
    """A deliberately lossy key that collapses common misreads.

    Folds letter/digit lookalikes, the classic rn/m and vv/w pairs, and drops punctuation
    entirely -- apostrophes and commas are the first things to vanish from a photo read.
    """
    text = normalize_name(raw).replace("rn", "m").replace("vv", "w")
    return re.sub(r"[^a-z0-9]", "", text.translate(SQUINT_TABLE))


def ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


# --------------------------------------------------------------------------------------
# Card index
# --------------------------------------------------------------------------------------

class CardIndex:
    """The set's cards, indexed by every name they might be read as.

    A two-faced card answers to its front face, its back face, and the combined "A // B" name,
    because a photo shows only the front but a typed list may carry either.
    """

    KEEP_FIELDS = ("name", "set", "rarity", "colors", "color_identity", "cmc", "mana_cost",
                   "type_line", "is_basic_land", "gih_wr", "iwd", "gih_games", "ratings_source")

    def __init__(self, rows: list[dict]) -> None:
        self.cards = rows
        self.by_alias: dict[str, int] = {}
        self.by_squint: dict[str, set[int]] = defaultdict(set)
        self.alias_keys: list[str] = []

        for index, row in enumerate(rows):
            for alias in self._aliases(row):
                key = normalize_name(alias)
                if not key:
                    continue
                # The universe has no alias collisions; first writer wins if that ever changes.
                self.by_alias.setdefault(key, index)
                self.by_squint[squint_key(alias)].add(index)
        self.alias_keys = sorted(self.by_alias)

    @staticmethod
    def _aliases(row: dict) -> list[str]:
        aliases = [row["name"], row.get("face_1_name") or ""]
        if row.get("face_2_name"):
            aliases.append(row["face_2_name"])
        return [a for a in aliases if a]

    def card(self, index: int) -> dict:
        row = self.cards[index]
        out = {}
        for field in self.KEEP_FIELDS:
            value = row.get(field, "")
            if field in ("cmc", "gih_wr", "iwd"):
                out[field] = float(value) if value not in ("", None) else None
            elif field == "gih_games":
                out[field] = int(float(value)) if value not in ("", None) else 0
            elif field == "is_basic_land":
                out[field] = str(value).strip().lower() == "true"
            else:
                out[field] = value
        return out


def load_index(path: Path | None) -> CardIndex:
    if path is None:
        for candidate in DATA_CANDIDATES:
            if candidate.exists():
                path = candidate
                break
        else:
            raise SystemExit(
                "Could not find sos_pool_universe.csv. Run scripts/fetch_data.py first, "
                "or pass --data."
            )
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit(f"{path} has no rows.")
    return CardIndex(rows)


# --------------------------------------------------------------------------------------
# Input parsing
# --------------------------------------------------------------------------------------

def parse_line(line: str) -> tuple[int, str] | None:
    """Split a pool line into (count, name). Returns None for blanks and comments."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None

    text = LEADING_BULLET.sub("", text)
    count = 1
    for pattern in QUANTITY_PATTERNS:
        found = pattern.match(text)
        if found:
            count, text = int(found.group(1)), found.group(2)
            break
    else:
        found = TRAILING_QUANTITY.match(text)
        if found:
            text, count = found.group(1), int(found.group(2))

    changed = True
    while changed:
        changed = False
        for pattern in TRAILING_HINTS:
            stripped = pattern.sub("", text)
            if stripped != text:
                text, changed = stripped, True
    text = text.strip(" .,\t")
    return (count, text) if text else None


def read_pool(source: Path | None) -> list[tuple[int, str]]:
    raw = source.read_text(encoding="utf-8") if source else sys.stdin.read()
    entries = []
    for line in raw.splitlines():
        parsed = parse_line(line)
        if parsed:
            entries.append(parsed)
    return entries


# --------------------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------------------

def effective_cutoff(name: str, cutoff: float) -> float:
    if len(normalize_name(name)) < SHORT_NAME_CHARS:
        return min(0.95, cutoff + SHORT_NAME_PENALTY)
    return cutoff


def match_name(name: str, index: CardIndex, cutoff: float) -> dict:
    """Resolve one read to a card, in order of how much we trust the evidence."""
    key = normalize_name(name)
    if not key:
        return {"status": "unmatched", "index": None, "confidence": 0.0, "candidates": []}

    if key in index.by_alias:
        return {"status": "exact", "index": index.by_alias[key],
                "confidence": 1.0, "candidates": [], "method": "exact"}

    squint = squint_key(name)
    hits = index.by_squint.get(squint, set())
    if len(hits) == 1:
        found = next(iter(hits))
        # An exact hit after folding lookalikes is strong evidence, so the raw ratio (which
        # still sees the misread characters) gets a floor rather than being taken at face value.
        best = max((ratio(key, normalize_name(alias))
                    for alias in CardIndex._aliases(index.cards[found])), default=0.0)
        return {"status": "matched", "index": found,
                "confidence": round(max(best, 0.90), 4),
                "candidates": [], "method": "squint"}

    # A read cut off by the edge of a photo or a card sleeve: unique prefix wins.
    prefixed = [k for k in index.alias_keys if k.startswith(key)]
    if len({index.by_alias[k] for k in prefixed}) == 1:
        return {"status": "matched", "index": index.by_alias[prefixed[0]],
                "confidence": round(len(key) / len(prefixed[0]), 4),
                "candidates": [], "method": "prefix"}

    bar = effective_cutoff(name, cutoff)
    scored = sorted(((ratio(key, alias), alias) for alias in index.alias_keys), reverse=True)
    best_score, best_alias = scored[0]
    candidates = [{"name": index.cards[index.by_alias[alias]]["name"],
                   "confidence": round(score, 4)} for score, alias in scored[:3]]

    if best_score < bar:
        return {"status": "unmatched", "index": None,
                "confidence": round(best_score, 4), "candidates": candidates,
                "method": "fuzzy"}

    runner_up = next((s for s, alias in scored[1:]
                      if index.by_alias[alias] != index.by_alias[best_alias]), 0.0)
    if best_score - runner_up < AMBIGUITY_MARGIN:
        return {"status": "ambiguous", "index": index.by_alias[best_alias],
                "confidence": round(best_score, 4), "candidates": candidates,
                "method": "fuzzy"}

    return {"status": "matched", "index": index.by_alias[best_alias],
            "confidence": round(best_score, 4), "candidates": candidates[1:],
            "method": "fuzzy"}


def resolve_pool(entries: list[tuple[int, str]], index: CardIndex, cutoff: float) -> dict:
    """Resolve every line, merging duplicate reads of the same card into one count."""
    pool: dict[int, dict] = {}
    problems: list[dict] = []

    for count, raw in entries:
        result = match_name(raw, index, cutoff)
        if result["index"] is None:
            problems.append({"input": raw, "count": count, "status": result["status"],
                             "confidence": result["confidence"],
                             "candidates": result["candidates"]})
            continue

        card_index = result["index"]
        entry = pool.setdefault(card_index, {
            **index.card(card_index),
            "count": 0,
            "reads": [],
            "min_confidence": 1.0,
        })
        entry["count"] += count
        entry["reads"].append({"input": raw, "confidence": result["confidence"],
                               "method": result["method"], "status": result["status"]})
        entry["min_confidence"] = min(entry["min_confidence"], result["confidence"])

        if result["status"] == "ambiguous":
            problems.append({"input": raw, "count": count, "status": "ambiguous",
                             "confidence": result["confidence"],
                             "candidates": result["candidates"],
                             "assumed": index.cards[card_index]["name"]})

    resolved = sorted(pool.values(), key=lambda c: (c["name"]))
    return {
        "pool": resolved,
        "problems": problems,
        "summary": {
            "lines_read": len(entries),
            "cards_resolved": len(resolved),
            "total_count": sum(c["count"] for c in resolved),
            "unmatched": sum(1 for p in problems if p["status"] == "unmatched"),
            "ambiguous": sum(1 for p in problems if p["status"] == "ambiguous"),
        },
    }


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------

def front_cost(mana_cost: str) -> str:
    """Front-face cost only -- a combined "A // B" cost overflows the column and reads as broken."""
    return (mana_cost or "").split(" // ")[0][:12]


def print_table(result: dict) -> None:
    pool, problems, summary = result["pool"], result["problems"], result["summary"]

    print(f"{'#':>3}  {'CARD':<42} {'SET':<4} {'R':<2} {'COST':<12} {'GIH':<6} CONF")
    print("-" * 86)
    for card in pool:
        gih = f"{card['gih_wr']:.3f}" if card["gih_wr"] is not None else "  -  "
        flag = "" if card["min_confidence"] >= 0.999 else f" {card['min_confidence']:.2f}"
        print(f"{card['count']:>3}  {card['name'][:42]:<42} {card['set'].upper():<4} "
              f"{card['rarity'][:1].upper():<2} {front_cost(card['mana_cost']):<12} "
              f"{gih:<6}{flag}")

    if problems:
        print()
        print("NEEDS A HUMAN")
        print("-" * 86)
        for problem in problems:
            if problem["status"] == "ambiguous":
                print(f"  ambiguous: {problem['input']!r} -> assumed {problem['assumed']!r}")
            else:
                print(f"  unmatched: {problem['input']!r} (best {problem['confidence']:.2f})")
            for candidate in problem["candidates"]:
                print(f"       maybe {candidate['name']!r} ({candidate['confidence']:.2f})")

    print()
    print(f"{summary['total_count']} cards over {summary['cards_resolved']} distinct names "
          f"from {summary['lines_read']} lines; "
          f"{summary['unmatched']} unmatched, {summary['ambiguous']} ambiguous")


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve pool photo reads to SOS cards.")
    parser.add_argument("--pool", type=Path, help="file of card names; omit to read stdin")
    parser.add_argument("--data", type=Path, help="path to sos_pool_universe.csv")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--cutoff", type=float, default=DEFAULT_CUTOFF,
                        help=f"fuzzy match threshold (default {DEFAULT_CUTOFF})")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero if anything is unmatched or ambiguous")
    args = parser.parse_args()

    index = load_index(args.data)
    entries = read_pool(args.pool)
    if not entries:
        raise SystemExit("No card names on input.")

    result = resolve_pool(entries, index, args.cutoff)

    if args.format == "json":
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print_table(result)

    if args.strict and (result["summary"]["unmatched"] or result["summary"]["ambiguous"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
