"""The lead lifecycle: no status may leave a lead invisible or stranded.

The bug these pin down: 'ingested' and 'extracted' -- the statuses a lead holds
for the seconds between being written down and being screened -- appeared on no
page at all, so a lead whose ingest run died in that window still existed, still
had its email, and could not be seen or acted on from anywhere in the dashboard.
'duplicate' was visible but had no way back, which is the same problem wearing a
different hat.
"""

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import lead_stages
from dashboard.app import app

ROOT = Path(__file__).resolve().parent.parent


class StatusRegistryTests(unittest.TestCase):
    """Read the statuses back out of db.py, so the registry cannot drift."""

    @staticmethod
    def _statuses_written_by_db():
        source = (ROOT / 'db.py').read_text(encoding='utf-8')
        found = set()
        for line in source.splitlines():
            # The run-lease blob is a dict with its own 'status' key and has
            # nothing to do with leads; it is the one false positive here.
            if "'token'" in line:
                continue
            found.update(re.findall(r"'status': '(\w+)'", line))
        found.update(re.findall(r"UPDATE leads\s+SET status = '(\w+)'", source))
        found.update(re.findall(r"def insert_lead\([^)]*?status='(\w+)'",
                                source, re.S))
        return found

    def test_every_status_db_can_write_has_a_page(self):
        missing = self._statuses_written_by_db() - set(lead_stages.ALL)
        self.assertEqual(
            missing, set(),
            f'db.py can put a lead in {sorted(missing)}, which no page lists. '
            'Add it to lead_stages.py, on the page a reviewer should find it.',
        )

    def test_no_status_is_claimed_by_two_pages(self):
        # A lead in two places at once has two answers to "where is it?", which
        # is how an in-flight lead came to sit in both the queue and History.
        for status in lead_stages.ALL:
            pages = [name for name, statuses in (
                ('Pending Review', lead_stages.PENDING),
                ('Input Queue', lead_stages.QUEUE),
                ('History', lead_stages.HISTORY),
            ) if status in statuses]
            self.assertEqual(len(pages), 1, f'{status} is listed by {pages}')

    def test_a_status_nobody_defined_is_reported_as_homeless(self):
        self.assertIsNone(lead_stages.page_of('invented_status'))
        self.assertFalse(lead_stages.is_listed('invented_status'))


class EveryStatusIsOnExactlyOnePageTests(unittest.TestCase):
    """The end-to-end version: render the three pages and go looking."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        db.init_db()
        self.lead_ids = {}
        for status in lead_stages.ALL:
            self.lead_ids[status] = db.insert_lead(
                {'first_name': 'Ada', 'last_name': 'Lovelace',
                 'email': f'{status}@example.com',
                 'organization': f'MARKER-{status.upper()}'},
                'email_ai_extract', status=status,
            )
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        thread = patch('dashboard.app.db.get_lead_email_thread', return_value=[])
        thread.start()
        self.addCleanup(thread.stop)

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def _body(self, path):
        """What the page itself lists -- <main>, not the shared header.

        The notification bell lives outside <main> and deliberately follows a
        lead onto every page, so matching the whole document would say a
        flagged lead is listed three times over.
        """
        html = self.client.get(path).get_data(as_text=True)
        match = re.search(r'<main>(.*)</main>', html, re.S)
        return match.group(1) if match else html

    def _pages(self):
        return {
            'Pending Review': self._body('/'),
            'Input Queue': self._body('/queue'),
            'History': self._body('/history'),
        }

    def test_each_status_appears_on_its_page_and_only_there(self):
        pages = self._pages()
        for status in lead_stages.ALL:
            marker = f'MARKER-{status.upper()}'
            showing = [name for name, html in pages.items() if marker in html]
            self.assertEqual(
                showing, [lead_stages.page_of(status)],
                f'a lead in {status!r} shows on {showing or "no page at all"}',
            )

    def test_an_interrupted_ingest_still_reaches_a_reviewer(self):
        # The concrete failure: pipeline_ingest inserts the lead, then screens
        # it. A run that dies in between used to lose the lead entirely.
        page = self._body('/')
        for status in lead_stages.INGESTING:
            self.assertIn(f'MARKER-{status.upper()}', page)


class ReturnPathTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        db.init_db()
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def _lead(self, status, **fields):
        return db.insert_lead(
            {'first_name': 'Ada', 'email': 'ada@example.com', **fields},
            'email_ai_extract', status=status,
        )

    def test_a_wrongly_matched_duplicate_can_be_brought_back(self):
        lead_id = self._lead('duplicate')

        response = self.client.post(f'/leads/{lead_id}/restore',
                                    data={'_csrf_token': 'token'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_lead(lead_id)['status'], 'pending_review')

    def test_every_returnable_status_really_returns(self):
        for status in lead_stages.RETURNABLE:
            lead_id = self._lead(status)
            self.client.post(f'/leads/{lead_id}/restore',
                             data={'_csrf_token': 'token'})
            self.assertEqual(db.get_lead(lead_id)['status'], 'pending_review',
                             f'{status} did not return to Pending Review')

    def test_a_submitted_lead_is_not_returnable(self):
        lead_id = self._lead('submitted')

        response = self.client.post(f'/leads/{lead_id}/restore',
                                    data={'_csrf_token': 'token'})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(db.get_lead(lead_id)['status'], 'submitted')

    def test_a_lead_left_claimed_by_a_dead_run_can_be_released(self):
        lead_id = self._lead('submitting')

        response = self.client.post(f'/leads/{lead_id}/check-oracle',
                                    data={'_csrf_token': 'token'})

        self.assertEqual(response.status_code, 302)
        lead = db.get_lead(lead_id)
        # submission_unknown, not approved: the dead run may have reached
        # Oracle before it died, so a human checks before anything is resent.
        self.assertEqual(lead['status'], 'submission_unknown')
        self.assertIn('stopped without finishing', lead['submit_error'])

    def test_releasing_a_lead_that_is_not_sending_is_refused(self):
        lead_id = self._lead('approved')

        response = self.client.post(f'/leads/{lead_id}/check-oracle',
                                    data={'_csrf_token': 'token'})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(db.get_lead(lead_id)['status'], 'approved')


class MidIngestLeadsAreActionableTests(unittest.TestCase):
    """Being visible is not enough -- the buttons on the card have to work."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        db.init_db()
        self.lead_id = db.insert_lead(
            {'first_name': 'Ada', 'last_name': 'Lovelace',
             'email': 'ada@example.com', 'organization': 'Analytical Engines',
             'country': 'USA', 'state': 'OH', 'market': 'INDUSTRIAL'},
            'email_ai_extract', status='extracted',
        )
        db.set_territory_assignment(self.lead_id, 'Alicia Sabillon', 'US state: OH')
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def test_a_lead_left_mid_ingest_can_be_approved(self):
        response = self.client.post(f'/leads/{self.lead_id}/approve',
                                    data={'_csrf_token': 'token'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'review_approved')

    def test_a_lead_left_mid_ingest_can_be_rejected(self):
        self.client.post(f'/leads/{self.lead_id}/reject',
                         data={'_csrf_token': 'token'})
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'rejected')

    def test_a_lead_left_mid_ingest_can_be_edited(self):
        response = self.client.post(
            f'/leads/{self.lead_id}/save',
            data={'_csrf_token': 'token', 'city': 'Cincinnati'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_lead(self.lead_id)['city'], 'Cincinnati')

    def test_the_ingest_run_finishing_late_cannot_undo_that_decision(self):
        # The lead is on the page while fraud screening is still running, so
        # the run's final write has to lose a race it used to win silently.
        self.client.post(f'/leads/{self.lead_id}/approve',
                         data={'_csrf_token': 'token'})

        self.assertFalse(db.finish_ingest(self.lead_id))
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'review_approved')

    def test_an_undisturbed_ingest_still_ends_in_pending_review(self):
        self.assertTrue(db.finish_ingest(self.lead_id))
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'pending_review')


class BatchApprovalPrecisionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        db.init_db()
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    @patch('subprocess.Popen')
    def test_two_leads_with_the_same_name_are_judged_separately(self, popen):
        # Same person, two inquiries: one complete, one missing its country.
        # Held-back leads used to be filtered out by display name, so the
        # complete one was dropped from the batch alongside its namesake.
        complete = db.insert_lead(
            {'first_name': 'Ada', 'last_name': 'Lovelace',
             'email': 'ada@example.com', 'country': 'USA', 'state': 'OH',
             'organization': 'Analytical Engines'},
            'email_ai_extract', status='review_approved',
        )
        db.set_territory_assignment(complete, 'Alicia Sabillon', 'US state: OH')
        blocked = db.insert_lead(
            {'first_name': 'Ada', 'last_name': 'Lovelace',
             'email': 'ada@example.com', 'organization': 'Analytical Engines'},
            'email_ai_extract', status='review_approved',
        )

        with patch('dashboard.app.review_marking.maybe_mark_email_reviewed'):
            self.client.post('/queue/approve_all', data={'_csrf_token': 'token'})

        self.assertEqual(db.get_lead(complete)['status'], 'approved')
        self.assertEqual(db.get_lead(blocked)['status'], 'pending_review')

    @patch('subprocess.Popen')
    def test_a_lead_staged_after_the_check_is_not_swept_along(self, popen):
        """finalize_approved_batch only promotes what was actually checked."""
        checked = db.insert_lead(
            {'first_name': 'Ada', 'email': 'ada@example.com',
             'country': 'USA', 'state': 'OH'},
            'email_ai_extract', status='review_approved',
        )
        db.set_territory_assignment(checked, 'Alicia Sabillon', 'US state: OH')
        latecomer = db.insert_lead(
            {'first_name': 'Grace', 'email': 'grace@example.com'},
            'email_ai_extract', status='review_approved',
        )

        db.finalize_approved_batch(reviewed_by='dashboard', lead_ids=[checked])

        self.assertEqual(db.get_lead(checked)['status'], 'approved')
        self.assertEqual(db.get_lead(latecomer)['status'], 'review_approved')


if __name__ == '__main__':
    unittest.main()
