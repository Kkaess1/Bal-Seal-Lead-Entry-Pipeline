import tempfile
import unittest
from pathlib import Path

import db
from dashboard.app import app


class StatusConversionRatesTests(unittest.TestCase):
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

    def test_status_renders_zero_rates_without_division_error(self):
        response = self.client.get('/status')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Qualification Rate', html)
        self.assertIn('Opportunity Conversion', html)
        self.assertIn('Overall Conversion Rate', html)
        self.assertIn('0.0%', html)

    def test_status_computes_and_renders_correct_percentages(self):
        # Insert 4 intake leads
        l1 = db.insert_lead({'first_name': 'Lead', 'last_name': 'One'}, 'email_ai_extract', status='submitted')
        l2 = db.insert_lead({'first_name': 'Lead', 'last_name': 'Two'}, 'email_ai_extract', status='submitted')
        l3 = db.insert_lead({'first_name': 'Lead', 'last_name': 'Three'}, 'email_ai_extract', status='rejected')
        l4 = db.insert_lead({'first_name': 'Lead', 'last_name': 'Four'}, 'email_ai_extract', status='rejected')

        db.update_lead_fields(l1, {'oracle_record_id': '101'})
        db.update_lead_fields(l2, {'oracle_record_id': '102'})

        # Save oracle snapshots: 2 submitted, 2 synced, 1 qualified, 1 converted
        db.save_oracle_snapshot(
            l1,
            {
                'oracle_lead_id': '101', 'oracle_status': 'QUALIFIED',
                'qualified_at': '2026-08-10T10:00:00Z', 'converted_at': '2026-08-12T10:00:00Z',
            },
            [{'oracle_opportunity_id': '201', 'opportunity_number': 'OPTY-1'}],
        )
        db.save_oracle_snapshot(
            l2,
            {'oracle_lead_id': '102', 'oracle_status': 'UNQUALIFIED'},
            [],
        )

        # Submitted = 2, Synced = 2, Qualified = 1, Converted = 1, Total = 4
        # Qualification Rate = 1/2 = 50.0%
        # Opportunity Conversion = 1/2 = 50.0%
        # Overall Pipeline Rate = 1/4 = 25.0%
        response = self.client.get('/status')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertIn('50.0%', html)
        self.assertIn('25.0%', html)
        self.assertIn('1 of 2 submitted leads qualified in Oracle', html)
        self.assertIn('1 of 2 submitted leads converted to Opportunities', html)
        self.assertIn('1 converted of 4 total pipeline intake leads', html)

    def test_status_computes_days_to_qualify(self):
        from dashboard.app import _calculate_days_to_qualify
        # 3 days diff
        self.assertEqual(_calculate_days_to_qualify('2026-08-01T10:00:00Z', '2026-08-04T12:00:00Z'), 3)
        # Same day
        self.assertEqual(_calculate_days_to_qualify('2026-08-01T10:00:00Z', '2026-08-01T18:00:00Z'), 0)
        # None when not qualified
        self.assertIsNone(_calculate_days_to_qualify('2026-08-01T10:00:00Z', None))

        l1 = db.insert_lead({'first_name': 'Fast', 'last_name': 'Lead'}, 'email_ai_extract', status='submitted')
        db.update_lead_fields(l1, {'oracle_record_id': '201', 'created_at': '2026-08-01T10:00:00Z'})
        db.save_oracle_snapshot(
            l1,
            {
                'oracle_lead_id': '201', 'oracle_status': 'QUALIFIED',
                'qualified_at': '2026-08-04T15:00:00Z',
            },
            [],
        )
        response = self.client.get('/status')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('3 days', html)
        self.assertIn('Qualified on 2026-08-04', html)


if __name__ == '__main__':
    unittest.main()
