"""
Showing the rest of a thread when the rest of the thread isn't in the mailbox.

A "Re:" that reaches the Sales inbox is often the only message of its
conversation there -- the exchange it answers happened from a rep's own
mailbox. Verified on 2026-08-10 across all 79 folders of the Sales mailbox:
for several follow-up leads, Outlook's conversation held exactly one item.

The earlier messages travel inside the reply as quoted text, and
clean_email_body() strips them for display -- so a lead flagged "Follow-Up
Email" showed no thread at all. One had 1,481 characters stored and 219 shown.
"""

import tempfile
import unittest
from pathlib import Path

import db
import email_cleaner
from dashboard.app import _decorate, _quoted_history

REPLY_WITH_HISTORY = """Hi - can someone please assist? thanks.

Alejandro Rodriguez
Supply Planner

From: Alejandro Rodriguez <Alejandro.Rodriguez@ametek.com>
Sent: Wednesday, July 22, 2026 10:45
To: Sales <Sales@BalSeal.com>
Subject: TSE - KAMAN HT17-149

Good morning, could you please confirm the lead time on HT17-149?
Thank you,
Alejandro
"""


class ExtractQuotedHistoryTests(unittest.TestCase):
    def test_the_quoted_exchange_is_returned(self):
        quoted = email_cleaner.extract_quoted_history(REPLY_WITH_HISTORY)

        self.assertIn('TSE - KAMAN HT17-149', quoted)
        self.assertIn('confirm the lead time', quoted)
        self.assertIn('Sent: Wednesday, July 22, 2026', quoted)

    def test_the_new_message_is_not_repeated_in_it(self):
        # The reply itself is already on screen above; this is only the history.
        quoted = email_cleaner.extract_quoted_history(REPLY_WITH_HISTORY)

        self.assertNotIn('can someone please assist', quoted)

    def test_it_is_exactly_what_the_display_cleaner_removes(self):
        cleaned = email_cleaner.clean_email_body(REPLY_WITH_HISTORY)
        quoted = email_cleaner.extract_quoted_history(REPLY_WITH_HISTORY)

        self.assertIn('can someone please assist', cleaned)
        self.assertNotIn('confirm the lead time', cleaned)
        self.assertIn('confirm the lead time', quoted)

    def test_a_first_contact_email_quotes_nothing(self):
        body = 'Hello, we need a rotary seal for a 2.5in bore.\n\nThanks,\nAda\n'

        self.assertEqual(email_cleaner.extract_quoted_history(body), '')

    def test_other_quote_styles_are_recognised(self):
        for marker in ('On Tuesday, Ada wrote:\n', '-----Original Message-----\n'):
            body = f'Thanks!\n\n{marker}\nthe earlier message text\n'
            quoted = email_cleaner.extract_quoted_history(body)
            self.assertIn('earlier message text', quoted, marker)

    def test_tracking_urls_are_stripped_but_the_exchange_is_not(self):
        body = (
            'Thanks!\n\n'
            'From: Ada <ada@example.com>\n'
            'Sent: Monday\n'
            'Subject: Quote\n\n'
            'https://nam12.safelinks.protection.outlook.com/?url=' + 'x' * 120 + '\n'
            'Please quote 500 units.\n'
        )

        quoted = email_cleaner.extract_quoted_history(body)

        self.assertIn('Please quote 500 units.', quoted)
        self.assertNotIn('safelinks', quoted)

    def test_empty_input_is_safe(self):
        self.assertEqual(email_cleaner.extract_quoted_history(''), '')
        self.assertEqual(email_cleaner.extract_quoted_history(None), '')


class LeadPageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()

        # Cached bodies carry a metadata header above a divider.
        self.cache = Path(self.tempdir.name) / 'body.txt'
        self.cache.write_text(
            'Subject: Re: TSE\n' + ('=' * 60) + '\n\n' + REPLY_WITH_HISTORY,
            encoding='utf-8',
        )

    def _lead_with_body(self, body_path):
        message_id = db.insert_message(
            'entry-1', subject='Re: TSE - KAMAN HT17-149',
            sender_email='alejandro.rodriguez@ametek.com',
            body_cache_path=str(body_path) if body_path else None,
            conversation_id='conv-1',
        )
        lead_id = db.insert_lead(
            {'first_name': 'Alejandro', 'email': 'alejandro.rodriguez@ametek.com'},
            'email_ai_extract', message_id=message_id, status='pending_review',
        )
        return db.get_lead(lead_id)

    def test_a_lonely_reply_still_shows_its_thread(self):
        lead = _decorate(self._lead_with_body(self.cache))

        # One ingested message -- the old condition (>1) hid the thread entirely.
        self.assertEqual(len(lead['email_thread']), 1)
        self.assertIsNotNone(lead['quoted_history'])
        self.assertIn('confirm the lead time', lead['quoted_history'])

    def test_a_first_contact_lead_has_none(self):
        plain = Path(self.tempdir.name) / 'plain.txt'
        plain.write_text(
            ('=' * 60) + '\n\nHello, we need a seal.\n', encoding='utf-8')

        lead = _decorate(self._lead_with_body(plain))

        self.assertIsNone(lead['quoted_history'])

    def test_a_missing_cache_file_is_not_an_error(self):
        lead = _decorate(self._lead_with_body(Path(self.tempdir.name) / 'gone.txt'))

        self.assertIsNone(lead['quoted_history'])

    def test_no_message_at_all_is_not_an_error(self):
        self.assertIsNone(_quoted_history(None))
        self.assertIsNone(_quoted_history({}))


if __name__ == '__main__':
    unittest.main()
