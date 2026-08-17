"""
Autocomplete for the forward and reply address boxes.

Suggestions come from what the dashboard has already seen. Somewhere you have
already sent something ranks first -- that is the short list of colleagues a
lead gets handed to, and the whole point of not retyping an address.

Forwards used to leave a trace only in a lead's review_notes prose, so the
addresses most worth suggesting were the ones not recorded anywhere usable.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
from dashboard.app import app


class KnownAddressTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()

        self.message_id = db.insert_message(
            'entry-1', subject='Quote', sender_email='ada@customer.com',
            sender_name='Ada Lovelace', received_at='2026-08-01T09:00:00',
        )
        db.set_message_recipients(self.message_id, {
            'to': [{'name': 'Sales', 'address': 'sales@balseal.com'}],
            'cc': [{'name': 'Bystander', 'address': 'cc@customer.com'}],
        })
        self.lead_id = db.insert_lead(
            {'first_name': 'Ada', 'email': 'ada@customer.com'},
            'email_ai_extract', message_id=self.message_id, status='pending_review',
        )

    def _addresses(self):
        return [a['address'] for a in db.list_known_addresses()]

    def test_senders_and_people_on_the_email_are_offered(self):
        found = self._addresses()

        self.assertIn('ada@customer.com', found)
        self.assertIn('cc@customer.com', found)

    def test_somewhere_you_have_sent_before_ranks_first(self):
        db.record_lead_reply(
            self.lead_id, 'Passing this to you.', recipients=['brost@balseal.com'],
            kind='forward',
        )

        known = db.list_known_addresses()

        self.assertEqual(known[0]['address'], 'brost@balseal.com')

    def test_a_reply_recipient_is_remembered_too(self):
        db.record_lead_reply(
            self.lead_id, 'Which size?', recipients=['ada@customer.com'],
            cc_recipients=['manager@balseal.com'],
        )

        known = {a['address'] for a in db.list_known_addresses()}

        self.assertIn('manager@balseal.com', known)

    def test_each_address_appears_once_regardless_of_case(self):
        db.record_lead_reply(self.lead_id, 'hi', recipients=['Ada@Customer.com'])

        found = [a.lower() for a in self._addresses()]

        self.assertEqual(found.count('ada@customer.com'), 1)

    def test_names_are_carried_through_when_known(self):
        by_address = {a['address']: a['name'] for a in db.list_known_addresses()}

        self.assertEqual(by_address['ada@customer.com'], 'Ada Lovelace')

    def test_saved_territory_contact_can_be_found_by_name(self):
        db.record_lead_reply(
            self.lead_id, 'Passing this to you.', recipients=['wkahout@balseal.com'],
            kind='forward',
        )

        by_address = {a['address']: a['name'] for a in db.list_known_addresses()}

        self.assertEqual(by_address['wkahout@balseal.com'], 'Wade Kahout')

    def test_territory_contact_email_is_inferred_before_it_has_been_used(self):
        by_address = {a['address']: a for a in db.list_known_addresses()}

        self.assertEqual(by_address['mcopeland@balseal.com']['name'], 'Mark Copeland')

    def test_multiword_surname_uses_every_surname_part(self):
        by_address = {a['address']: a for a in db.list_known_addresses()}

        self.assertEqual(
            by_address['svandermaat@balseal.com']['name'], 'Sandra van der Maat',
        )

    def test_junk_is_not_offered(self):
        db.insert_message('entry-2', sender_email='not-an-address')

        self.assertNotIn('not-an-address', self._addresses())


class ForwardIsRecordedTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()
        self.message_id = db.insert_message('entry-1', subject='Not a lead')
        self.lead_id = db.insert_lead(
            {'first_name': 'Ada', 'email': 'ada@customer.com'},
            'email_ai_extract', message_id=self.message_id, status='pending_review',
        )
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'

    @patch('dashboard.app.review_marking.maybe_mark_email_reviewed')
    @patch('dashboard.app.outlook_ingest.forward_message',
           return_value=['brost@balseal.com'])
    def test_forwarding_remembers_the_address(self, forward, mark):
        self.client.post(
            f'/leads/{self.lead_id}/forward',
            data={'recipient': 'brost@balseal.com', '_csrf_token': 'token'},
        )

        recorded = db.list_lead_replies(self.lead_id)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]['kind'], 'forward')
        self.assertEqual(recorded[0]['recipients'], ['brost@balseal.com'])
        # ...and it is now suggestible.
        self.assertIn('brost@balseal.com',
                      [a['address'] for a in db.list_known_addresses()])

    def test_the_page_ships_the_list_and_wires_the_inputs(self):
        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            page = self.client.get(f'/leads/{self.lead_id}').get_data(as_text=True)

        self.assertIn('id="known-addresses"', page)
        # Every editable To/Cc line in both actions supports several addresses
        # and receives the same remembered-address dropdown.
        for field in ('recipient', 'cc_recipients', 'extra_recipients',
                      'extra_cc_recipients'):
            tag = next(
                part.split('>', 1)[0]
                for part in page.split('<input ')[1:]
                if f'name="{field}"' in part.split('>', 1)[0]
            )
            self.assertIn('data-address-autocomplete', tag, field)
            self.assertIn('data-address-multiple="1"', tag, field)

        self.assertIn('inputmode="email"', page)
        self.assertIn('if (!Number.isInteger(caret)) caret = value.length;', page)
        self.assertIn("var name = (person.name || '').toLowerCase();", page)
        self.assertIn('a.score - b.score || a.index - b.index', page)
        self.assertIn("nameParts.slice(1).join('') + '@balseal.com'", page)
        self.assertNotIn('address-suggestion-why', page)

    def test_dropdown_data_contains_only_name_and_email(self):
        for contact in db.list_known_addresses():
            self.assertEqual(set(contact), {'address', 'name'})

    def test_review_queue_wires_every_forward_and_reply_address_line(self):
        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            page = self.client.get('/').get_data(as_text=True)

        for field in ('recipient', 'cc_recipients', 'extra_recipients',
                      'extra_cc_recipients'):
            tag = next(
                part.split('>', 1)[0]
                for part in page.split('<input ')[1:]
                if f'name="{field}"' in part.split('>', 1)[0]
            )
            self.assertIn('data-address-autocomplete', tag, field)
            self.assertIn('data-address-multiple="1"', tag, field)


class MigrationTests(unittest.TestCase):
    def test_a_database_without_the_kind_column_gains_it(self):
        """A real database from before forwards were recorded: the column is
        added in place, and the rows already there still read correctly."""
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / 'old.db'
            db.init_db(path)
            conn = db.get_connection(path)
            conn.execute('ALTER TABLE lead_replies DROP COLUMN kind')
            conn.execute(
                "INSERT INTO leads (source, status, created_at, updated_at) "
                "VALUES ('test', 'pending_review', '2026-01-01', '2026-01-01')"
            )
            conn.execute(
                "INSERT INTO lead_replies (lead_id, recipients, body, sent_at) "
                """VALUES (1, '{"to": ["old@example.com"], "cc": []}', 'hi', '2026-01-01')"""
            )
            conn.commit()
            columns = {row[1] for row in conn.execute('PRAGMA table_info(lead_replies)')}
            self.assertNotIn('kind', columns)
            conn.close()

            db.init_db(path)

            conn = db.get_connection(path)
            columns = {row[1] for row in conn.execute('PRAGMA table_info(lead_replies)')}
            self.assertIn('kind', columns)
            # The pre-existing row defaults to a reply and is still suggestible.
            known = db.list_known_addresses(conn=conn)
            conn.close()
            self.assertIn('old@example.com', [a['address'] for a in known])


if __name__ == '__main__':
    unittest.main()
