"""
Every lead list is ordered like the Outlook inbox: newest email at the top.

It used to be ordered by fraud risk, which bore no relation to the mailbox.
The date it sorts on is the lead's own email, not the first message of its
thread -- since earlier messages are kept as context, a reply to an old thread
would otherwise date itself to that thread's start and sink to the bottom.

History was the odd one out for longer: it is paginated, so it takes its order
from SQL rather than the Python sort the other two pages use, and that SQL
ordered by created_at -- ingest time, not email time. HistoryOrderTests below
covers it, including the pagination case a Python-side sort cannot fix.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
from dashboard.app import _decorate, _newest_first, app


class ReviewOrderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()

    def _lead(self, name, received_at, fraud_risk=None, conversation_id=None):
        message_id = db.insert_message(
            f'entry-{name}', subject=f'Enquiry from {name}',
            sender_email=f'{name}@example.com', received_at=received_at,
            conversation_id=conversation_id,
        )
        lead_id = db.insert_lead(
            {'first_name': name, 'email': f'{name}@example.com'},
            'email_ai_extract', message_id=message_id, status='pending_review',
        )
        if fraud_risk:
            db.set_fraud_screen_result(lead_id, fraud_risk, [], None)
        return lead_id

    def _order_on_page(self):
        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            page = self.client.get('/').get_data(as_text=True)
        # Cards carry data-lead-id in render order.
        import re
        return [int(i) for i in re.findall(r'data-lead-id="(\d+)"', page)]

    def test_newest_email_is_first(self):
        oldest = self._lead('oldest', '2026-08-01T09:00:00+00:00')
        newest = self._lead('newest', '2026-08-10T17:30:00+00:00')
        middle = self._lead('middle', '2026-08-05T12:00:00+00:00')

        self.assertEqual(self._order_on_page(), [newest, middle, oldest])

    def test_fraud_risk_no_longer_moves_a_lead_up(self):
        # This ordering used to put 'reject' first regardless of date.
        risky_old = self._lead('risky', '2026-08-01T09:00:00+00:00', fraud_risk='reject')
        clean_new = self._lead('clean', '2026-08-10T09:00:00+00:00', fraud_risk='clear')

        self.assertEqual(self._order_on_page(), [clean_new, risky_old])

    def test_a_reply_is_dated_by_its_own_arrival_not_the_threads_start(self):
        # The thread's first message is old context; the reply arrived today.
        db.record_thread_message(
            'ctx-1', conversation_id='conv-1', received_at='2026-07-01T08:00:00+00:00',
            sender_email='buyer@example.com', subject='Original', body_text='...',
        )
        reply = self._lead('reply', '2026-08-10T16:00:00+00:00', conversation_id='conv-1')
        other = self._lead('other', '2026-08-09T09:00:00+00:00')

        decorated = _decorate(db.get_lead(reply))
        self.assertIn('Aug 10, 2026', decorated['received_date_formatted'])
        self.assertEqual(self._order_on_page(), [reply, other])

    def test_an_email_with_no_received_time_falls_back_to_when_it_was_ingested(self):
        # created_at is NOT NULL, so there is always something to sort on --
        # ingest time is the right stand-in, since it tracks mailbox order.
        old_email = self._lead('dated', '2026-08-01T09:00:00+00:00')
        no_time = self._lead('undated', None)

        decorated = _decorate(db.get_lead(no_time))
        self.assertIsNotNone(decorated['received_at_raw'])
        # Ingested just now, so it is the newer of the two.
        self.assertEqual(self._order_on_page(), [no_time, old_email])

    def test_mixed_timezone_and_naive_timestamps_do_not_crash(self):
        # Outlook supplies an offset; the created_at fallback may not. Python
        # refuses to compare the two, which would break the page.
        aware = self._lead('aware', '2026-08-10T09:00:00+00:00')
        naive = self._lead('naive', '2026-08-11T09:00:00')

        self.assertEqual(self._order_on_page(), [naive, aware])

    def test_the_queue_uses_the_same_order(self):
        older = self._lead('older', '2026-08-01T09:00:00+00:00')
        newer = self._lead('newer', '2026-08-09T09:00:00+00:00')
        for lead_id in (older, newer):
            db.update_lead_fields(lead_id, {'status': 'review_approved'})

        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            page = self.client.get('/queue').get_data(as_text=True)

        self.assertLess(page.index('newer@example.com'), page.index('older@example.com'))


class HistoryOrderTests(unittest.TestCase):
    """History is paginated, so its order comes from SQL, not the Python sort."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()

    def _reviewed_lead(self, name, received_at):
        """A lead in a History status, whose email arrived at received_at."""
        message_id = db.insert_message(
            f'entry-{name}', subject=f'Enquiry from {name}',
            sender_email=f'{name}@example.com', received_at=received_at,
        )
        return db.insert_lead(
            {'first_name': name, 'email': f'{name}@example.com'},
            'email_ai_extract', message_id=message_id, status='submitted',
        )

    def test_history_is_ordered_by_email_date_not_ingest_time(self):
        # Inserted oldest-email-first, so ingest order is the reverse of the
        # order we want. created_at ordering would hand these back as-inserted.
        old = self._reviewed_lead('old', '2026-08-01T09:00:00+00:00')
        new = self._reviewed_lead('new', '2026-08-10T09:00:00+00:00')
        mid = self._reviewed_lead('mid', '2026-08-05T09:00:00+00:00')

        ordered = [l['id'] for l in db.list_leads(status='submitted')]

        self.assertEqual(ordered, [new, mid, old])

    def test_two_emails_in_the_same_second_keep_their_real_order(self):
        # The exact case seen in production: one ingest run, two emails a
        # fraction of a second apart. Ordering by ingest time put the older
        # of the two on top.
        earlier = self._reviewed_lead('earlier', '2026-08-13T09:54:17.305000+00:00')
        later = self._reviewed_lead('later', '2026-08-13T09:54:17.529000+00:00')

        ordered = [l['id'] for l in db.list_leads(status='submitted')]

        self.assertEqual(ordered, [later, earlier])

    def test_order_holds_across_a_page_boundary(self):
        # A Python sort applied after LIMIT/OFFSET would order each page
        # internally and still put the wrong leads on page 1.
        ids_newest_first = [
            self._reviewed_lead(f'lead{day:02d}', f'2026-08-{day:02d}T09:00:00+00:00')
            for day in range(1, 8)
        ][::-1]

        page1 = [l['id'] for l in db.list_leads(status='submitted', limit=3, offset=0)]
        page2 = [l['id'] for l in db.list_leads(status='submitted', limit=3, offset=3)]

        self.assertEqual(page1, ids_newest_first[:3])
        self.assertEqual(page2, ids_newest_first[3:6])

    def test_a_lead_with_no_email_falls_back_to_ingest_time(self):
        # Spreadsheet rows have no message; they must still sort sensibly
        # rather than sinking below everything or crashing the join.
        emailed = self._reviewed_lead('emailed', '2026-08-01T09:00:00+00:00')
        spreadsheet = db.insert_lead(
            {'first_name': 'sheet', 'email': 'sheet@example.com'},
            'excel', status='submitted',
        )

        ordered = [l['id'] for l in db.list_leads(status='submitted')]

        # Ingested just now, so newer than the August 1st email.
        self.assertEqual(ordered, [spreadsheet, emailed])

    def test_the_history_page_renders_in_that_order(self):
        self._reviewed_lead('older', '2026-08-01T09:00:00+00:00')
        self._reviewed_lead('newer', '2026-08-10T09:00:00+00:00')

        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            page = self.client.get('/history').get_data(as_text=True)

        self.assertLess(page.index('newer@example.com'), page.index('older@example.com'))


class SortKeyTests(unittest.TestCase):
    def test_undateable_values_sort_last(self):
        floor = _newest_first({'received_at_raw': None})

        self.assertLess(floor, _newest_first({'received_at_raw': '2026-01-01T00:00:00'}))
        self.assertEqual(floor, _newest_first({'received_at_raw': 'not a date'}))

    def test_a_trailing_z_is_understood(self):
        self.assertEqual(
            _newest_first({'received_at_raw': '2026-08-10T09:00:00Z'}),
            _newest_first({'received_at_raw': '2026-08-10T09:00:00+00:00'}),
        )


if __name__ == '__main__':
    unittest.main()
