"""
Approval is the only thing that sends a lead to Oracle.

Nothing uploads on a timer: the scheduled OSC submission task was removed on
2026-08-07. These tests pin the rule down at both ends -- every path that
reaches Oracle starts from a human action, and a lead that has not been
approved is never in a submit run's list.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
from dashboard.app import app


class SubmissionTriggerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'

    def _lead(self, status):
        # Complete enough to pass submission_blockers(): these tests are about
        # what starts a submit run, and an incomplete lead is now held back at
        # approval before any of that is reached.
        return db.insert_lead(
            {
                'first_name': 'Ada', 'email': 'ada@example.com',
                'country': 'United States', 'state': 'MD',
                'assigned_tm': 'Mark Copeland',
            },
            'email_ai_extract', status=status,
        )

    @patch('subprocess.Popen')
    def test_approving_a_batch_sends_it_to_oracle(self, popen):
        lead_id = self._lead('review_approved')

        with patch('dashboard.app.review_marking.maybe_mark_email_reviewed'):
            response = self.client.post(
                '/queue/approve_all', data={'_csrf_token': 'token'},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_lead(lead_id)['status'], 'approved')
        popen.assert_called_once()
        self.assertIn('pipeline_submit.py', popen.call_args.args[0][1])

    @patch('subprocess.Popen')
    def test_send_now_covers_leads_left_over_from_a_failed_attempt(self, popen):
        # A connection error puts a lead back to 'approved' with a retry time.
        # With no scheduled run, this button is what picks it up.
        lead_id = self._lead('approved')
        db.schedule_lead_retry(lead_id, 'Network error', delay_minutes=0)

        response = self.client.post('/queue/submit_now', data={'_csrf_token': 'token'})

        popen.assert_called_once()
        self.assertIn(b'Sending 1 approved lead', self.client.get(response.location).data)

    @patch('subprocess.Popen')
    def test_send_now_does_nothing_when_nothing_is_approved(self, popen):
        self._lead('pending_review')

        response = self.client.post('/queue/submit_now', data={'_csrf_token': 'token'})

        popen.assert_not_called()
        self.assertIn(b'No approved leads are waiting', self.client.get(response.location).data)

    @patch('subprocess.Popen')
    def test_authorizing_a_retry_starts_the_run_itself(self, popen):
        lead_id = self._lead('submission_unknown')

        self.client.post(
            f'/leads/{lead_id}/unknown-retry', data={'_csrf_token': 'token'},
        )

        self.assertEqual(db.get_lead(lead_id)['status'], 'approved')
        popen.assert_called_once()

    @patch('subprocess.Popen')
    def test_sending_requires_the_csrf_token(self, popen):
        self._lead('approved')

        self.assertEqual(self.client.post('/queue/submit_now').status_code, 400)
        self.assertEqual(self.client.post('/queue/approve_all').status_code, 400)
        popen.assert_not_called()

    def test_only_approved_leads_are_ever_in_a_submit_run(self):
        for status in ('ingested', 'extracted', 'screened', 'pending_review',
                       'review_approved', 'rejected', 'failed', 'duplicate'):
            self._lead(status)
        approved_id = self._lead('approved')

        ready = db.list_ready_for_submission()

        self.assertEqual([lead['id'] for lead in ready], [approved_id])

    def test_a_lead_still_awaiting_its_retry_time_is_not_sent(self):
        lead_id = self._lead('approved')
        db.schedule_lead_retry(lead_id, 'Network error', delay_minutes=15)

        self.assertEqual(db.list_ready_for_submission(), [])


class SchedulerSetupTests(unittest.TestCase):
    """Nothing in this pipeline runs on a timer. Emails are ingested when the
    operator clicks the button; leads reach Oracle when they are approved."""

    def setUp(self):
        self.script = (
            Path(__file__).resolve().parent.parent / 'setup_scheduler.bat'
        ).read_text(encoding='utf-8', errors='replace')

    def test_no_task_runs_the_submitter_or_the_ingester(self):
        # Both files are named only in comments explaining why no such task
        # exists; neither appears in a task action.
        self.assertNotIn('%SCRIPT_DIR%pipeline_submit.py', self.script)
        self.assertNotIn('%SCRIPT_DIR%pipeline_ingest.py', self.script)

    def test_no_daily_schedule_is_registered_at_all(self):
        self.assertNotIn('/sc daily', self.script)

    def test_retired_timers_are_cleaned_up(self):
        for task in ('LeadPipeline_Ingest_AM', 'LeadPipeline_Ingest_PM',
                     'LeadPipeline_Submit_OSC'):
            self.assertIn(task, self.script)
        self.assertIn('schtasks /delete /tn "%%T" /f', self.script)


if __name__ == '__main__':
    unittest.main()
