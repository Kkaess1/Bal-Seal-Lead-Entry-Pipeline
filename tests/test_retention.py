"""
Deleting old leads, and keeping their numbers.

The pipeline kept every lead it had ever seen, and each one anchors a cached
email body and any files the customer attached -- real personal data
accumulating on one machine forever. retention.py deletes finished leads once
they are old enough.

The hard part is not the delete. It is that every Oracle reporting table is
keyed to leads(id) with a foreign key and foreign keys are ON, so a purged lead
takes its qualification, its opportunities and its revenue with it. Without the
rollup these tests pin, the status page's totals, conversion rates and funnel
would all step down the first morning the retention job ran, and a rate over a
shrinking denominator still looks like a real number.

The other half is what must NOT be deleted: a lead still awaiting review, a
lead that has not aged out yet, and the cached email of a sibling lead that
came from the same trade-show spreadsheet.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import db
import retention
from dashboard.app import app


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def days_ago(count):
    return (NOW - timedelta(days=count)).isoformat()


class RetentionTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()

    def _lead(self, name='lead', status='submitted', finished=None,
              message_id=None, **fields):
        """A lead in `status` that finished `finished` days ago."""
        lead_id = db.insert_lead(
            {'first_name': name, 'email': f'{name}@example.com'},
            'email_ai_extract', message_id=message_id, status='pending_review',
        )
        stamp = days_ago(finished) if finished is not None else days_ago(0)
        # submitted_at for a submitted lead, reviewed_at for a rejected one --
        # the two stamps db.FINISHED_AT_SQL reads, in that order.
        finish_field = 'submitted_at' if status == 'submitted' else 'reviewed_at'
        db.update_lead_fields(
            lead_id, {'status': status, finish_field: stamp, **fields})
        return lead_id

    def _purge(self, apply=True, **kwargs):
        return retention.purge(now=NOW, apply=apply, **kwargs)

    def _count(self, table):
        conn = db.get_connection()
        try:
            return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        finally:
            conn.close()


class WhatGetsDeletedTests(RetentionTestCase):
    def test_a_rejected_lead_is_deleted_after_a_month(self):
        old = self._lead('old', status='rejected', finished=31)

        self._purge()

        self.assertIsNone(db.get_lead(old))

    def test_a_rejected_lead_from_last_week_is_kept(self):
        # The window exists so a rejection can be noticed and undone with
        # "Return to Review" before the lead is gone for good.
        recent = self._lead('recent', status='rejected', finished=7)

        self._purge()

        self.assertIsNotNone(db.get_lead(recent))

    def test_a_submitted_lead_is_kept_for_a_year_then_deleted(self):
        # A submitted lead is not junk -- its record is in Oracle and the local
        # copy is worth having around. It gets the full year, not the month.
        eleven_months = self._lead('recent', status='submitted', finished=330)
        thirteen_months = self._lead('ancient', status='submitted', finished=400)

        self._purge()

        self.assertIsNotNone(db.get_lead(eleven_months))
        self.assertIsNone(db.get_lead(thirteen_months))

    def test_duplicate_and_unknown_leads_get_the_year_not_the_month(self):
        # Only 'rejected' takes the short window. A duplicate is a real inquiry
        # that matched an existing Oracle lead, and an unknown submission may
        # have reached Oracle -- neither is junk to be cleared out in a month.
        duplicate = self._lead('dupe', status='duplicate', finished=60)
        unknown = self._lead('unknown', status='submission_unknown', finished=60)

        self._purge()

        self.assertIsNotNone(db.get_lead(duplicate))
        self.assertIsNotNone(db.get_lead(unknown))

    def test_a_lead_still_awaiting_review_is_never_deleted(self):
        """However old it is. lead_stages.py exists to guarantee every lead has
        a way onward and a way back; deleting unfinished work is the most
        complete dead end there is, and age is not a decision."""
        forgotten = db.insert_lead(
            {'first_name': 'forgotten'}, 'email_ai_extract', status='pending_review')
        db.update_lead_fields(forgotten, {'created_at': days_ago(900)})

        self._purge()

        self.assertIsNotNone(db.get_lead(forgotten))

    def test_a_lead_stuck_in_the_input_queue_is_never_deleted(self):
        # 'approved' and 'failed' sit in the Input Queue waiting for a human.
        # Purging one would delete a lead that was on its way to Oracle.
        stuck = db.insert_lead(
            {'first_name': 'stuck'}, 'email_ai_extract', status='approved')
        db.update_lead_fields(stuck, {'created_at': days_ago(900)})

        self._purge()

        self.assertIsNotNone(db.get_lead(stuck))

    def test_a_dry_run_deletes_nothing(self):
        old = self._lead('old', status='rejected', finished=90)

        result = self._purge(apply=False)

        self.assertEqual(result['due'], 1)
        self.assertEqual(result['leads'], 0)
        self.assertIsNotNone(db.get_lead(old))

    def test_age_is_measured_from_the_decision_not_the_email(self):
        """A year-old email rejected yesterday is one day old, not a year.

        Ageing on the email date would delete leads the operator had only just
        dealt with -- and the Sales inbox regularly turns up old threads.
        """
        message_id = db.insert_message(
            'entry-old-email', subject='Re: an old thread',
            sender_email='buyer@example.com', received_at=days_ago(500),
        )
        just_rejected = self._lead(
            'just-rejected', status='rejected', finished=1, message_id=message_id)

        self._purge()

        self.assertIsNotNone(db.get_lead(just_rejected))


class WhatGoesWithALeadTests(RetentionTestCase):
    def test_the_source_email_and_its_cached_body_go_too(self):
        cache = Path(self.tempdir.name) / 'body.txt'
        cache.write_text('the customer wrote this', encoding='utf-8')
        message_id = db.insert_message(
            'entry-1', subject='Enquiry', sender_email='buyer@example.com',
            received_at=days_ago(400), body_cache_path=str(cache),
        )
        self._lead('gone', status='rejected', finished=90, message_id=message_id)

        with patch.object(retention, '_is_deletable', return_value=True):
            result = self._purge()

        self.assertIsNone(db.get_message(message_id))
        self.assertFalse(cache.exists())
        self.assertEqual(result['messages'], 1)

    def test_a_spreadsheet_email_survives_while_one_of_its_leads_does(self):
        """One trade-show spreadsheet becomes many leads off a single email.

        Deleting the message with the first purged lead would empty the email
        thread of every sibling still in review -- the reviewer would open a
        live lead and find its source gone.
        """
        cache = Path(self.tempdir.name) / 'batch.txt'
        cache.write_text('spreadsheet attached', encoding='utf-8')
        message_id = db.insert_message(
            'entry-batch', subject='Trade show list', received_at=days_ago(400),
            body_cache_path=str(cache),
        )
        self._lead('old-row', status='rejected', finished=90, message_id=message_id)
        survivor = self._lead('new-row', status='rejected', finished=1,
                              message_id=message_id)

        with patch.object(retention, '_is_deletable', return_value=True):
            self._purge()

        self.assertIsNotNone(db.get_lead(survivor))
        self.assertIsNotNone(db.get_message(message_id))
        self.assertTrue(cache.exists())

    def test_replies_and_oracle_records_are_deleted_with_their_lead(self):
        # Foreign keys are ON, so a child row left behind does not just leak --
        # it makes the lead undeletable and the whole purge fail.
        lead_id = self._lead('sent', status='submitted', finished=400)
        db.record_lead_reply(lead_id, body='Thanks for getting in touch')
        db.save_oracle_snapshot(lead_id, {
            'oracle_lead_id': '42', 'oracle_status': 'QUALIFIED',
            'qualified_at': days_ago(390),
        }, [{'opportunity_number': 'OPP-1', 'status': 'OPEN'}])

        self._purge()

        # oracle_lifecycle_events too: save_oracle_snapshot() writes one, and a
        # single surviving child row is enough to make the lead undeletable.
        for table in ('lead_replies', 'oracle_lead_snapshots',
                      'oracle_opportunities', 'oracle_lifecycle_events'):
            self.assertEqual(self._count(table), 0, table)

    def test_a_surviving_lead_marked_duplicate_of_a_purged_one_is_kept(self):
        """Its duplicate_of pointer is cleared rather than the lead being
        dragged down with the one it points at -- a foreign key would otherwise
        refuse the delete, and purging the survivor would delete a lead that
        was not due."""
        original = self._lead('original', status='submitted', finished=400)
        survivor = self._lead('copy', status='duplicate', finished=1,
                              duplicate_of_lead_id=original)

        self._purge()

        remaining = db.get_lead(survivor)
        self.assertIsNotNone(remaining)
        self.assertIsNone(remaining['duplicate_of_lead_id'])

    def test_a_file_outside_the_cache_directories_is_never_deleted(self):
        """The paths come out of the database, and one bad value should not
        turn the retention job into a delete-anything tool."""
        outsider = Path(self.tempdir.name) / 'not-ours.txt'
        outsider.write_text('important', encoding='utf-8')

        deleted = retention.delete_files([str(outsider)])

        self.assertEqual(deleted, 0)
        self.assertTrue(outsider.exists())


class StatsSurviveTests(RetentionTestCase):
    """The status page must not lose count of a lead just because it was deleted."""

    def test_lifetime_counts_include_deleted_leads(self):
        self._lead('a', status='rejected', finished=90)
        self._lead('b', status='rejected', finished=90)
        self._lead('c', status='rejected', finished=1)

        before = db.lifetime_lead_counts()
        self._purge()
        after = db.lifetime_lead_counts()

        self.assertEqual(before['rejected'], 3)
        self.assertEqual(after['rejected'], 3)
        # ...while the live count, which History paginates on, really does drop.
        self.assertEqual(db.count_leads(status='rejected'), 1)

    def test_two_purge_runs_add_up_rather_than_overwrite(self):
        self._lead('first', status='rejected', finished=90)
        self._purge()
        self._lead('second', status='rejected', finished=90)
        self._purge()

        self.assertEqual(db.lifetime_lead_counts()['rejected'], 2)

    def test_oracle_qualification_and_conversion_survive_the_purge(self):
        lead_id = self._lead('sent', status='submitted', finished=400)
        db.save_oracle_snapshot(lead_id, {
            'oracle_lead_id': '42', 'oracle_lead_number': 'L-42',
            'oracle_status': 'QUALIFIED', 'qualified_at': days_ago(390),
        }, [{
            'opportunity_number': 'OPP-1', 'name': 'Seal order',
            'status': 'WON', 'revenue_amount': 5000.0, 'currency_code': 'USD',
        }])

        before = db.lifetime_oracle_summary()
        self._purge()
        after = db.lifetime_oracle_summary()

        for key in ('synced', 'qualified', 'converted', 'won'):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(after['qualified'], 1)
        self.assertEqual([dict(row) for row in after['currencies']],
                         [dict(row) for row in before['currencies']])

    def test_revenue_from_a_deleted_lead_still_counts(self):
        lead_id = self._lead('sent', status='submitted', finished=400)
        db.save_oracle_snapshot(lead_id, {'oracle_lead_id': '42'}, [{
            'opportunity_number': 'OPP-1', 'status': 'WON',
            'revenue_amount': 5000.0, 'currency_code': 'USD',
        }])

        self._purge()
        currencies = db.lifetime_oracle_summary()['currencies']

        self.assertEqual(len(currencies), 1)
        self.assertEqual(currencies[0]['currency'], 'USD')
        self.assertEqual(currencies[0]['pipeline_value'], 5000.0)
        self.assertEqual(currencies[0]['won_value'], 5000.0)

    def test_live_and_purged_revenue_in_one_currency_are_added_together(self):
        # A currency can have money on both sides of a purge; the merge has to
        # sum them rather than one row winning.
        for finished, amount in ((400, 1000.0), (10, 250.0)):
            lead_id = self._lead(f'lead{finished}', status='submitted',
                                 finished=finished)
            db.save_oracle_snapshot(lead_id, {'oracle_lead_id': str(lead_id)}, [{
                'opportunity_number': f'OPP-{finished}', 'status': 'OPEN',
                'revenue_amount': amount, 'currency_code': 'EUR',
            }])

        self._purge()
        currencies = db.lifetime_oracle_summary()['currencies']

        self.assertEqual(len(currencies), 1)
        self.assertEqual(currencies[0]['pipeline_value'], 1250.0)

    def test_the_rollup_and_the_live_summary_agree_on_what_converted_means(self):
        """A lead with an opportunity but no converted_at counts as converted
        live, and must count the same once it is purged -- oracle_dashboard_summary()
        and the purge rollup define 'converted' separately, and a drift between
        them would change the funnel for old leads only."""
        lead_id = self._lead('sent', status='submitted', finished=400)
        db.save_oracle_snapshot(lead_id, {'oracle_lead_id': '42'},
                                [{'opportunity_number': 'OPP-1', 'status': 'OPEN'}])

        before = db.lifetime_oracle_summary()['converted']
        self._purge()

        self.assertEqual(before, 1)
        self.assertEqual(db.lifetime_oracle_summary()['converted'], 1)

    def test_message_counts_include_messages_deleted_with_their_leads(self):
        message_id = db.insert_message(
            'entry-1', subject='Enquiry', received_at=days_ago(400))
        db.update_message_status(message_id, 'done')
        self._lead('gone', status='rejected', finished=90, message_id=message_id)

        before = db.lifetime_message_counts()
        self._purge()

        self.assertEqual(db.lifetime_message_counts(), before)


class StatusPageTests(RetentionTestCase):
    """The page the numbers are for."""

    def setUp(self):
        super().setUp()
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()

    def _page(self):
        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            return self.client.get('/status').get_data(as_text=True)

    def test_the_total_does_not_drop_when_leads_are_purged(self):
        for index in range(3):
            self._lead(f'lead{index}', status='rejected', finished=90)

        before = self._page()
        self._purge()
        after = self._page()

        # "All leads" is the first KPI value on the page.
        self.assertIn('>3</div>', before)
        self.assertIn('>3</div>', after)

    def test_the_conversion_rate_is_not_inflated_by_a_purge(self):
        """The denominator is submitted leads. If purging dropped it while the
        Oracle numerator survived in the rollup, every rate on the page would
        climb the morning the job ran."""
        for index in range(4):
            self._lead(f'sent{index}', status='submitted', finished=400)
        qualified = self._lead('qualified', status='submitted', finished=400)
        db.save_oracle_snapshot(
            qualified, {'oracle_lead_id': '42', 'oracle_status': 'QUALIFIED'}, [])

        before = self._page()
        self._purge()

        self.assertIn('20.0%', before)
        self.assertIn('20.0%', self._page())

    def test_the_page_says_how_many_leads_it_is_counting_that_no_longer_exist(self):
        # Otherwise the gap between the funnel and the searchable list below it
        # reads as a bug in the search.
        self._lead('gone', status='rejected', finished=90)
        self._purge()

        self.assertIn('1 lead has been deleted so far', self._page())

    def test_the_retention_policy_is_stated_on_the_page(self):
        self.assertIn('Rejected leads are deleted 30 days', self._page())


if __name__ == '__main__':
    unittest.main()
