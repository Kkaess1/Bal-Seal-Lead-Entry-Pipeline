"""
Integrity tests for the ISO 3166 country table.

The table is hand-maintained, so these guard the two failure modes that matter:
a malformed/duplicated entry, and a country the pipeline actually does business
in (i.e. one territory_map routes) failing to resolve to a code.
"""

import unittest

import country_codes
import territory_map


class TableIntegrityTests(unittest.TestCase):

    def test_codes_are_two_uppercase_letters(self):
        for code in country_codes.NAME_BY_CODE:
            self.assertRegex(code, r'^[A-Z]{2}$')

    def test_names_are_unique(self):
        names = list(country_codes.NAME_BY_CODE.values())
        self.assertEqual(len(names), len(set(names)))

    def test_table_is_roughly_complete(self):
        # ISO 3166-1 currently assigns 249 alpha-2 codes. A table that has
        # drifted far from that has probably lost entries in an edit.
        self.assertGreaterEqual(len(country_codes.NAME_BY_CODE), 245)

    def test_every_alias_points_at_a_real_code(self):
        for alias, code in country_codes.ALIASES.items():
            self.assertIn(code, country_codes.NAME_BY_CODE, f'alias {alias!r}')

    def test_every_official_name_round_trips(self):
        for code, name in country_codes.NAME_BY_CODE.items():
            self.assertEqual(country_codes.to_alpha2(name), code, f'for {name!r}')

    def test_codes_resolve_to_themselves(self):
        for code in country_codes.NAME_BY_CODE:
            self.assertEqual(country_codes.to_alpha2(code), code)


class NormalizationTests(unittest.TestCase):

    def test_punctuation_and_case_are_ignored(self):
        for raw in ('usa', 'U.S.A.', '  United States  ', 'UNITED STATES'):
            self.assertEqual(country_codes.to_alpha2(raw), 'US', f'for {raw!r}')

    def test_accents_are_folded(self):
        self.assertEqual(country_codes.to_alpha2('Côte d\'Ivoire'), 'CI')
        self.assertEqual(country_codes.to_alpha2('Türkiye'), 'TR')
        self.assertEqual(country_codes.to_alpha2('España'), 'ES')

    def test_unknown_returns_none(self):
        for raw in ('Narnia', '', None, '   ', 'ZZ'):
            self.assertIsNone(country_codes.to_alpha2(raw), f'for {raw!r}')

    def test_the_two_congos_stay_distinct(self):
        self.assertEqual(country_codes.to_alpha2('Congo'), 'CG')
        self.assertEqual(country_codes.to_alpha2('Democratic Republic of the Congo'), 'CD')

    def test_display_name_lookup(self):
        self.assertEqual(country_codes.to_display_name('DE'), 'Germany')
        self.assertEqual(country_codes.to_display_name('usa'), 'United States')
        self.assertIsNone(country_codes.to_display_name('Narnia'))


class TerritoryMapCoverageTests(unittest.TestCase):
    """The countries Bal Seal actually assigns a TM to are the ones that must
    never fail to resolve -- an unmapped one there means a real lead reaches
    Oracle with no country."""

    def test_every_routed_country_resolves(self):
        unresolved = [
            name for name in territory_map.COUNTRY_TO_TM
            if country_codes.to_alpha2(name) is None
        ]
        self.assertEqual(unresolved, [], f'unresolved territory countries: {unresolved}')


if __name__ == '__main__':
    unittest.main()
