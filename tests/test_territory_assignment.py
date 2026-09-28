"""
Territory assignment, checked against the sheet itself.

The tables here are transcribed from reference/TM_Territories.pdf ("Sales -
Sales Territory", last updated 09/11/2025) independently of the ones in
territory_map.py, so a typo in either fails rather than silently routing a
lead to the wrong rep. The PDF is a scan with no text layer, so neither copy
can be generated from it -- two transcriptions that agree is the strongest
check available.

Assignment was dead code until 2026-08-10: assign_tm() returned Keaton Kaess
unconditionally, so every lead carried the same owner regardless of geography.
"""

import unittest

import territory_map


# --- US Sales Territory, page 1 of the sheet -------------------------------
# 2026-08-18: John Pan's NORTHEAST territories were transferred to Bill Rost.
NORTHEAST = 'Bill Rost'
WEST = 'Keaton Kaess'
SOCAL = 'John Pan'
SOUTH = 'Mark Copeland'
MIDWEST = 'Wade Kahout'
MIDEAST = 'Bill Rost'

US_STATES = {
    NORTHEAST: ['Connecticut', 'Delaware', 'Massachusetts', 'Maine', 'New Hampshire',
                'New Jersey', 'New York', 'Rhode Island', 'Vermont'],
    WEST: ['Alaska', 'Colorado', 'Hawaii', 'Idaho', 'Montana', 'Oregon', 'Utah',
           'Washington', 'Wyoming'],
    SOCAL: ['Arizona', 'New Mexico'],
    SOUTH: ['Alabama', 'Arkansas', 'Florida', 'Georgia', 'Louisiana', 'Maryland',
            'Mississippi', 'North Carolina', 'Oklahoma', 'South Carolina',
            'Tennessee', 'Texas', 'Virginia'],
    MIDWEST: ['Iowa', 'Minnesota', 'North Dakota', 'Nebraska', 'South Dakota',
              'Wisconsin'],
    MIDEAST: ['Illinois', 'Indiana', 'Kansas', 'Kentucky', 'Michigan', 'Missouri',
              'Ohio', 'West Virginia'],
}

CANADA = {
    NORTHEAST: ['NL', 'NS', 'QC'],
    WEST: ['AB', 'BC', 'NWT', 'SK', 'YT'],
    MIDWEST: ['MB', 'NU'],
    MIDEAST: ['ON'],
}

# --- Rest of world, pages 1-3 ----------------------------------------------
INTERNATIONAL = {
    'Davide Amato': ['Italy', 'Portugal', 'Spain', 'Israel', 'Slovenia', 'Croatia',
                     'Bosnia', 'Montenegro', 'Albania', 'Greece', 'Cyprus', 'Austria',
                     'Morocco', 'Algeria', 'Tunisia'],
    'Marcus James': ['England', 'Ireland', 'Scotland', 'Wales', 'Northern Ireland',
                     'Denmark', 'Finland', 'Norway', 'Sweden'],
    'Ralph Delcourt': ['France', 'Belgium', 'Bulgaria', 'Czech Republic', 'Hungary',
                       'Luxembourg', 'Netherlands', 'Poland', 'Romania', 'Slovakia',
                       'Moldova'],
    'Serdar Eren': ['Germany', 'Switzerland', 'Libya', 'Egypt', 'Nigeria', 'Kenya',
                    'South Africa', 'Saudi Arabia', 'Qatar', 'Ghana', 'Ethiopia'],
    'Alan Lam': ['Korea'],
    'Nozomi Kato': ['Japan'],
    'Sam Tam': ['China', 'Taiwan', 'India', 'Australia', 'New Zealand', 'Singapore',
                'Thailand', 'Vietnam', 'Malaysia', 'Indonesia', 'Philippines'],
    # The PDF says Lisa Wolf here. She left the company on 2026-08-10 and the
    # Americas passed to John Pan; the sheet is a scan and has not been
    # reissued, so this intentionally differs from it. See territory_map.py.
    'John Pan': ['Mexico', 'Guatemala', 'Nicaragua', 'Costa Rica', 'Cuba',
                 'Dominican Republic', 'Puerto Rico', 'Colombia', 'Ecuador', 'Peru',
                 'Suriname', 'Brazil', 'Bolivia', 'Chile', 'Paraguay', 'Argentina',
                 'Uruguay'],
}

# Marked "NA" on the sheet -- a real answer (no TM), not a gap.
UNASSIGNED_COUNTRIES = ['Venezuela', 'Iran', 'Iraq', 'Syria', 'Afghanistan']


class UsTerritoryTests(unittest.TestCase):
    def test_every_state_on_the_sheet_routes_to_its_tm(self):
        for expected_tm, states in US_STATES.items():
            for state in states:
                tm, basis = territory_map.assign_tm({'state': state, 'country': 'USA'})
                self.assertEqual(tm, expected_tm, f'{state} -> {tm} ({basis})')

    def test_state_abbreviations_work_too(self):
        # Extraction usually returns "OH", not "Ohio".
        for state, expected in (('OH', MIDEAST), ('TX', SOUTH), ('MA', NORTHEAST),
                                ('AZ', SOCAL), ('WI', MIDWEST), ('CO', WEST)):
            tm, _ = territory_map.assign_tm({'state': state, 'country': 'USA'})
            self.assertEqual(tm, expected, state)

    def test_a_missing_country_is_treated_as_domestic(self):
        # Plenty of US emails never say "USA".
        tm, _ = territory_map.assign_tm({'state': 'OH'})
        self.assertEqual(tm, MIDEAST)

    def test_california_splits_on_zip(self):
        # "CA Zip 93599 & Below" -> John Pan, "93600 & Above" -> Keaton Kaess.
        self.assertEqual(
            territory_map.assign_tm({'state': 'CA', 'zip': '92610'})[0], SOCAL)
        self.assertEqual(
            territory_map.assign_tm({'state': 'CA', 'zip': '93599'})[0], SOCAL)
        self.assertEqual(
            territory_map.assign_tm({'state': 'CA', 'zip': '93600'})[0], WEST)
        self.assertEqual(
            territory_map.assign_tm({'state': 'CA', 'zip': '95120'})[0], WEST)

    def test_pennsylvania_routes_to_bill_rost(self):
        # All Pennsylvania ZIPs (both <= 16999 and >= 17000) -> Bill Rost.
        self.assertEqual(
            territory_map.assign_tm({'state': 'PA', 'zip': '15201'})[0], MIDEAST)
        self.assertEqual(
            territory_map.assign_tm({'state': 'PA', 'zip': '16999'})[0], MIDEAST)
        self.assertEqual(
            territory_map.assign_tm({'state': 'PA', 'zip': '17000'})[0], MIDEAST)
        self.assertEqual(
            territory_map.assign_tm({'state': 'PA', 'zip': '19104'})[0], MIDEAST)

    def test_a_split_state_without_a_zip_asks_for_review(self):
        # Guessing here would hand the lead to the wrong half of the state.
        for state in ('CA',):
            tm, basis = territory_map.assign_tm({'state': state})
            self.assertIsNone(tm, state)
            self.assertIn('ZIP', basis, state)

    def test_pennsylvania_without_a_zip_routes_to_bill_rost(self):
        tm, basis = territory_map.assign_tm({'state': 'PA'})
        self.assertEqual(tm, MIDEAST, basis)

    def test_nevada_splits_on_clark_county(self):
        # Clark County -> John Pan; the rest of Nevada -> Keaton Kaess.
        vegas, basis = territory_map.assign_tm({'state': 'NV', 'zip': '89101'})
        self.assertEqual(vegas, SOCAL, basis)
        reno, basis = territory_map.assign_tm({'state': 'NV', 'zip': '89501'})
        self.assertEqual(reno, WEST, basis)

    def test_states_written_the_way_people_write_them(self):
        # A real lead arrived with "Tn." and went unassigned.
        for written in ('Tn.', 'tn', ' TN ', 'Tennessee', 'tennessee '):
            tm, basis = territory_map.assign_tm({'state': written})
            self.assertEqual(tm, SOUTH, f'{written!r} -> {tm} ({basis})')

    def test_a_zip_from_a_different_state_is_not_silently_split(self):
        # A real lead had state "Pennsylvania" with ZIP 38133 (Bartlett, TN).
        # The split rule would have handed it to the Northeast rep.
        tm, basis = territory_map.assign_tm(
            {'state': 'Pennsylvania', 'zip': '38133', 'country': 'United States'})

        self.assertIsNone(tm)
        self.assertIn('38133', basis)
        self.assertIn('disagree', basis)

    def test_a_matching_zip_still_splits_normally(self):
        # The guard must not break the ordinary case.
        self.assertEqual(territory_map.assign_tm({'state': 'PA', 'zip': '15201'})[0], MIDEAST)
        self.assertEqual(territory_map.assign_tm({'state': 'CA', 'zip': '92610'})[0], SOCAL)

    def test_an_unsplit_state_is_unaffected_by_its_zip(self):
        # Only CA and PA are split, so only they can be misrouted this way --
        # an odd ZIP elsewhere should not block an otherwise clear assignment.
        tm, _ = territory_map.assign_tm({'state': 'OH', 'zip': '99999'})
        self.assertEqual(tm, MIDEAST)

    def test_no_state_is_not_guessed(self):
        tm, basis = territory_map.assign_tm({'country': 'USA'})
        self.assertIsNone(tm)
        self.assertIn('state', basis.lower())


class CanadaTerritoryTests(unittest.TestCase):
    def test_every_province_on_the_sheet_routes_to_its_tm(self):
        for expected_tm, provinces in CANADA.items():
            for province in provinces:
                tm, basis = territory_map.assign_tm(
                    {'state': province, 'country': 'Canada'})
                self.assertEqual(tm, expected_tm, f'{province} -> {tm} ({basis})')


class InternationalTerritoryTests(unittest.TestCase):
    def test_every_country_on_the_sheet_routes_to_its_tm(self):
        for expected_tm, countries in INTERNATIONAL.items():
            for country in countries:
                tm, basis = territory_map.assign_tm({'country': country})
                self.assertEqual(tm, expected_tm, f'{country} -> {tm} ({basis})')

    def test_countries_marked_na_return_no_tm_with_a_reason(self):
        for country in UNASSIGNED_COUNTRIES:
            tm, basis = territory_map.assign_tm({'country': country})
            self.assertIsNone(tm, country)
            self.assertTrue(basis, country)

    def test_a_country_not_on_the_sheet_is_not_guessed(self):
        tm, basis = territory_map.assign_tm({'country': 'Atlantis'})
        self.assertIsNone(tm)
        self.assertTrue(basis)


class SalesRepTests(unittest.TestCase):
    """The CRR shown on a lead, from reference/Sales_List.pdf.

    Reference only: Oracle's Owner is the Territory Manager, and this is
    computed for display rather than stored so it cannot reach a payload.
    """

    # Spot-checks transcribed from the sheet, chosen to cover each rep and
    # each rule rather than to repeat the whole table.
    US_SPOT_CHECKS = {
        'Teresa Meier': ['AK', 'AL', 'CO', 'FL', 'GA', 'HI', 'ID', 'MS', 'MT', 'NC',
                         'ND', 'OR', 'SC', 'SD', 'UT', 'WA', 'WY'],
        'Barbara Stubbs': ['AR', 'IA', 'IL', 'KS', 'LA', 'MN', 'MO', 'NM', 'OK',
                           'TX', 'WI'],
        'Trinidad Bivian': ['AZ', 'NE'],
        'Claudine Allen': ['CT', 'DE', 'MA', 'MD', 'ME', 'NH', 'NJ', 'NY', 'RI',
                           'VA', 'VT', 'WV'],
        'Alicia Sabillon': ['IN', 'KY', 'MI', 'OH', 'TN'],
    }

    def test_each_state_maps_to_the_rep_on_the_sheet(self):
        for expected, states in self.US_SPOT_CHECKS.items():
            for state in states:
                crr, basis = territory_map.assign_crr({'state': state}, 'irrelevant')
                self.assertEqual(crr, expected, f'{state} -> {crr} ({basis})')

    def test_california_splits_the_same_way_the_tm_does(self):
        self.assertEqual(
            territory_map.assign_crr({'state': 'CA', 'zip': '92610'}, None)[0],
            'Trinidad Bivian')
        self.assertEqual(
            territory_map.assign_crr({'state': 'CA', 'zip': '95120'}, None)[0],
            'Teresa Meier')

    def test_pennsylvania_does_not_split_even_though_the_tm_does(self):
        # Both halves are Claudine Allen on the sales list.
        for zip_code in ('15201', '19104'):
            self.assertEqual(
                territory_map.assign_crr({'state': 'PA', 'zip': zip_code}, None)[0],
                'Claudine Allen', zip_code)

    def test_nevada_splits_on_clark_county(self):
        self.assertEqual(
            territory_map.assign_crr({'state': 'NV', 'zip': '89101'}, None)[0],
            'Trinidad Bivian')
        self.assertEqual(
            territory_map.assign_crr({'state': 'NV', 'zip': '89501'}, None)[0],
            'Teresa Meier')

    def test_canada_follows_the_sales_list_groups(self):
        for province, expected in (('AB', 'Teresa Meier'), ('BC', 'Teresa Meier'),
                                   ('SK', 'Teresa Meier'), ('MB', 'Claudine Allen'),
                                   ('NU', 'Claudine Allen'), ('ON', 'Claudine Allen'),
                                   ('QC', 'Claudine Allen'), ('NS', 'Claudine Allen')):
            crr, basis = territory_map.assign_crr(
                {'state': province, 'country': 'Canada'}, None)
            self.assertEqual(crr, expected, f'{province} -> {crr} ({basis})')

    def test_emea_is_elif_unless_it_is_serdars_territory(self):
        for tm in ('Davide Amato', 'Marcus James', 'Ralph Delcourt'):
            crr, basis = territory_map.assign_crr({'country': 'Italy'}, tm)
            self.assertEqual(crr, 'Elif', f'{tm} -> {crr} ({basis})')

        crr, basis = territory_map.assign_crr({'country': 'Germany'}, 'Serdar Eren')
        self.assertEqual(crr, 'Sandra van der Maat')
        self.assertIn('Serdar', basis)

    def test_latin_america_and_puerto_rico_come_from_special_assignments(self):
        for country in ('Mexico', 'Brazil', 'Colombia', 'Argentina'):
            self.assertEqual(
                territory_map.assign_crr({'country': country}, 'John Pan')[0],
                'Alicia Sabillon', country)
        self.assertEqual(
            territory_map.assign_crr({'country': 'Puerto Rico'}, None)[0],
            'Trinidad Bivian')

    def test_asia_is_alan_lam(self):
        for country, tm in (('Japan', 'Nozomi Kato'), ('Korea', 'Alan Lam'),
                            ('China', 'Sam Tam'), ('India', 'Sam Tam'),
                            ('Singapore', 'Sam Tam')):
            crr, basis = territory_map.assign_crr({'country': country}, tm)
            self.assertEqual(crr, 'Alan Lam', f'{country} -> {crr} ({basis})')
            self.assertIn('Asia', basis)

    def test_oceania_follows_asia(self):
        # The territory sheet files Australia and New Zealand under its Asia
        # section with Sam Tam rather than giving them a region of their own.
        for country in ('Australia', 'New Zealand'):
            self.assertEqual(
                territory_map.assign_crr({'country': country}, 'Sam Tam')[0],
                'Alan Lam', country)

    def test_somewhere_on_no_list_at_all_is_not_guessed(self):
        crr, basis = territory_map.assign_crr({'country': 'Atlantis'}, None)
        self.assertIsNone(crr)
        self.assertIn('not on the sales list', basis)

    def test_no_location_says_so(self):
        crr, basis = territory_map.assign_crr({}, None)
        self.assertIsNone(crr)
        self.assertIn('No state on file', basis)

    def test_the_rep_is_never_a_lead_column(self):
        # Storing it would risk it reaching Oracle; it is display-only.
        import db
        self.assertNotIn('sales_rep', db.LEAD_SCHEMA_FIELDS)
        self.assertNotIn('crr', db.LEAD_SCHEMA_FIELDS)


class AssignmentIsLiveTests(unittest.TestCase):
    def test_the_owner_is_no_longer_hardcoded(self):
        """Regression: assign_tm() returned Keaton Kaess for every lead, so
        geography never reached Oracle's Owner field."""
        leads = [
            {'state': 'OH', 'country': 'USA'},
            {'country': 'Germany'},
            {'state': 'TX'},
        ]
        owners = {territory_map.assign_tm(lead)[0] for lead in leads}

        self.assertNotIn('Keaton Kaess', owners)
        self.assertEqual(owners, {'Bill Rost', 'Serdar Eren', 'Mark Copeland'})

    def test_the_basis_explains_the_choice(self):
        _, basis = territory_map.assign_tm({'state': 'CA', 'zip': '92610'})
        self.assertIn('92610', basis)


if __name__ == '__main__':
    unittest.main()
