import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openpyxl

import ai_column_mapper
import company_website
import db
import email_cleaner
import email_translator
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

    def test_same_batch_thread_is_one_lead_but_later_followup_is_new(self):
        first_message = db.insert_message(
            'thread-msg-1', received_at='2026-08-05T09:00:00+00:00',
            conversation_id='conversation-1', ingest_batch_id='batch-1', conn=self.conn,
        )
        latest_message = db.insert_message(
            'thread-msg-2', received_at='2026-08-05T09:05:00+00:00',
            conversation_id='conversation-1', ingest_batch_id='batch-1', conn=self.conn,
        )
        first_lead = db.insert_lead(
            {'email': 'person@example.com', 'first_name': 'Person'}, 'email_ai_extract',
            message_id=first_message, status='pending_review', conn=self.conn,
        )
        latest_lead = db.insert_lead(
            {'email': 'person@example.com', 'first_name': 'Person'}, 'email_ai_extract',
            message_id=latest_message, status='pending_review', conn=self.conn,
        )

        self.assertEqual(db.consolidate_ingest_batch_threads('batch-1', conn=self.conn), 1)
        self.assertIsNone(db.get_lead(first_lead, conn=self.conn))
        self.assertIsNotNone(db.get_lead(latest_lead, conn=self.conn))

        later_message = db.insert_message(
            'thread-msg-3', received_at='2026-08-06T09:00:00+00:00',
            conversation_id='conversation-1', ingest_batch_id='batch-2', conn=self.conn,
        )
        later_lead = db.insert_lead(
            {'email': 'person@example.com', 'first_name': 'Person'}, 'email_ai_extract',
            message_id=later_message, status='pending_review', conn=self.conn,
        )
        self.assertEqual(db.consolidate_ingest_batch_threads('batch-2', conn=self.conn), 0)
        self.assertEqual(len(db.list_leads(status='pending_review', conn=self.conn)), 2)
        self.assertEqual(len(db.get_lead_email_thread(later_lead, conn=self.conn)), 3)


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


class EmailCleanerTests(unittest.TestCase):
    """Form-submission trimming. Both fixtures are the real layouts seen in
    email_cache/, shortened: the tab structure is what the parser keys off,
    so it has to survive editing."""

    JOTFORM = (
        'CAUTION: This email originated from outside of the organization.\n\n'
        ' <https://cdn.jotfor.ms/assets/img/logo2021/jotform-logo.png> \t\n'
        'Design Request Form\n\n'
        '\tPlease choose your solution\t Bal Contact electrical contact\t \n'
        "Tell us about your application or engineering challenge, and we'll get to work on it "
        'right away. The more we know, the faster we can help.\t We would like a good RF contact '
        'in the region 1-4 GHz between two waveguides (WR284) upon compression. \t\n'
        'Your name\t Daniele Filippetto\t \n'
        'Company name\t DC Fusion\t \n'
        'Your email address\t df@dcfusionllc.com\t \n'
        'Your phone number\t (510) 219-1011\t \n'
        'International phone (if applicable)\t 5102191011\t \n'
        'Address\t 660 4th St., San Francisco, California, 94107, United States\t \n'
        '\tYou can edit this submission <https://www.jotform.com/edit/1234567890abcdefghij> and '
        'view all your submissions easily.\t \t\n'
    )

    GRAVITY = (
        'To get started, select one of the product types below. \t\n \t Bal Seal \t\n'
        'Please tell us what kind of service your Bal Seal will perform. \t\n \t Rotary \t\n'
        'Tell us about your application or engineering challenge. \t\n'
        ' \t I need to pressurize through my rotary shaft with air.\n\n'
        'Shaft 40mm, 5000 rpm.\n\n'
        'Regards,\nTom Kivisto\n'
        '123 Main St, Minneapolis, MN 55401 \t\n'
        'Name* \t\n \t Tom Kivisto \t\n'
        'Company \t\n \t Forward Technology \t\n'
        'Email \t\n \t tkivisto@forwardtech.com <mailto:tkivisto@forwardtech.com>  \t\n'
        'Phone \t\n \t 2816080319 \t\n'
        'Address \t\n \t MN\nUnited States\nMap It \t\n'
        'Consent \t\n \t I agree to the privacy policy. \t\n'
    )

    def test_jotform_layout_keeps_only_the_question(self):
        cleaned = email_cleaner.clean_email_body(self.JOTFORM)
        self.assertEqual(
            cleaned,
            'Solution: Bal Contact electrical contact\n\n'
            'We would like a good RF contact in the region 1-4 GHz between two waveguides '
            '(WR284) upon compression.'
        )

    def test_gravity_layout_keeps_multi_paragraph_answer_and_signature(self):
        cleaned = email_cleaner.clean_email_body(self.GRAVITY)
        self.assertEqual(
            cleaned,
            'Product type: Bal Seal\n\n'
            'Service: Rotary\n\n'
            'I need to pressurize through my rotary shaft with air.\n\n'
            'Shaft 40mm, 5000 rpm.\n\n'
            'Regards,\nTom Kivisto\n123 Main St, Minneapolis, MN 55401'
        )

    def test_prose_email_is_not_treated_as_a_form(self):
        prose = (
            'Hi,\n\nI am looking for canted coil springs\tfor a sliding mechanism.\n\n'
            'Thanks,\nAndy\n'
        )
        self.assertIsNone(email_cleaner.extract_form_submission(prose))
        self.assertIn('canted coil springs', email_cleaner.clean_email_body(prose))

    def test_cleaning_is_idempotent(self):
        once = email_cleaner.clean_email_body(self.JOTFORM)
        self.assertEqual(email_cleaner.clean_email_body(once), once)

    def test_product_labels_survive_the_contact_field_filter(self):
        # "part name" ends in a contact word but is the answer, not the sender.
        self.assertFalse(email_cleaner._is_contact_label('Part name'))
        self.assertTrue(email_cleaner._is_contact_label('International phone (if applicable)'))
        self.assertTrue(email_cleaner._is_contact_label('Your name'))
        self.assertTrue(email_cleaner._is_contact_label('State/Area:'))


class EmailTranslatorTests(unittest.TestCase):
    def test_english_does_not_need_translation(self):
        for language in ('English', 'en', 'EN-US'):
            self.assertFalse(email_translator.needs_translation(language))

    def test_non_english_language_needs_translation(self):
        self.assertTrue(email_translator.needs_translation('German'))
        self.assertTrue(email_translator.needs_translation('Japanese'))

    @patch('email_translator.ai_provider.call_llm')
    def test_translation_returns_complete_subject_and_body(self, call_llm):
        call_llm.return_value = (
            '{"subject":"Quote request","body":"Hello, I need a seal.\\nRegards, Ana"}'
        )
        translated = email_translator.translate_email(
            'Solicitud', 'Hola, necesito un sello.', 'Spanish'
        )
        self.assertEqual(translated['subject'], 'Quote request')
        self.assertIn('I need a seal', translated['body'])

    def test_non_english_ingest_stores_translation_and_language(self):
        import pipeline_ingest
        with tempfile.TemporaryDirectory() as tempdir:
            original_db_path = db.DB_PATH
            original_cache_dir = pipeline_ingest.EMAIL_CACHE_DIR
            db.DB_PATH = Path(tempdir) / 'test.db'
            pipeline_ingest.EMAIL_CACHE_DIR = Path(tempdir) / 'cache'
            try:
                db.init_db()
                message_id = db.insert_message('spanish-message')
                extraction = {
                    'is_lead': True,
                    'is_followup': False,
                    'source_language': 'Spanish',
                    'confidence': 0.9,
                    'fields': {'email': 'ana@example.com', 'organization': 'Ejemplo'},
                    'notes': 'Valid inquiry.',
                    'plausibility': {'fraud_risk': 'clear', 'reasons': []},
                }
                msg = {
                    'id': 'spanish-message', 'subject': 'Solicitud',
                    'sender_email': 'ana@example.com', 'sender_name': 'Ana',
                    'body_text': 'Hola, necesito un sello.',
                }
                with patch('pipeline_ingest.lead_extractor.extract_lead', return_value=extraction), \
                     patch('pipeline_ingest.email_translator.translate_email', return_value={
                         'subject': 'Request', 'body': 'Hello, I need a seal.',
                     }), \
                     patch('pipeline_ingest._screen_and_stage'):
                    pipeline_ingest._process_body_text(message_id, msg)

                lead = db.list_leads()[0]
                stored_message = db.get_message(message_id)
                self.assertEqual(lead['lead_details'], 'Hello, I need a seal.')
                self.assertEqual(stored_message['original_language'], 'Spanish')
                self.assertEqual(stored_message['translated_subject'], 'Request')
                self.assertTrue(Path(stored_message['translated_body_cache_path']).exists())
            finally:
                db.DB_PATH = original_db_path
                pipeline_ingest.EMAIL_CACHE_DIR = original_cache_dir


class AttachmentMetadataTests(unittest.TestCase):
    """Attachments are recorded as metadata and never stored or served."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / 'test.db'
        db.init_db(self.db_path)
        self.conn = db.get_connection(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def test_round_trip(self):
        message_id = db.insert_message('src-1', conn=self.conn)
        db.record_attachments(message_id, [
            {'name': 'seal-3.jpg', 'size': 421890, 'kind': 'image'},
        ], conn=self.conn)
        self.conn.commit()
        got = db.get_message_attachments(message_id, conn=self.conn)
        self.assertEqual(got[0]['name'], 'seal-3.jpg')
        self.assertEqual(got[0]['kind'], 'image')

    def test_missing_and_malformed_values_render_as_empty(self):
        message_id = db.insert_message('src-2', conn=self.conn)
        self.assertEqual(db.get_message_attachments(message_id, conn=self.conn), [])
        self.assertEqual(db.get_message_attachments(None, conn=self.conn), [])
        db.update_message_fields(message_id, {'attachments': 'not json'}, conn=self.conn)
        self.conn.commit()
        self.assertEqual(db.get_message_attachments(message_id, conn=self.conn), [])

    def test_migration_adds_column_to_an_existing_database(self):
        # The production database predates this column; init_db has to add it
        # rather than only creating it on fresh installs.
        path = Path(self.tempdir.name) / 'legacy.db'
        conn = db.get_connection(path)
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_message_id TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL)""")
        conn.commit()
        conn.close()
        db.init_db(path)
        conn = db.get_connection(path)
        columns = {row[1] for row in conn.execute('PRAGMA table_info(messages)')}
        conn.close()
        self.assertIn('attachments', columns)

    def test_signature_logos_are_not_reported_as_attachments(self):
        import outlook_ingest
        for embedded in ('image001.png', 'image0023.jpg', 'logo.gif', 'oledata.emf'):
            self.assertRegex(embedded, outlook_ingest._EMBEDDED_IMAGE_RE, embedded)
        for real in ('seal-3.jpg', 'V150-omniseal.pdf', 'drawing.dwg', 'image of seal.png'):
            self.assertNotRegex(real, outlook_ingest._EMBEDDED_IMAGE_RE, real)

    def test_attachment_kind_labels(self):
        import outlook_ingest
        self.assertEqual(outlook_ingest._attachment_kind('seal-3.JPG'), 'image')
        self.assertEqual(outlook_ingest._attachment_kind('part.STEP'), 'drawing/CAD')
        self.assertEqual(outlook_ingest._attachment_kind('quote.pdf'), 'document')
        self.assertEqual(outlook_ingest._attachment_kind('payload.exe'), 'exe')
        self.assertEqual(outlook_ingest._attachment_kind('noextension'), 'file')


class CompanyWebsiteTests(unittest.TestCase):
    def test_work_address_yields_the_company_domain(self):
        self.assertEqual(
            company_website.derive_website({'email': 'df@dcfusionllc.com'}),
            'https://dcfusionllc.com',
        )
        # Multi-part TLDs are kept whole rather than reduced to a registrable
        # domain -- the mail domain is the site.
        self.assertEqual(
            company_website.derive_website({'email': 'a@eng-mag.co.uk'}),
            'https://eng-mag.co.uk',
        )

    def test_consumer_and_relay_domains_yield_nothing(self):
        for email in ('tdoskamp@gmail.com', 'someone@me.com', 'x@mailinator.com',
                      'rep@balseal.com', 'noreply@jotform.com', 'nodomain'):
            self.assertIsNone(
                company_website.derive_website({'email': email}), email
            )

    def test_stated_website_wins_and_is_normalized(self):
        lead = {'email': 'buyer@gmail.com', 'website': 'www.acme-seals.com/products?ref=x'}
        self.assertEqual(company_website.derive_website(lead), 'https://www.acme-seals.com')
        # An explicit http:// is preserved; not every industrial supplier redirects.
        self.assertEqual(
            company_website.derive_website({'website': 'http://legacy.com'}),
            'http://legacy.com',
        )

    def test_junk_website_values_fall_back_to_the_email_domain(self):
        for junk in ('n/a', 'none', '-', 'ask me', 'sales@dcfusionllc.com'):
            self.assertEqual(
                company_website.derive_website({'website': junk, 'email': 'df@dcfusionllc.com'}),
                'https://dcfusionllc.com',
                junk,
            )

    def test_ingest_fills_the_field_it_stores(self):
        # Guards the wiring, not the rule: derivation has to land on the key
        # db.LEAD_SCHEMA_FIELDS actually persists, or none of it reaches Oracle.
        import pipeline_ingest
        self.assertIn('website', db.LEAD_SCHEMA_FIELDS)
        lead = pipeline_ingest._fill_website({'email': 'df@dcfusionllc.com'})
        self.assertEqual(lead['website'], 'https://dcfusionllc.com')
        self.assertNotIn('website', pipeline_ingest._fill_website({'email': 'x@gmail.com'}))

    def test_ingest_defaults_a_missing_market_to_industrial(self):
        import pipeline_ingest
        self.assertEqual(pipeline_ingest._fill_market({})['market'], 'INDUSTRIAL')
        self.assertEqual(
            pipeline_ingest._fill_market({'market': '   '})['market'], 'INDUSTRIAL'
        )
        self.assertEqual(
            pipeline_ingest._fill_market({'market': 'MOBILITY'})['market'], 'MOBILITY'
        )

    def test_only_http_urls_become_clickable_links(self):
        # The website box is editable on the review form, so the dashboard
        # decides what is linkable rather than trusting what is stored.
        from dashboard.app import _decorate
        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            self.assertEqual(
                _decorate({'id': 1, 'website': 'https://dcfusionllc.com'})['website_url'],
                'https://dcfusionllc.com',
            )
            for hostile in ('javascript:alert(1)', 'data:text/html,x', 'n/a', ''):
                self.assertIsNone(
                    _decorate({'id': 1, 'website': hostile})['website_url'], hostile
                )

    def test_ip_addresses_are_not_websites(self):
        self.assertIsNone(company_website.normalize_url('http://192.168.1.10'))
        self.assertIsNone(company_website.derive_website({'email': 'a@192.168.1.10'}))


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
            # These leads carry no country, which warns on its own -- assert
            # only that the market mapped without complaint.
            self.assertFalse([w for w in warnings if 'market' in w.lower()])

    def test_website_is_only_sent_when_the_tenant_field_is_configured(self):
        lead = {'market': 'INDUSTRIAL', 'website': 'https://dcfusionllc.com'}
        payload, _, _ = oracle_api.build_lead_payload(
            lead, 'Website/Email', source_label='Inquiry', lead_source='Website',
        )
        self.assertNotIn('dcfusionllc.com', json.dumps(payload))

        payload, _, _ = oracle_api.build_lead_payload(
            lead, 'Website/Email', source_label='Inquiry', lead_source='Website',
            website_field='PrimaryContactURLBSE_c',
        )
        self.assertEqual(payload['PrimaryContactURLBSE_c'], 'https://dcfusionllc.com')

    def test_configured_website_field_is_omitted_when_lead_has_none(self):
        payload, _, _ = oracle_api.build_lead_payload(
            {'market': 'INDUSTRIAL'}, 'Website/Email', source_label='Inquiry',
            lead_source='Website', website_field='PrimaryContactURLBSE_c',
        )
        self.assertNotIn('PrimaryContactURLBSE_c', payload)

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
