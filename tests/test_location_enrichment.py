import unittest
from unittest.mock import Mock, patch

import location_enrichment


class StatedLocationTests(unittest.TestCase):
    def test_bdi_signature_supplies_the_missing_territory_fields(self):
        body = (
            'Thank you\n\nJay Whalen\n\nBDI Customer Support Specialist\n\n'
            '11506 Enterprise Park Dr.\n\nCincinnati, OH 45241\n\n'
            'Phone: 513-761-0866\n'
        )
        lead = {'email': 'jwhalen@bdiworldwide.com'}

        location_enrichment.enrich_location(lead, body=body)

        self.assertEqual(lead['city'], 'Cincinnati')
        self.assertEqual(lead['state'], 'OH')
        self.assertEqual(lead['zip'], '45241')
        self.assertEqual(lead['country'], 'USA')
        self.assertEqual(lead['address1'], '11506 Enterprise Park Dr.')
        self.assertEqual(lead['_location_source'], 'email signature')

    def test_an_existing_location_is_never_replaced(self):
        lead = {'state': 'TX', 'city': 'Austin'}
        location_enrichment.enrich_location(lead, body='Cincinnati, OH 45241')
        self.assertEqual(lead, {'state': 'TX', 'city': 'Austin'})

    def test_canadian_signature_is_recognized(self):
        found = location_enrichment.extract_stated_location(
            '50 Bay Street\nToronto, ON M5J 2L2\nCanada'
        )
        self.assertEqual(found['state'], 'ON')
        self.assertEqual(found['country'], 'Canada')
        self.assertEqual(found['zip'], 'M5J2L2')


class PageTextTests(unittest.TestCase):
    def test_inline_script_and_style_are_not_page_text(self):
        # A modern corporate page is mostly inline JS. Counting it as text
        # buried the readable part under thousands of characters of feature
        # flags, which is what the model then failed to find an address in.
        page = (
            '<html><head><style>.a{color:red}</style>'
            '<script>var flags="vector-feature-toc-pinned-clientpref-1";</script>'
            '</head><body><p>Headquarters in Beachwood, Ohio</p>'
            '<noscript>Enable JavaScript</noscript></body></html>'
        )

        text, _ = location_enrichment.page_text_and_links(page)

        self.assertIn('Headquarters in Beachwood, Ohio', text)
        self.assertNotIn('clientpref', text)
        self.assertNotIn('color:red', text)
        self.assertNotIn('Enable JavaScript', text)

    def test_table_cells_do_not_run_together(self):
        # An infobox is a table: without a break, its label and value become
        # "HeadquartersDublin, Ireland".
        text, _ = location_enrichment.page_text_and_links(
            '<table><tr><th>Headquarters</th><td>Dublin, Ireland</td></tr></table>'
        )
        self.assertIn('Headquarters\nDublin, Ireland', text)


class CompanyResearchTests(unittest.TestCase):
    @patch('location_enrichment.get_public_page')
    def test_research_follows_a_same_site_contact_page(self, get_page):
        get_page.side_effect = [
            ('<a href="/contact-us">Contact</a>', 'https://acme.example/'),
            ('<address>100 Main St.<br>Austin, TX 78701</address>',
             'https://acme.example/contact-us'),
        ]

        found = location_enrichment.research_company_location('https://acme.example')

        self.assertEqual(found['state'], 'TX')
        self.assertEqual(found['city'], 'Austin')
        self.assertEqual(get_page.call_count, 2)

    @patch('location_enrichment.research_company_location')
    def test_website_research_only_fills_blanks(self, research):
        research.return_value = {
            'city': 'Milwaukee', 'state': 'WI', 'zip': '53202', 'country': 'USA',
        }
        lead = {'website': 'https://acme.example', 'city': 'Customer-stated city'}

        location_enrichment.enrich_location(lead, research=True)

        self.assertEqual(lead['city'], 'Customer-stated city')
        self.assertEqual(lead['state'], 'WI')
        self.assertEqual(lead['_location_source'], 'company website')


if __name__ == '__main__':
    unittest.main()
