"""
A lead still being worked on is listed like the Outlook inbox: newest email at
the top. A lead that is finished is listed by when it finished.

Pending Review and the Input Queue used to order by fraud risk, which bore no
relation to the mailbox. The date they sort on is the lead's own email, not
the first message of its thread -- since earlier messages are kept as context,
a reply to an old thread would otherwise date itself to that thread's start
and sink to the bottom.

History took that same inbox order for a while, and it was the wrong question
to ask of a finished lead. One approved batch carries emails from a spread of
dates, so the leads submitted in it scattered themselves through months of
older ones and the page read as unordered -- the lead just sent to Oracle was
rarely anywhere near the top. HistoryOrderTests below covers the order it uses
instead, including the pagination case a Python-side sort cannot fix.
"""

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import lead_stages
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

    def _lead(self, name, received_at, **finished):
        """A finished lead: its email arrived at received_at, and `finished`
        says when and how it left the queue."""
        message_id = db.insert_message(
            f'entry-{name}', subject=f'Enquiry from {name}',
            sender_email=f'{name}@example.com', received_at=received_at,
        )
        lead_id = db.insert_lead(
            {'first_name': name, 'email': f'{name}@example.com'},
            'email_ai_extract', message_id=message_id, status='pending_review',
        )
        db.update_lead_fields(lead_id, dict(finished))
        return lead_id

    def _submitted(self, name, received_at, submitted_at):
        return self._lead(name, received_at,
                          status='submitted', submitted_at=submitted_at)

    def _history_order(self):
        return [l['id'] for l in db.list_leads(
            status=list(lead_stages.HISTORY), order='finished')]

    def test_the_most_recently_submitted_lead_is_first(self):
        # The whole request: a lead sent to Oracle just now goes to the top,
        # and is pushed down by the next one.
        first = self._submitted('first', '2026-08-01T09:00:00+00:00',
                                '2026-08-20T10:00:00+00:00')
        second = self._submitted('second', '2026-08-02T09:00:00+00:00',
                                 '2026-08-21T10:00:00+00:00')
        third = self._submitted('third', '2026-08-03T09:00:00+00:00',
                                '2026-08-22T10:00:00+00:00')

        self.assertEqual(self._history_order(), [third, second, first])

    def test_an_old_email_submitted_today_outranks_a_new_email_submitted_last_week(self):
        # Exactly what made the page look disorganized: a batch approved today
        # is full of emails from a spread of dates, so email order buried
        # today's submissions among older leads.
        old_email_sent_today = self._submitted(
            'stale', '2026-06-01T09:00:00+00:00', '2026-08-28T09:00:00+00:00')
        new_email_sent_last_week = self._submitted(
            'fresh', '2026-08-20T09:00:00+00:00', '2026-08-21T09:00:00+00:00')

        self.assertEqual(self._history_order(),
                         [old_email_sent_today, new_email_sent_last_week])

    def test_a_rejected_lead_is_placed_by_when_it_was_rejected(self):
        # Nothing was ever sent, so submitted_at is NULL. reviewed_at is the
        # moment that lead finished, and it has to sort against submitted_at
        # rather than falling to the bottom of the page.
        submitted = self._submitted('sent', '2026-08-01T09:00:00+00:00',
                                    '2026-08-25T09:00:00+00:00')
        rejected = self._lead('binned', '2026-08-02T09:00:00+00:00',
                              status='rejected',
                              reviewed_at='2026-08-26T09:00:00+00:00')

        self.assertEqual(self._history_order(), [rejected, submitted])

    def test_two_leads_submitted_by_the_same_run_keep_a_fixed_order(self):
        # One submit run stamps every lead in the batch with the same second.
        # Without the id tiebreak their order is the query planner's choice and
        # can change between page loads.
        earlier = self._submitted('earlier', '2026-08-01T09:00:00+00:00',
                                  '2026-08-25T09:54:17+00:00')
        later = self._submitted('later', '2026-08-02T09:00:00+00:00',
                                '2026-08-25T09:54:17+00:00')

        self.assertEqual(self._history_order(), [later, earlier])

    def test_dismissing_an_alert_does_not_move_a_finished_lead(self):
        # updated_at is only the last-resort key for a reason: it is touched
        # long after a lead is done, and sorting on it would jump a months-old
        # lead to the top the moment someone cleared its bell.
        newest = self._submitted('newest', '2026-08-03T09:00:00+00:00',
                                 '2026-08-27T09:00:00+00:00')
        older = self._submitted('older', '2026-08-01T09:00:00+00:00',
                                '2026-08-20T09:00:00+00:00')

        db.dismiss_lead_alert(older)

        self.assertEqual(self._history_order(), [newest, older])

    def test_order_holds_across_a_page_boundary(self):
        # A Python sort applied after LIMIT/OFFSET would order each page
        # internally and still put the wrong leads on page 1.
        ids_newest_first = [
            self._submitted(f'lead{day:02d}', '2026-08-01T09:00:00+00:00',
                            f'2026-08-{day:02d}T09:00:00+00:00')
            for day in range(1, 8)
        ][::-1]

        statuses = list(lead_stages.HISTORY)
        page1 = [l['id'] for l in db.list_leads(
            status=statuses, limit=3, offset=0, order='finished')]
        page2 = [l['id'] for l in db.list_leads(
            status=statuses, limit=3, offset=3, order='finished')]

        self.assertEqual(page1, ids_newest_first[:3])
        self.assertEqual(page2, ids_newest_first[3:6])

    def test_a_lead_with_no_finish_stamp_falls_back_to_when_it_was_parked(self):
        # 'duplicate' and 'submission_unknown' have neither submitted_at nor a
        # rejection stamp of their own; they must still sort somewhere sane
        # rather than sinking below everything or crashing the join.
        stamped = self._submitted('stamped', '2026-08-01T09:00:00+00:00',
                                  '2026-08-20T09:00:00+00:00')
        unstamped = self._lead('unstamped', '2026-08-02T09:00:00+00:00',
                               status='duplicate')

        # Parked just now, so newer than the August 20th submission.
        self.assertEqual(self._history_order(), [unstamped, stamped])

    def test_an_unknown_order_is_refused_rather_than_silently_ignored(self):
        with self.assertRaises(ValueError):
            db.list_leads(order='whatever')

    def test_the_history_page_renders_in_that_order(self):
        self._submitted('older', '2026-08-10T09:00:00+00:00',
                        '2026-08-20T09:00:00+00:00')
        self._submitted('newer', '2026-08-01T09:00:00+00:00',
                        '2026-08-26T09:00:00+00:00')

        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            page = self.client.get('/history').get_data(as_text=True)

        self.assertLess(page.index('newer@example.com'), page.index('older@example.com'))

    def test_the_column_the_page_shows_is_the_column_it_sorts_on(self):
        # A page sorted on a date it does not display reads as unordered even
        # when it is not: History showed reviewed_at, which is stamped when a
        # reviewer stages a lead, not when the batch it rides out with goes.
        self._submitted('older', '2026-08-01T09:00:00+00:00',
                        '2026-08-20T11:00:00+00:00')
        self._submitted('newer', '2026-08-02T09:00:00+00:00',
                        '2026-08-26T11:00:00+00:00')

        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            page = self.client.get('/history').get_data(as_text=True)

        shown = [
            value.strip()
            for value in re.findall(r'data-label="Finished">([^<]*)<', page)
        ]
        self.assertEqual(shown, ['2026-08-26 11:00', '2026-08-20 11:00'])


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
