"""
Tests for the /ingest/status endpoint and run lease status checks used to drive
the dynamic loading bar on the dashboard.
"""

import tempfile
import unittest
from pathlib import Path

import db
from dashboard.app import app, _INGEST_JOB_KEY


class IngestStatusEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()
        self.client = app.test_client()

    def test_status_returns_not_running_when_no_lease(self):
        response = self.client.get('/ingest/status')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['running'])
        self.assertIsNone(data['status'])

    def test_status_returns_running_when_starting(self):
        token = db.acquire_run_lease(_INGEST_JOB_KEY)
        db.update_run_lease(_INGEST_JOB_KEY, token, 'starting', pid=1234)

        response = self.client.get('/ingest/status')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['running'])
        self.assertEqual(data['status'], 'starting')
        self.assertEqual(data['pid'], 1234)

    def test_status_returns_running_when_running(self):
        token = db.acquire_run_lease(_INGEST_JOB_KEY)
        db.update_run_lease(_INGEST_JOB_KEY, token, 'running', pid=5678)

        response = self.client.get('/ingest/status')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['running'])
        self.assertEqual(data['status'], 'running')

    def test_status_returns_not_running_when_succeeded_or_failed(self):
        token = db.acquire_run_lease(_INGEST_JOB_KEY)
        db.update_run_lease(_INGEST_JOB_KEY, token, 'succeeded')

        response = self.client.get('/ingest/status')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['running'])
        self.assertIsNone(data['status'])


if __name__ == '__main__':
    unittest.main()
