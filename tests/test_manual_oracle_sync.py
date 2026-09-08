import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
from dashboard.app import app


class ManualOracleSyncTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        db.init_db()
        app.config['TESTING'] = True
        self.client = app.test_client()

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.tempdir.cleanup()

    @patch('subprocess.Popen')
    def test_trigger_oracle_sync_launches_subprocess_and_acquires_lease(self, mock_popen):
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'
        response = self.client.post('/status/sync', data={'_csrf_token': 'token'}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_popen.called)
        html = response.get_data(as_text=True)
        self.assertIn('Started Oracle outcome sync in the background', html)

        # Verify lease token was acquired
        lease = db.get_run_lease('job:manual_oracle_sync')
        self.assertIsNotNone(lease)
        self.assertEqual(lease.get('status'), 'starting')

    @patch('subprocess.Popen')
    def test_second_trigger_blocked_while_sync_running(self, mock_popen):
        # Acquire lease first
        token = db.acquire_run_lease('job:manual_oracle_sync')
        self.assertIsNotNone(token)

        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'
        # Second trigger attempt
        response = self.client.post('/status/sync', data={'_csrf_token': 'token'}, follow_redirects=True)
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(mock_popen.called)
        self.assertIn('An Oracle sync is already in progress', html)


if __name__ == '__main__':
    unittest.main()
