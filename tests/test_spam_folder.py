"""Pending Review's Spam folder: Gmail leads, reviewable one by one or
disqualified all at once (which also deletes their emails from Outlook).

See spam_folder.py for why the folder is a view over pending leads rather
than a status of its own.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import outlook_ingest
import spam_folder
from dashboard.app import app


class SpamAddressTests(unittest.TestCase):
    def test_gmail_and_googlemail_addresses_are_spam(self):
        for address in ('joe@gmail.com', 'Joe.Smith@GMAIL.COM ', 'joe@googlemail.com',
                        '<joe@gmail.com>'):
            with self.subTest(address=address):
                self.assertTrue(spam_folder.is_spam_address(address))

    def test_other_addresses_are_not(self):
        for address in ('joe@acme.com', 'joe@gmail.com.acme.com', 'gmail.com',
                        'joe@notgmail.com', '', None):
            with self.subTest(address=address):
                self.assertFalse(spam_folder.is_spam_address(address))

    def test_a_lead_with_no_email_behind_it_is_never_spam(self):
        """A hand-entered lead was put in front of the reviewer on purpose, and
        has no Outlook message for Disqualify all to delete."""
        self.assertFalse(spam_folder.is_spam({'email': 'joe@gmail.com', 'message_id': None}))

    def test_a_gmail_sender_is_spam_even_when_the_contact_address_is_not(self):
        lead = {'email': 'joe@acme.com', 'message_id': 7}
        self.assertTrue(spam_folder.is_spam(lead, sender_email='joe@gmail.com'))
        self.assertFalse(spam_folder.is_spam(lead, sender_email='joe@acme.com'))


class SpamFolderTestCase(unittest.TestCase):
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

        for target in ('dashboard.app.db.get_lead_email_thread',):
            patcher = patch(target, return_value=[])
            patcher.start()
            self.addCleanup(patcher.stop)
        enabled = patch('dashboard.app.outlook_ingest.deletion_enabled', return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)
        marking = patch('dashboard.app.review_marking.maybe_mark_email_reviewed')
        marking.start()
        self.addCleanup(marking.stop)

    def _lead(self, first_name, email, sender=None, message=True, entry_id=None, **fields):
        message_id = None
        if message:
            message_id = db.insert_message(
                entry_id or f'entry-{first_name}', sender_email=sender or email,
                subject=f'Inquiry from {first_name}',
            )
        return db.insert_lead(
            {'first_name': first_name, 'email': email, **fields},
            'email_ai_extract', message_id=message_id, status='pending_review',
        )

    def _page(self, folder=None):
        path = '/?folder=spam' if folder == 'spam' else '/'
        return self.client.get(path).get_data(as_text=True)


class FilingTests(SpamFolderTestCase):
    def test_a_gmail_lead_is_in_spam_and_not_in_the_inbox(self):
        self._lead('Gary', 'gary@gmail.com')
        self._lead('Ingrid', 'ingrid@acme.com')

        inbox, spam = self._page(), self._page('spam')

        self.assertIn('Ingrid', inbox)
        self.assertNotIn('Gary', inbox)
        self.assertIn('Gary', spam)
        self.assertNotIn('Ingrid', spam)

    def test_a_web_form_naming_a_gmail_contact_is_spam(self):
        """A form submission arrives from the website's relay, not from the
        customer; the Gmail address is the one written inside it."""
        self._lead('Gary', 'gary@gmail.com', sender='forms@balseal.com')

        self.assertIn('Gary', self._page('spam'))
        self.assertNotIn('Gary', self._page())

    def test_a_hand_entered_gmail_lead_stays_in_the_inbox(self):
        self._lead('Hana', 'hana@gmail.com', message=False)

        self.assertIn('Hana', self._page())
        self.assertNotIn('Hana', self._page('spam'))

    def test_both_folders_count_toward_pending_review(self):
        self._lead('Gary', 'gary@gmail.com')
        self._lead('Ingrid', 'ingrid@acme.com')

        self.assertIn('Pending Review (<span data-live-count>2</span>)', self._page())

    def test_an_unknown_folder_shows_the_inbox(self):
        self._lead('Ingrid', 'ingrid@acme.com')
        html = self.client.get('/?folder=nonsense').get_data(as_text=True)
        self.assertIn('Ingrid', html)


class ReviewInSpamTests(SpamFolderTestCase):
    def test_a_spam_lead_can_be_approved_like_any_other(self):
        lead_id = self._lead(
            'Gary', 'gary@gmail.com', last_name='Smith', organization='Smith Machining',
            country='USA', state='OH', market='INDUSTRIAL',
        )
        db.set_territory_assignment(lead_id, 'Alicia Sabillon', 'US state: OH')

        response = self.client.post(f'/leads/{lead_id}/approve', data={'_csrf_token': 'token'})

        self.assertEqual(db.get_lead(lead_id)['status'], 'review_approved')
        self.assertTrue(response.location.endswith('/?folder=spam'))

    def test_rejecting_one_spam_lead_does_not_touch_its_email(self):
        lead_id = self._lead('Gary', 'gary@gmail.com')
        with patch('dashboard.app.outlook_ingest.delete_message') as delete_message:
            self.client.post(f'/leads/{lead_id}/reject', data={'_csrf_token': 'token'})

        self.assertEqual(db.get_lead(lead_id)['status'], 'rejected')
        delete_message.assert_not_called()


class DisqualifyAllTests(SpamFolderTestCase):
    def _disqualify(self, lead_ids):
        return self.client.post(
            '/spam/disqualify_all',
            data={'_csrf_token': 'token', 'lead_id': [str(i) for i in lead_ids]},
            headers={'X-Requested-With': 'XMLHttpRequest'},
        ).get_json()

    @patch('dashboard.app.outlook_ingest.delete_message', return_value=True)
    def test_every_spam_lead_is_rejected_and_its_email_deleted(self, delete_message):
        gary = self._lead('Gary', 'gary@gmail.com', entry_id='entry-gary')
        gwen = self._lead('Gwen', 'gwen@gmail.com', entry_id='entry-gwen')
        ingrid = self._lead('Ingrid', 'ingrid@acme.com')

        result = self._disqualify([gary, gwen])

        self.assertTrue(result['ok'])
        self.assertEqual(sorted(result['dropped']), sorted([gary, gwen]))
        self.assertEqual(db.get_lead(gary)['status'], 'rejected')
        self.assertEqual(db.get_lead(gwen)['status'], 'rejected')
        self.assertIn('Spam folder', db.get_lead(gary)['review_notes'])
        self.assertEqual(db.get_lead(ingrid)['status'], 'pending_review')
        self.assertEqual(
            sorted(c.args[0] for c in delete_message.call_args_list),
            ['entry-gary', 'entry-gwen'],
        )
        self.assertIn('deleted 2 emails', result['message'])

    @patch('dashboard.app.outlook_ingest.delete_message', return_value=True)
    def test_an_inbox_lead_posted_by_mistake_is_left_alone(self, delete_message):
        """The route trusts the folder, not the form: only leads that are in
        Spam right now can be disqualified from it."""
        ingrid = self._lead('Ingrid', 'ingrid@acme.com')
        gary = self._lead('Gary', 'gary@gmail.com')

        self._disqualify([ingrid, gary])

        self.assertEqual(db.get_lead(ingrid)['status'], 'pending_review')
        self.assertEqual(db.get_lead(gary)['status'], 'rejected')

    @patch('dashboard.app.outlook_ingest.delete_message', return_value=True)
    def test_a_gmail_lead_that_arrived_after_the_page_loaded_is_not_swept_up(self, delete_message):
        seen = self._lead('Gary', 'gary@gmail.com')
        unseen = self._lead('Gwen', 'gwen@gmail.com')

        self._disqualify([seen])

        self.assertEqual(db.get_lead(seen)['status'], 'rejected')
        self.assertEqual(db.get_lead(unseen)['status'], 'pending_review')
        delete_message.assert_called_once_with('entry-Gary')

    @patch('dashboard.app.outlook_ingest.delete_message', return_value=True)
    def test_an_email_another_reviewable_lead_came_from_is_kept(self, delete_message):
        """One email can produce several leads. Deleting it would leave its
        non-Gmail lead with nothing to be checked against."""
        message_id = db.insert_message('entry-shared', sender_email='list@acme.com')
        gary = db.insert_lead({'first_name': 'Gary', 'email': 'gary@gmail.com'},
                              'email_excel_attachment', message_id=message_id,
                              status='pending_review')
        ingrid = db.insert_lead({'first_name': 'Ingrid', 'email': 'ingrid@acme.com'},
                                'email_excel_attachment', message_id=message_id,
                                status='pending_review')

        result = self._disqualify([gary])

        self.assertEqual(db.get_lead(gary)['status'], 'rejected')
        self.assertEqual(db.get_lead(ingrid)['status'], 'pending_review')
        delete_message.assert_not_called()
        self.assertIn('left in the inbox', result['message'])

    def test_an_outlook_failure_stops_the_run_and_leaves_the_rest_in_spam(self):
        gary = self._lead('Gary', 'gary@gmail.com')
        gwen = self._lead('Gwen', 'gwen@gmail.com')
        failure = outlook_ingest.OutlookDeleteError('deleting the email', RuntimeError('denied'))

        with patch('dashboard.app.outlook_ingest.delete_message',
                   side_effect=failure) as delete_message:
            result = self._disqualify([gary, gwen])

        self.assertFalse(result['ok'])
        self.assertIn('deleting the email', result['error'])
        self.assertEqual(delete_message.call_count, 1)
        self.assertEqual(db.get_lead(gary)['status'], 'pending_review')
        self.assertEqual(db.get_lead(gwen)['status'], 'pending_review')

    def test_nothing_is_disqualified_when_deletion_is_switched_off(self):
        gary = self._lead('Gary', 'gary@gmail.com')

        with patch('dashboard.app.outlook_ingest.deletion_enabled', return_value=False), \
                patch('dashboard.app.outlook_ingest.delete_message') as delete_message:
            result = self._disqualify([gary])

        self.assertFalse(result['ok'])
        delete_message.assert_not_called()
        self.assertEqual(db.get_lead(gary)['status'], 'pending_review')

    def test_the_button_is_offered_only_in_spam(self):
        self._lead('Gary', 'gary@gmail.com')
        self._lead('Ingrid', 'ingrid@acme.com')

        self.assertIn('open-disqualify-dialog', self._page('spam'))
        self.assertNotIn('open-disqualify-dialog', self._page())


if __name__ == '__main__':
    unittest.main()
