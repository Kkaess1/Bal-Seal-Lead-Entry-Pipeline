"""
What gets sent to the model, and what must survive the trip.

compact_for_ai() exists because the extractor was being handed the raw body --
HTML flattened to text, tracking URLs and all. It cannot simply reuse
clean_email_body(): that is built for a human reader and drops the two things
the extractor most needs, quoted history and a form's contact rows.
"""

import unittest
from unittest.mock import patch

import email_cleaner


class CompactionKeepsWhatTheExtractorNeedsTests(unittest.TestCase):
    def test_quoted_history_survives(self):
        # A reply's company and email often exist only in the quoted original:
        # "RE: HOT RFQ" yielded its fields that way and nowhere else.
        body = (
            'Quick process question about the To/Cc line.\n\n'
            'From: Terri Taggart <terri.taggart@motion.com>\n'
            'Sent: Friday, August 7, 2026 8:01 AM\n'
            'To: Sales\n'
            'Subject: HOT RFQ\n\n'
            'We need a quote. Terri Taggart, Motion, (555) 123-4567\n'
        )

        compacted = email_cleaner.compact_for_ai(body)

        for needle in ('Terri Taggart', 'motion.com', 'Motion', '(555) 123-4567'):
            self.assertIn(needle, compacted, needle)
        # ...whereas the display cleaner deliberately throws that away.
        self.assertNotIn('motion.com', email_cleaner.clean_email_body(body))

    def test_form_contact_rows_survive(self):
        body = (
            'Name\tAda Lovelace\n'
            'Company\tAnalytical Engines\n'
            'Email\tada@example.com\n'
            'Phone\t(555) 987-6543\n'
            'Application\tRotary seal for a test rig\n'
        )

        compacted = email_cleaner.compact_for_ai(body)

        for needle in ('Ada Lovelace', 'ada@example.com', '(555) 987-6543'):
            self.assertIn(needle, compacted, needle)

    def test_a_plain_signature_is_kept_intact(self):
        body = 'Please quote.\n\nRegards,\nAda Lovelace\nAnalytical Engines\n+1 555 987 6543\n'

        self.assertIn('+1 555 987 6543', email_cleaner.compact_for_ai(body))


class CompactionRemovesNoiseTests(unittest.TestCase):
    def test_tracking_urls_and_banners_go(self):
        body = (
            'CAUTION: This email originated from outside of the organization.\n'
            'Hello, we need a seal.\n'
            'https://nam12.safelinks.protection.outlook.com/?url=' + 'x' * 200 + '\n'
            'Ada <mailto:ada@example.com>\n'
        )

        compacted = email_cleaner.compact_for_ai(body)

        self.assertIn('we need a seal', compacted)
        self.assertNotIn('CAUTION:', compacted)
        self.assertNotIn('safelinks', compacted)
        self.assertNotIn('mailto:', compacted)
        # The address itself stays -- only the duplicate mailto link goes.
        self.assertIn('Ada', compacted)

    def test_long_legal_disclaimers_go(self):
        body = (
            'We need a quote.\n\n'
            'This message is confidential and intended solely for the intended '
            'recipient. If you are not the intended recipient please notify the '
            'sender and delete it. Any unauthorised disclosure is prohibited.\n'
        )

        compacted = email_cleaner.compact_for_ai(body)

        self.assertIn('We need a quote.', compacted)
        self.assertNotIn('unauthorised disclosure', compacted)


class CompactionCapsTests(unittest.TestCase):
    def test_an_over_long_body_keeps_its_head_and_its_tail(self):
        # The new message sits at the top of a reply chain and the signature
        # with the phone number at the bottom, so a plain truncation would
        # lose exactly what the extractor is looking for.
        body = 'NEW MESSAGE AT TOP\n' + ('filler line\n' * 5000) + 'Ada, (555) 111-2222\n'

        compacted = email_cleaner.compact_for_ai(body, max_chars=2000)

        self.assertIn('NEW MESSAGE AT TOP', compacted)
        self.assertIn('(555) 111-2222', compacted)
        self.assertIn('characters of quoted thread omitted', compacted)
        self.assertLess(len(compacted), 2400)

    def test_a_short_body_is_returned_whole(self):
        body = 'Thanks, received.'

        self.assertEqual(email_cleaner.compact_for_ai(body), body)

    def test_empty_input_is_safe(self):
        self.assertEqual(email_cleaner.compact_for_ai(''), '')
        self.assertIsNone(email_cleaner.compact_for_ai(None))


class ExtractorIsGivenTheCompactBodyTests(unittest.TestCase):
    @patch('pipeline_ingest.db')
    @patch('pipeline_ingest._screen_and_stage')
    @patch('pipeline_ingest.lead_extractor.extract_lead')
    def test_the_raw_body_is_never_sent_to_the_model(self, extract, stage, mock_db):
        import pipeline_ingest

        extract.return_value = {'is_lead': True, 'confidence': 0.9, 'fields': {}, 'notes': ''}
        raw = (
            'We need a seal.\n'
            'https://nam12.safelinks.protection.outlook.com/?url=' + 'x' * 300 + '\n'
        )

        pipeline_ingest._process_body_text(1, {
            'id': 'entry-1', 'subject': 'Quote', 'sender_email': 'ada@example.com',
            'sender_name': 'Ada', 'body_text': raw,
        })

        sent = extract.call_args.kwargs['body']
        self.assertIn('We need a seal.', sent)
        self.assertNotIn('safelinks', sent)
        self.assertLess(len(sent), len(raw))


if __name__ == '__main__':
    unittest.main()
