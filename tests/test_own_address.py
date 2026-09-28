"""
A lead's contact email is the customer's, never one of ours.

Before 2026-09-28, 100 leads carried a Bal Seal address as the contact email.
Two routes led there: the model reading sales@balseal.com off the To: line of
a quarantine digest or a forwarded enquiry, and ingest falling back to the
sender when the model found no address -- and the sender of most of our leads
is the website form relay, leads@bse-web.com. A blank email is a gap a
reviewer fills; one of ours is a lead Oracle files against our own inbox.
"""

import unittest
from unittest.mock import patch

import lead_extractor


class OwnAddressRecognitionTests(unittest.TestCase):
    def test_every_bal_seal_domain_is_ours(self):
        for address in ('sales@balseal.com', 'Sales@BalSeal.com', 'leads@bse-web.com',
                        'mjames@balseal.co.uk', 'svandermaat@balseal.nl',
                        'Sales <sales@balseal.com>', 'relay@mail.balseal.com'):
            self.assertTrue(lead_extractor.is_own_address(address), address)

    def test_a_customer_address_is_not_ours(self):
        """Including look-alikes that merely contain our name."""
        for address in ('ada@example.com', 'buyer@notbalseal.com',
                        'someone@balseal.com.evil.net', '', None):
            self.assertFalse(lead_extractor.is_own_address(address), address)


class ExtractedOwnAddressIsDroppedTests(unittest.TestCase):
    def test_the_inbox_the_enquiry_was_sent_to_is_not_kept(self):
        """The screenshot that prompted this: a digest whose only address was
        the To: line, returned as the contact."""
        fields = lead_extractor.validate_fields({
            'organization': 'Acme Seals', 'email': 'sales@balseal.com',
        })
        self.assertNotIn('email', fields)
        self.assertEqual(fields['organization'], 'Acme Seals')

    def test_a_colleagues_forwarding_address_is_not_kept(self):
        fields = lead_extractor.validate_fields({
            'organization': 'Helix Synchrotron Ltd', 'email': 'mjames@balseal.co.uk',
        })
        self.assertNotIn('email', fields)

    def test_the_customers_own_address_is_kept(self):
        fields = lead_extractor.validate_fields({'email': 'roy@helix-lab.ac.uk'})
        self.assertEqual(fields['email'], 'roy@helix-lab.ac.uk')


@patch('pipeline_ingest.db')
@patch('pipeline_ingest._screen_and_stage')
@patch('pipeline_ingest.hq_lookup')
@patch('pipeline_ingest.location_enrichment')
@patch('pipeline_ingest._fill_website')
@patch('pipeline_ingest.lead_extractor.extract_lead')
class SenderFallbackTests(unittest.TestCase):
    def _ingest(self, extract, sender_email):
        import pipeline_ingest

        extract.return_value = {'is_lead': True, 'confidence': 0.9, 'fields': {}, 'notes': ''}
        pipeline_ingest._process_body_text(1, {
            'id': 'entry-1', 'subject': 'Quote request', 'sender_email': sender_email,
            'sender_name': 'Sender', 'body_text': 'We need a seal.',
        })

    def _stored_lead(self, mock_db):
        return mock_db.insert_lead.call_args.args[0]

    def test_the_web_form_relay_is_never_the_customers_address(
            self, extract, _website, _location, _hq, _stage, mock_db):
        """leads@bse-web.com sends most of our leads; falling back to it put
        our own form relay on every lead the model found no address for."""
        self._ingest(extract, 'leads@bse-web.com')
        self.assertFalse(self._stored_lead(mock_db).get('email'))

    def test_a_customer_sender_still_fills_a_missing_email(
            self, extract, _website, _location, _hq, _stage, mock_db):
        self._ingest(extract, 'ada@example.com')
        self.assertEqual(self._stored_lead(mock_db)['email'], 'ada@example.com')


if __name__ == '__main__':
    unittest.main()
