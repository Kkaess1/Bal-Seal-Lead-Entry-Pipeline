import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openpyxl

import ai_column_mapper
import db
import excel_parser
import oracle_api
from phone_parser import parse_phone


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / 'test.db'
        db.init_db(self.db_path)
        self.conn = db.get_connection(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def test_ingest_key_is_idempotent_and_force_updates(self):
        first = {'email': 'a@example.com', 'first_name': 'A'}
        lead_id = db.insert_lead(
            first, 'email_ai_extract', ingest_key='message:1:body', conn=self.conn
        )
        same_id = db.insert_lead(
            {'email': 'a@example.com', 'first_name': 'Changed'},
            'email_ai_extract',
            ingest_key='message:1:body',
            replace_existing=True,
            conn=self.conn,
        )
        self.conn.commit()
        self.assertEqual(lead_id, same_id)
        self.assertEqual(db.get_lead(lead_id, conn=self.conn)['first_name'], 'Changed')
        self.assertEqual(db.count_leads(conn=self.conn), 1)

    def test_message_and_submission_claims_are_atomic(self):
        message_id = db.insert_message('source-1', conn=self.conn)
        self.assertTrue(db.claim_message(message_id, conn=self.conn))
        self.assertFalse(db.claim_message(message_id, conn=self.conn))

        lead_id = db.insert_lead(
            {'email': 'a@example.com'}, 'test', status='approved', conn=self.conn
        )
        self.assertTrue(db.claim_lead_for_submission(lead_id, conn=self.conn))
        self.assertFalse(db.claim_lead_for_submission(lead_id, conn=self.conn))
        self.assertEqual(db.get_lead(lead_id, conn=self.conn)['status'], 'submitting')

    def test_unknown_submission_requires_explicit_resolution(self):
        lead_id = db.insert_lead(
            {'email': 'a@example.com'}, 'test', status='approved', conn=self.conn
        )
        db.claim_lead_for_submission(lead_id, conn=self.conn)
        db.mark_submission_unknown(
            lead_id, 'uncertain', oracle_project_name='PROJECT-1', conn=self.conn
        )
        lead = db.get_lead(lead_id, conn=self.conn)
        self.assertEqual(lead['status'], 'submission_unknown')
        self.assertEqual(lead['oracle_project_name'], 'PROJECT-1')
        db.schedule_lead_retry(lead_id, 'operator authorized', delay_minutes=0, conn=self.conn)
        self.assertEqual(db.get_lead(lead_id, conn=self.conn)['status'], 'approved')


class ParserTests(unittest.TestCase):
    def test_phone_formats(self):
        self.assertEqual(parse_phone('1-800-555-1234')['area_code'], '800')
        self.assertEqual(parse_phone('+442012345678')['country_code'], '44')
        self.assertEqual(parse_phone('+442012345678')['area_code'], '20')
        self.assertNotIn('42', parse_phone('202 555 0199 ext 42')['number'])

    def test_header_fingerprint_is_position_sensitive(self):
        self.assertNotEqual(
            ai_column_mapper._fingerprint(['First Name', 'Email']),
            ai_column_mapper._fingerprint(['Email', 'First Name']),
        )

    def test_legacy_xls_has_clear_error(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / 'legacy.xls'
            path.write_bytes(b'not an xls workbook')
            with self.assertRaisesRegex(ValueError, 'not supported'):
                excel_parser.parse_excel(path)

    def test_arbitrary_ninth_column_is_not_assumed_to_be_rank(self):
        headers = ['First Name', 'Last Name', 'Company', 'Email',
                   'A', 'B', 'C', 'D', 'Unrelated']
        with patch('excel_parser.map_columns_with_ai', return_value={}):
            mapping = excel_parser._build_column_map(headers)
        self.assertNotIn('rank', mapping)

    def test_xlsx_parsing(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / 'leads.xlsx'
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.append(['First Name', 'Last Name', 'Company', 'Email'])
            sheet.append(['Ada', 'Lovelace', 'Analytical Engines', 'ada@example.com'])
            workbook.save(path)
            with patch('excel_parser.map_columns_with_ai', return_value={}):
                leads = excel_parser.parse_excel(path)
            self.assertEqual(len(leads), 1)
            self.assertEqual(leads[0]['organization'], 'Analytical Engines')


class OracleApiTests(unittest.TestCase):
    def test_build_payload_maps_verified_custom_fields(self):
        lead = {
            'first_name': 'Ada',
            'last_name': 'Lovelace',
            'organization': 'Analytical Engines',
            'title': 'Engineer',
            'phone': '202-555-0199',
            'email': 'ada@example.com',
            'website': 'https://example.com',
            'address1': '1 Difference Way',
            'city': 'London',
            'state': 'CA',
            'zip': '90210',
            'country': 'United States',
            'rank': 'Hot',
            'market': 'INDUSTRIAL',
            'lead_details': 'Needs a seal quote.',
        }
        payload, project_name, warnings = oracle_api.build_lead_payload(
            lead, 'Website/Email', source_label='Inquiry', lead_source='Website',
            campaign_name='Seal Starter',
        )
        self.assertEqual(
            project_name,
            'ANALYTICAL ENGINES - Ada Lovelace - Inquiry - Website/Email',
        )
        self.assertEqual(payload['Name'], project_name)
        self.assertEqual(payload['CustomerPartyName'], 'Analytical Engines')
        self.assertEqual(payload['PrimaryContactPersonFirstName'], 'Ada')
        self.assertEqual(payload['PrimaryContactPersonLastName'], 'Lovelace')
        self.assertEqual(payload['LeadSourceDescriptionBSE_c'], 'Website')
        self.assertEqual(
            payload['IndustryDescriptionBSE_c'], 'NON-TARGETED SEALS/SPRINGS'
        )
        self.assertEqual(payload['Rank'], 'HOT')
        self.assertEqual(payload['PhoneAreaCodeBSE_c'], '202')
        self.assertEqual(payload['PhoneNumberBSE_c'], '555-0199')
        self.assertEqual(payload['PrimaryContactState'], 'CA')
        self.assertFalse(warnings)

    def test_canadian_sample_uses_province_and_aerospace_market(self):
        lead = {
            'first_name': 'Charles',
            'last_name': 'Hamel',
            'organization': 'UNIVERSITY OF SHERBROOKE CTA',
            'title': 'Mechanical Engineering Technician',
            'phone': '1 819 821-7657',
            'email': 'charles.hamel@cta-brp-udes.com',
            'address1': "3000 boulevard de l'Universite",
            'city': 'SHERBROOKE',
            'state': 'QUEBEC',
            'country': 'Canada',
            'rank': 'Hot',
            'market': 'AEROSPACE/DEFENSE',
            'lead_details': 'Request for technical guidance.',
            'campaign': 'Seal Starter',
            'assigned_tm': 'Daniel Smoliga',
        }
        payload, project_name, warnings = oracle_api.build_lead_payload(
            lead, 'Cadenas RFQ', source_label='', lead_source='Website'
        )
        self.assertEqual(
            project_name,
            'UNIVERSITY OF SHERBROOKE CTA - Charles Hamel - Cadenas RFQ',
        )
        self.assertEqual(payload['PrimaryContactCountry'], 'CA')
        self.assertEqual(payload['PrimaryContactProvince'], 'QUEBEC')
        self.assertNotIn('PrimaryContactState', payload)
        self.assertEqual(payload['IndustryDescriptionBSE_c'], 'AEROSPACE/DEFENSE')
        self.assertEqual(payload['OwnerPartyName'], 'Daniel Smoliga')
        self.assertEqual(payload['PhoneAreaCodeBSE_c'], '819')
        self.assertEqual(payload['PhoneNumberBSE_c'], '821-7657')
        self.assertFalse(warnings)

    def test_all_known_market_dropdowns_have_api_values(self):
        for market in [
            'AEROSPACE/DEFENSE',
            'ALTERNATIVE ENERGY',
            'ANALYTICAL DEVICES',
            'FOREIGN DISTRIBUTORS',
            'INDUSTRIAL',
            'MEDICAL DEVICES',
            'MEDICAL ELECTRONICS',
            'MOBILITY',
            'OIL & GAS',
            'POWER T&D',
        ]:
            payload, _, warnings = oracle_api.build_lead_payload(
                {'market': market},
                'Website/Email',
                source_label='Inquiry',
                lead_source='Website',
            )
            self.assertIn('IndustryDescriptionBSE_c', payload)
            self.assertFalse(warnings)

    def test_unknown_market_defaults_with_warning(self):
        payload, _, warnings = oracle_api.build_lead_payload(
            {'market': 'UNEXPECTED MARKET'},
            'Website/Email',
            source_label='Inquiry',
            lead_source='Website',
        )
        self.assertEqual(
            payload['IndustryDescriptionBSE_c'], 'NON-TARGETED SEALS/SPRINGS'
        )
        self.assertTrue(any('UNEXPECTED MARKET' in warning for warning in warnings))


if __name__ == '__main__':
    unittest.main()
