"""Tests for scripts/match.py.

Standard library only, like the code under test. Run with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import match  # noqa: E402

DATA = REPO_ROOT / "data" / "sos_pool_universe.csv"


def load() -> match.CardIndex:
    return match.load_index(DATA)


class ParseLineTests(unittest.TestCase):
    def test_bare_name(self):
        self.assertEqual(match.parse_line("Rubble Rouser"), (1, "Rubble Rouser"))

    def test_quantity_forms(self):
        for line in ("4 Rubble Rouser", "4x Rubble Rouser", "4 x Rubble Rouser",
                     "x4 Rubble Rouser", "Rubble Rouser x4"):
            with self.subTest(line=line):
                self.assertEqual(match.parse_line(line), (4, "Rubble Rouser"))

    def test_leading_bullet(self):
        self.assertEqual(match.parse_line("- Rubble Rouser"), (1, "Rubble Rouser"))
        self.assertEqual(match.parse_line("• 2 Rubble Rouser"), (2, "Rubble Rouser"))

    def test_trailing_hints_in_either_order(self):
        for line in ("Rubble Rouser (SOS)", "Rubble Rouser #123",
                     "Rubble Rouser (SOS) #123", "Rubble Rouser #123 (SOS)",
                     "Rubble Rouser [foil]"):
            with self.subTest(line=line):
                self.assertEqual(match.parse_line(line), (1, "Rubble Rouser"))

    def test_blanks_and_comments_skipped(self):
        self.assertIsNone(match.parse_line(""))
        self.assertIsNone(match.parse_line("   "))
        self.assertIsNone(match.parse_line("# six packs, pool photo 1"))

    def test_name_containing_digits_survives(self):
        # "Studious First-Year" must not lose anything to the collector-number stripper.
        self.assertEqual(match.parse_line("Studious First-Year"), (1, "Studious First-Year"))


class NormalizationTests(unittest.TestCase):
    def test_matches_fetch_data_contract(self):
        # The doubled space is what 17Lands headers carry; both stages must agree it is one space.
        self.assertEqual(match.normalize_name("Abigale,  Poet Laureate"),
                         match.normalize_name("Abigale, Poet Laureate"))

    def test_curly_apostrophe_folds(self):
        self.assertEqual(match.normalize_name("Teacher’s Pest"),
                         match.normalize_name("Teacher's Pest"))

    def test_squint_folds_lookalikes(self):
        self.assertEqual(match.squint_key("Ulna A11ey Shopkeep"),
                         match.squint_key("Ulna Alley Shopkeep"))
        self.assertEqual(match.squint_key("Torne Blast"), match.squint_key("Tome Blast"))


class MatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = load()

    def resolve(self, name, cutoff=match.DEFAULT_CUTOFF):
        result = match.match_name(name, self.index, cutoff)
        card = self.index.cards[result["index"]]["name"] if result["index"] is not None else None
        return result["status"], card

    def test_exact(self):
        self.assertEqual(self.resolve("Rubble Rouser"), ("exact", "Rubble Rouser"))

    def test_front_face_only_resolves_to_whole_card(self):
        status, card = self.resolve("Abigale, Poet Laureate")
        self.assertEqual(status, "exact")
        self.assertEqual(card, "Abigale, Poet Laureate // Heroic Stanza")

    def test_back_face_resolves_to_whole_card(self):
        status, card = self.resolve("Heroic Stanza")
        self.assertEqual(status, "exact")
        self.assertEqual(card, "Abigale, Poet Laureate // Heroic Stanza")

    def test_ocr_lookalike_digits(self):
        self.assertEqual(self.resolve("Rubb1e Rouser")[1], "Rubble Rouser")
        self.assertEqual(self.resolve("Ulna A11ey Shopkeep")[1], "Ulna Alley Shopkeep")

    def test_ocr_rn_for_m(self):
        self.assertEqual(self.resolve("Torne Blast")[1], "Tome Blast")

    def test_dropped_punctuation(self):
        self.assertEqual(self.resolve("Teachers Pest")[1], "Teacher's Pest")

    def test_truncated_read_resolves_by_prefix(self):
        self.assertEqual(self.resolve("Zaffai and the Temp")[1], "Zaffai and the Tempests")

    def test_dropped_letters(self):
        self.assertEqual(self.resolve("Eite Interceptr")[1], "Elite Interceptor // Rejoinder")

    def test_garbage_is_not_matched(self):
        status, card = self.resolve("Blrrble Zzzqx")
        self.assertEqual(status, "unmatched")
        self.assertIsNone(card)

    def test_short_garbage_does_not_grab_a_short_name(self):
        # "Dzx" scores 0.57 against "Daze"; the short-name bar must reject it.
        status, _ = self.resolve("Dzx")
        self.assertEqual(status, "unmatched")

    def test_unmatched_reports_candidates(self):
        result = match.match_name("Blrrble Zzzqx", self.index, match.DEFAULT_CUTOFF)
        self.assertTrue(result["candidates"])
        self.assertIn("confidence", result["candidates"][0])


class PoolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = load()

    def test_duplicate_reads_merge_and_sum(self):
        entries = [(2, "Rubble Rouser"), (1, "Rubb1e Rouser"), (1, "Rubble Rouser")]
        result = match.resolve_pool(entries, self.index, match.DEFAULT_CUTOFF)
        self.assertEqual(len(result["pool"]), 1)
        self.assertEqual(result["pool"][0]["count"], 4)
        self.assertEqual(result["summary"]["total_count"], 4)

    def test_unmatched_counted_not_dropped_silently(self):
        entries = [(1, "Rubble Rouser"), (1, "Blrrble Zzzqx")]
        result = match.resolve_pool(entries, self.index, match.DEFAULT_CUTOFF)
        self.assertEqual(result["summary"]["unmatched"], 1)
        self.assertEqual(result["summary"]["cards_resolved"], 1)

    def test_card_payload_has_fields_build_py_needs(self):
        result = match.resolve_pool([(1, "Rubble Rouser")], self.index, match.DEFAULT_CUTOFF)
        card = result["pool"][0]
        for field in ("name", "colors", "cmc", "type_line", "rarity", "gih_wr", "count"):
            self.assertIn(field, card)
        self.assertIsInstance(card["cmc"], float)


class FixtureTests(unittest.TestCase):
    """The garbled fixture must round-trip to its answer key.

    A wrong match is far worse than an admitted failure -- it silently puts a card in the pool
    that the player does not own -- so mis-resolutions are held to zero while the resolve rate
    is allowed some slack.
    """

    @classmethod
    def setUpClass(cls):
        cls.index = load()

    @staticmethod
    def read_key(path: Path) -> list[tuple[int, str]]:
        entries = []
        for line in path.read_text(encoding="utf-8").splitlines():
            found = re.match(r"^(\d+)\s+(.*)$", line)
            entries.append((int(found.group(1)), found.group(2)) if found else (1, line))
        return entries

    def test_clean_pool_resolves_completely(self):
        entries = match.read_pool(REPO_ROOT / "tests" / "sample_pool_clean.txt")
        result = match.resolve_pool(entries, self.index, match.DEFAULT_CUTOFF)
        self.assertEqual(result["summary"]["unmatched"], 0)
        self.assertEqual(result["summary"]["ambiguous"], 0)

    def test_garbled_pool_resolves_to_the_answer_key(self):
        entries = match.read_pool(REPO_ROOT / "tests" / "sample_pool_ocr.txt")
        expected = self.read_key(REPO_ROOT / "tests" / "sample_pool_expected.txt")
        self.assertEqual(len(entries), len(expected))

        wrong, unresolved = [], []
        for (_, raw), (_, want) in zip(entries, expected):
            result = match.match_name(raw, self.index, match.DEFAULT_CUTOFF)
            if result["index"] is None:
                unresolved.append(raw)
                continue
            got = self.index.cards[result["index"]]["name"]
            if got != want:
                wrong.append((raw, want, got))

        self.assertEqual(wrong, [], f"mis-resolved reads: {wrong}")
        rate = 1 - len(unresolved) / len(entries)
        self.assertGreaterEqual(rate, 0.90, f"only {rate:.0%} resolved; unresolved: {unresolved}")

    def test_expected_key_covers_every_card_in_the_universe_file(self):
        # Guards against the fixture drifting out of sync with a regenerated CSV.
        expected = self.read_key(REPO_ROOT / "tests" / "sample_pool_expected.txt")
        known = {row["name"] for row in self.index.cards}
        missing = [name for _, name in expected if name not in known]
        self.assertEqual(missing, [], f"fixture names absent from the pool universe: {missing}")


if __name__ == "__main__":
    unittest.main()
