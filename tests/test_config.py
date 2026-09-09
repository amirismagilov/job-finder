from pathlib import Path
import tempfile
import unittest

from job_finder.config import (
    DEFAULT_EXCLUDED_EMPLOYER_TERMS,
    DEFAULT_EXCLUDED_INDUSTRY_TERMS,
    load_config,
)


class SearchExclusionConfigTest(unittest.TestCase):
    def load_search(self, search_lines: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(f'[search]\nqueries = ["AI Lead"]\n{search_lines}\n', encoding="utf-8")
            return load_config(path).search

    def test_defaults_are_mandatory_even_when_lists_are_absent_or_empty(self):
        absent = self.load_search("")
        empty = self.load_search("excluded_employer_terms = []\nexcluded_industry_terms = []")
        for search in (absent, empty):
            self.assertTrue(set(DEFAULT_EXCLUDED_EMPLOYER_TERMS).issubset(search.excluded_employer_terms))
            self.assertTrue(set(DEFAULT_EXCLUDED_INDUSTRY_TERMS).issubset(search.excluded_industry_terms))
            self.assertNotIn("bet", {term.casefold() for term in search.excluded_industry_terms})

    def test_custom_terms_extend_defaults_and_are_deduplicated(self):
        search = self.load_search(
            'excluded_employer_terms = ["Acme Group", "СБЕР"]\n'
            'excluded_industry_terms = ["игровые автоматы", "FONBET"]'
        )
        self.assertIn("Acme Group", search.excluded_employer_terms)
        self.assertIn("игровые автоматы", search.excluded_industry_terms)
        self.assertEqual(sum(term.casefold() == "сбер" for term in search.excluded_employer_terms), 1)
        self.assertEqual(sum(term.casefold() == "fonbet" for term in search.excluded_industry_terms), 1)

    def test_invalid_exclusion_lists_fail_closed(self):
        too_many = ", ".join(f'"marker-{index}"' for index in range(101))
        cases = (
            'excluded_employer_terms = "Sber"',
            "excluded_employer_terms = [1]",
            'excluded_employer_terms = ["   "]',
            f'excluded_employer_terms = ["{"x" * 101}"]',
            f"excluded_employer_terms = [{too_many}]",
            'excluded_industry_terms = ["bet"]',
            'excluded_industry_terms = ["ＢＥＴ!"]',
            'excluded_industry_terms = ["---"]',
        )
        for search_lines in cases:
            with self.subTest(search_lines=search_lines[:80]):
                with self.assertRaises(RuntimeError):
                    self.load_search(search_lines)


if __name__ == "__main__":
    unittest.main()
