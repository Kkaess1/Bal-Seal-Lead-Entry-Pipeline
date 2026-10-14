"""The review form must prefer a real OSC account over creating a duplicate."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
from dashboard.app import app


class CompanyPickerTests(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()
        self.lead_id = db.insert_lead(
            {
                'first_name': 'Ada', 'last_name': 'Lovelace',
                'organization': 'Acme Corp', 'email': 'ada@example.com',
                'assigned_tm': 'Bill Rost', 'country': 'United States',
            },
            'email_ai_extract', status='pending_review',
        )
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'

    @staticmethod
    def _accounts():
        return [
            {'PartyId': 30, 'PartyNumber': 'Z30', 'OrganizationName': 'ZETA'},
            {'PartyId': 10, 'PartyNumber': 'A10', 'OrganizationName': 'ACME CORPORATION'},
            {'PartyId': 20, 'PartyNumber': 'A20', 'OrganizationName': 'ACME AEROSPACE'},
        ]

    @patch('dashboard.app._all_oracle_accounts')
    def test_endpoint_returns_every_company_with_the_best_match_first(self, accounts):
        accounts.return_value = self._accounts()

        response = self.client.get(f'/leads/{self.lead_id}/oracle-companies')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            [item['party_id'] for item in payload['accounts']],
            ['10', '20', '30'],
        )
        self.assertEqual(len(payload['accounts']), len(self._accounts()))

    def test_saving_a_selection_persists_the_party_id_and_canonical_name(self):
        response = self.client.post(
            f'/leads/{self.lead_id}/save',
            data={
                '_csrf_token': 'token',
                'organization': 'Acme Corp',
                'oracle_account_party_id': '100000055846517',
                'oracle_account_name': 'ACME CORPORATION',
                'oracle_account_party_number': 'A10',
            },
        )

        self.assertEqual(response.status_code, 302)
        lead = db.get_lead(self.lead_id)
        self.assertEqual(lead['organization'], 'ACME CORPORATION')
        self.assertEqual(lead['oracle_account_party_id'], '100000055846517')
        self.assertEqual(lead['oracle_account_party_number'], 'A10')

    def test_returning_to_free_text_clears_a_stale_account_identity(self):
        db.update_lead_fields(self.lead_id, {
            'oracle_account_name': 'ACME CORPORATION',
            'oracle_account_party_id': '100000055846517',
            'oracle_account_party_number': 'A10',
        })

        self.client.post(
            f'/leads/{self.lead_id}/save',
            data={
                '_csrf_token': 'token',
                'organization': 'Brand New Company',
                'oracle_account_party_id': '',
                'oracle_account_name': '',
                'oracle_account_party_number': '',
            },
        )

        lead = db.get_lead(self.lead_id)
        self.assertEqual(lead['organization'], 'Brand New Company')
        self.assertIsNone(lead['oracle_account_party_id'])
        self.assertIsNone(lead['oracle_account_name'])

    @patch('dashboard.app.db.get_lead_email_thread', return_value=[])
    def test_edit_page_contains_the_osc_company_picker(self, _thread):
        response = self.client.get(f'/leads/{self.lead_id}')

        page = response.get_data(as_text=True)
        self.assertIn('class="oracle-company-select"', page)
        self.assertIn(f'/leads/{self.lead_id}/oracle-companies', page)


if __name__ == '__main__':
    unittest.main()
