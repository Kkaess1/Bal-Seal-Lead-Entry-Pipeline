"""
Tests for searching/looking up leads in the History tab by name or organization.
"""

import os
import tempfile
import unittest
from pathlib import Path

import db
import lead_stages
from dashboard.app import app


class HistorySearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.tmp.close()
        self.db_path = Path(self.tmp.name)
        self.orig_db = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()

        app.config['TESTING'] = True
        self.client = app.test_client()

        # Create sample leads with finished status ('submitted' or 'rejected')
        self.lead1_id = db.insert_lead({
            'first_name': 'Alice',
            'last_name': 'Smith',
            'organization': 'Acme Corp',
            'email': 'alice@acme.com',
        }, 'test', status='submitted')
        self.lead2_id = db.insert_lead({
            'first_name': 'Bob',
            'last_name': 'Jones',
            'organization': 'Beta Inc',
            'email': 'bob@beta.com',
        }, 'test', status='rejected')
        self.lead3_id = db.insert_lead({
            'first_name': 'Charlie',
            'last_name': 'Brown',
            'organization': 'Acme Global',
            'email': 'charlie@acmeglobal.com',
        }, 'test', status='submitted')
        self.lead4_id = db.insert_lead({
            'first_name': 'David',
            'last_name': 'Smith',
            'organization': 'Gamma LLC',
            'email': 'david@gamma.com',
        }, 'test', status='pending_review')  # Not finished, shouldn't appear in history

    def tearDown(self):
        db.DB_PATH = self.orig_db
        if self.db_path.exists():
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def test_db_list_leads_filter_by_name(self):
        history_statuses = list(lead_stages.HISTORY)
        
        # Search by first name
        leads = db.list_leads(status=history_statuses, query='Alice')
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]['first_name'], 'Alice')

        # Search by last name
        leads = db.list_leads(status=history_statuses, query='Smith')
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]['first_name'], 'Alice')

        # Search by full name
        leads = db.list_leads(status=history_statuses, query='Bob Jones')
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]['first_name'], 'Bob')

    def test_db_list_leads_filter_by_organization(self):
        history_statuses = list(lead_stages.HISTORY)

        # Search by organization substring 'Acme'
        leads = db.list_leads(status=history_statuses, query='Acme')
        self.assertEqual(len(leads), 2)
        orgs = {l['organization'] for l in leads}
        self.assertEqual(orgs, {'Acme Corp', 'Acme Global'})

        count = db.count_leads(status=history_statuses, query='Acme')
        self.assertEqual(count, 2)

    def test_history_route_search(self):
        # GET /history with query parameter q=Beta
        resp = self.client.get('/history?q=Beta')
        self.assertEqual(resp.status_code, 200)
        content = resp.data.decode('utf-8')
        self.assertIn('Beta Inc', content)
        self.assertNotIn('Acme Corp', content)

        # GET /history with query parameter q=Nonexistent
        resp = self.client.get('/history?q=Nonexistent')
        self.assertEqual(resp.status_code, 200)
        content = resp.data.decode('utf-8')
        self.assertIn('No leads found matching "Nonexistent"', content)


if __name__ == '__main__':
    unittest.main()
