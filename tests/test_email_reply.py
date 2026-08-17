import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import db
import outlook_ingest
from dashboard.app import app


class FakeRecipients:
    """A stand-in for Outlook's Recipients collection that actually mutates.

    A plain MagicMock cannot model this: reply_message() clears the collection
    with `while Count > 0: Remove(1)`, and against a mock whose Count never
    changes that loop never ends. It is also the only way to check that
    removing shifts the collection down, which is why the code always removes
    index 1.
    """

    def __init__(self, addresses=()):
        self._addresses = list(addresses)
        self.added = []
        self.removed_indexes = []
        self.types = {}          # address -> olTo(1)/olCC(2), when set
        self.resolve_all_result = True

    @property
    def Count(self):
        return len(self._addresses)

    def Item(self, index):
        recipient = MagicMock()
        recipient.AddressEntry.GetExchangeUser.return_value = None
        recipient.Address = self._addresses[index - 1]
        return recipient

    def Add(self, address):
        self.added.append(address)
        self._addresses.append(address)
        collection = self

        class _Added:
            """Outlook's Recipients.Add() returns the Recipient it created, and
            setting .Type on it is what moves someone to the Cc line."""

            @property
            def Type(self):
                return collection.types.get(address)

            @Type.setter
            def Type(self, value):
                collection.types[address] = value

        return _Added()

    def Remove(self, index):
        self.removed_indexes.append(index)
        del self._addresses[index - 1]

    def ResolveAll(self):
        return self.resolve_all_result


def _resolvable_reply(connect, reply_all=False, addresses=('ada@example.com',)):
    """Wire a mocked Outlook item whose Reply()/ReplyAll() resolves cleanly."""
    item = connect.return_value.GetItemFromID.return_value
    reply = item.ReplyAll.return_value if reply_all else item.Reply.return_value
    reply.Recipients = FakeRecipients(addresses)
    return reply


class OutlookReplyTests(unittest.TestCase):
    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_replies_to_sender_from_the_shared_mailbox(
        self, connect, get_inbox, co_initialize, co_uninitialize,
    ):
        reply = _resolvable_reply(connect)

        recipients = outlook_ingest.reply_message('entry-id', 'Which size seal?')

        self.assertEqual(recipients, ['ada@example.com'])
        self.assertEqual(reply.SentOnBehalfOfName, 'sales@balseal.com')
        reply.Send.assert_called_once_with()
        connect.return_value.GetItemFromID.return_value.ReplyAll.assert_not_called()
        co_initialize.assert_called_once_with()
        co_uninitialize.assert_called_once_with()

    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_reply_all_uses_reply_all(self, connect, get_inbox):
        reply = _resolvable_reply(
            connect, reply_all=True,
            addresses=('ada@example.com', 'distributor@example.com'),
        )

        recipients = outlook_ingest.reply_message(
            'entry-id', 'Copying you both.', reply_all=True,
        )

        self.assertEqual(recipients, ['ada@example.com', 'distributor@example.com'])
        connect.return_value.GetItemFromID.return_value.Reply.assert_not_called()
        reply.Send.assert_called_once_with()

    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_body_is_prepended_above_the_quoted_original(self, connect, get_inbox):
        reply = _resolvable_reply(connect)
        reply.BodyFormat = 2
        reply.HTMLBody = '<p>-----Original Message-----</p>'

        outlook_ingest.reply_message('entry-id', 'Thanks for getting in touch.')

        self.assertTrue(reply.HTMLBody.endswith('<p>-----Original Message-----</p>'))
        self.assertIn('Thanks for getting in touch.', reply.HTMLBody)

    @patch('outlook_ingest._connect')
    def test_empty_body_never_reaches_outlook(self, connect):
        with self.assertRaises(ValueError):
            outlook_ingest.reply_message('entry-id', '   ')
        connect.assert_not_called()

    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_unresolved_recipients_are_not_sent(self, connect, get_inbox):
        reply = connect.return_value.GetItemFromID.return_value.Reply.return_value
        reply.Recipients.ResolveAll.return_value = False

        with self.assertRaises(ValueError):
            outlook_ingest.reply_message('entry-id', 'Hello?')
        reply.Send.assert_not_called()
        reply.Close.assert_called_once_with(1)

    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_a_sender_with_no_reply_address_is_reported(self, connect, get_inbox):
        reply = connect.return_value.GetItemFromID.return_value.Reply.return_value
        reply.Recipients.ResolveAll.return_value = True
        reply.Recipients.Count = 0

        with self.assertRaises(ValueError):
            outlook_ingest.reply_message('entry-id', 'Hello?')
        reply.Send.assert_not_called()

    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_missing_outlook_item_falls_back_to_preserved_msg(self, connect, get_inbox):
        with tempfile.NamedTemporaryFile(suffix='.msg') as saved_message:
            namespace = connect.return_value
            namespace.GetItemFromID.side_effect = RuntimeError('MAPI item not found')
            preserved = namespace.OpenSharedItem.return_value
            reply = preserved.Reply.return_value
            reply.Recipients.ResolveAll.return_value = True
            reply.Recipients.Count = 1

            outlook_ingest.reply_message(
                'stale-entry-id', 'Following up.',
                fallback_message_path=saved_message.name,
            )

        namespace.OpenSharedItem.assert_called_once_with(saved_message.name)
        reply.Send.assert_called_once_with()
        preserved.Close.assert_called_once_with(1)

    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_send_error_reports_stage_and_discards_draft(self, connect, get_inbox):
        reply = _resolvable_reply(connect)
        reply.Send.side_effect = RuntimeError('send denied')

        with self.assertRaises(outlook_ingest.OutlookReplyError) as raised:
            outlook_ingest.reply_message('entry-id', 'Hello.')

        self.assertEqual(raised.exception.stage, 'sending the reply')
        reply.Close.assert_called_once_with(1)


def _outlook_recipient(name, address, kind):
    recipient = MagicMock()
    recipient.Name = name
    recipient.Address = address
    recipient.Type = kind
    recipient.AddressEntry.GetExchangeUser.return_value = None
    return recipient


def _item_with_recipients(*recipients):
    item = MagicMock()
    item.Recipients.Count = len(recipients)
    item.Recipients.Item.side_effect = lambda index: recipients[index - 1]
    return item


class MessageRecipientTests(unittest.TestCase):
    def test_to_and_cc_are_separated_and_bcc_ignored(self):
        item = _item_with_recipients(
            _outlook_recipient('Sales', 'sales@balseal.com', 1),
            _outlook_recipient('Ada Lovelace', 'ada@example.com', 1),
            _outlook_recipient('Distributor', 'dist@example.com', 2),
            _outlook_recipient('Hidden', 'hidden@example.com', 3),
        )

        recipients = outlook_ingest.message_recipient_list(item)

        self.assertEqual(
            [r['address'] for r in recipients['to']],
            ['sales@balseal.com', 'ada@example.com'],
        )
        self.assertEqual([r['address'] for r in recipients['cc']], ['dist@example.com'])

    def test_exchange_recipients_resolve_to_smtp(self):
        recipient = _outlook_recipient('Lana', '/O=EXCHANGE/CN=LANA', 1)
        recipient.AddressEntry.GetExchangeUser.return_value = MagicMock(
            PrimarySmtpAddress='lana@example.com',
        )

        recipients = outlook_ingest.message_recipient_list(_item_with_recipients(recipient))

        self.assertEqual(recipients['to'][0]['address'], 'lana@example.com')

    def test_unreadable_recipients_do_not_raise(self):
        item = MagicMock()
        item.Recipients.Count = 1
        item.Recipients.Item.side_effect = AttributeError('no recipients')

        self.assertEqual(
            outlook_ingest.message_recipient_list(item), {'to': [], 'cc': []},
        )

    def test_never_recorded_is_not_the_same_as_nobody(self):
        # A message ingested before To/CC was captured must not read as
        # "the customer emailed us alone".
        self.assertIsNone(db.parse_message_recipients(None))
        self.assertEqual(
            db.parse_message_recipients('{"to": [], "cc": []}'), {'to': [], 'cc': []},
        )
        self.assertIsNone(db.parse_message_recipients('not json'))


class ReplyRecipientRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        db.init_db()
        self.message_id = db.insert_message('outlook-entry-id', subject='Seal enquiry')
        self.lead_id = db.insert_lead(
            {'first_name': 'Ada', 'email': 'ada@example.com'},
            'email_ai_extract', message_id=self.message_id, status='pending_review',
        )
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def _post(self):
        return self.client.post(
            f'/leads/{self.lead_id}/recipients', headers={'X-CSRF-Token': 'token'},
        )

    @patch('dashboard.app.outlook_ingest.message_recipients')
    def test_stored_recipients_do_not_hit_outlook(self, message_recipients):
        db.set_message_recipients(self.message_id, {
            'to': [{'name': 'Sales', 'address': 'sales@balseal.com'},
                   {'name': 'Ada Lovelace', 'address': 'ada@example.com'}],
            'cc': [{'name': '', 'address': 'dist@example.com'}],
        })

        payload = self._post().get_json()

        message_recipients.assert_not_called()
        self.assertTrue(payload['known'])
        self.assertEqual(payload['count'], 2)
        # Our own mailbox is on every inbound lead, so it is not "who else".
        self.assertNotIn('sales@balseal.com', payload['label'])
        self.assertIn('Ada Lovelace <ada@example.com>', payload['label'])
        self.assertIn('CC: dist@example.com', payload['label'])

    @patch('dashboard.app.outlook_ingest.message_recipients')
    def test_unrecorded_recipients_are_read_once_and_stored(self, message_recipients):
        message_recipients.return_value = {
            'to': [{'name': 'Ada', 'address': 'ada@example.com'}], 'cc': [],
        }

        first = self._post().get_json()
        second = self._post().get_json()

        self.assertTrue(first['known'])
        self.assertEqual(first['count'], 1)
        # Read from Outlook once; the answer is recorded for next time.
        message_recipients.assert_called_once_with(
            'outlook-entry-id', fallback_message_path=None,
        )
        self.assertEqual(second, first)
        self.assertEqual(
            db.get_message_recipients(self.message_id)['to'][0]['address'],
            'ada@example.com',
        )

    @patch('dashboard.app.outlook_ingest.message_recipients',
           side_effect=RuntimeError('Outlook is closed'))
    def test_outlook_failure_reports_unknown_rather_than_nobody(self, message_recipients):
        payload = self._post().get_json()

        self.assertFalse(payload['known'])
        self.assertIn('Outlook could not be reached', payload['error'])
        self.assertIsNone(db.get_message_recipients(self.message_id))

    def test_recipients_lookup_requires_the_csrf_token(self):
        response = self.client.post(f'/leads/{self.lead_id}/recipients')

        self.assertEqual(response.status_code, 400)

    def test_empty_recipient_list_reads_as_nobody_else(self):
        db.set_message_recipients(self.message_id, {'to': [], 'cc': []})

        payload = self._post().get_json()

        self.assertTrue(payload['known'])
        self.assertEqual(payload['count'], 0)
        self.assertEqual(payload['label'], '')


class ExplicitRecipientTests(unittest.TestCase):
    """An explicit list replaces whoever Outlook would have addressed."""

    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_outlooks_own_recipients_are_cleared_first(self, connect, get_inbox, *_):
        # Outlook pre-addressed the customer and a colleague; the operator
        # chose to answer only the colleague.
        reply = _resolvable_reply(
            connect, addresses=('ada@example.com', 'bill@balseal.com'),
        )

        sent_to = outlook_ingest.reply_message(
            'entry-id', 'Answering you only.', to_addresses=['bill@balseal.com'],
        )

        self.assertEqual(reply.Recipients.added, ['bill@balseal.com'])
        # Always index 1: removing shifts the collection down.
        self.assertEqual(reply.Recipients.removed_indexes, [1, 1])
        self.assertEqual(sent_to, ['bill@balseal.com'])
        self.assertNotIn('ada@example.com', sent_to)
        reply.Send.assert_called_once_with()

    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_a_copied_recipient_is_added_to_the_cc_line(self, connect, get_inbox, *_):
        reply = _resolvable_reply(connect, addresses=('ada@example.com',))

        outlook_ingest.reply_message(
            'entry-id', 'Answering you, copying her.',
            to_addresses=['bill@balseal.com'], cc_addresses=['ada@example.com'],
        )

        self.assertEqual(reply.Recipients.added, ['bill@balseal.com', 'ada@example.com'])
        # Add() puts everyone on the To line; only Type makes it a copy.
        self.assertEqual(reply.Recipients.types, {'ada@example.com': 2})
        reply.Send.assert_called_once_with()

    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_an_explicit_list_beats_reply_all(self, connect, get_inbox, *_):
        item = connect.return_value.GetItemFromID.return_value
        reply = _resolvable_reply(connect, addresses=('ada@example.com',))

        outlook_ingest.reply_message(
            'entry-id', 'Just you.', reply_all=True, to_addresses=['bill@balseal.com'],
        )

        # Reply(), not ReplyAll(): the caller already said who this goes to.
        item.ReplyAll.assert_not_called()
        self.assertEqual(reply.Recipients.added, ['bill@balseal.com'])

    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    @patch('outlook_ingest._find_thread_item')
    def test_a_thread_target_is_the_message_replied_to(self, find, connect, get_inbox, *_):
        target = MagicMock()
        target.Reply.return_value.Recipients = FakeRecipients(['bill@balseal.com'])
        find.return_value = target

        outlook_ingest.reply_message(
            'entry-id', 'Answering your question.',
            to_addresses=['bill@balseal.com'],
            thread_target={'received_at': '2026-08-07T12:04:00',
                           'sender_email': 'bill@balseal.com'},
        )

        # Quoted from that message, not from whichever one the lead came in on.
        target.Reply.assert_called_once_with()
        connect.return_value.GetItemFromID.return_value.Reply.assert_not_called()

    @patch('outlook_ingest.pythoncom.CoUninitialize')
    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    @patch('outlook_ingest._find_thread_item', return_value=None)
    def test_an_unfindable_thread_message_falls_back_to_the_ingested_one(
        self, find, connect, get_inbox, *_,
    ):
        reply = _resolvable_reply(connect, addresses=('ada@example.com',))

        outlook_ingest.reply_message(
            'entry-id', 'Still reaches you.', to_addresses=['bill@balseal.com'],
            thread_target={'received_at': '2026-08-07T12:04:00', 'sender_email': 'x@y.com'},
        )

        # The reply still goes to the chosen person rather than failing.
        reply.Send.assert_called_once_with()
        self.assertEqual(reply.Recipients.added, ['bill@balseal.com'])


class ReplyRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        db.init_db()
        self.message_id = db.insert_message(
            'outlook-entry-id', subject='Seal enquiry', sender_email='ada@example.com',
        )
        self.lead_id = db.insert_lead(
            {'first_name': 'Ada', 'email': 'ada@example.com'},
            'email_ai_extract', message_id=self.message_id, status='pending_review',
        )
        app.config.update(TESTING=True, SECRET_KEY='test-secret')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_csrf_token'] = 'token'

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def _post_reply(self, **fields):
        data = {
            'reply_body': 'Which shaft diameter?', 'recipients': ['ada@example.com'],
            '_csrf_token': 'token',
        }
        data.update(fields)
        return self.client.post(f'/leads/{self.lead_id}/reply', data=data)

    def _post_reply_from_the_dialog(self, **fields):
        """The review card's reply dialog: a fetch that stays on the page."""
        data = {
            'reply_body': 'Which shaft diameter?', 'recipients': ['ada@example.com'],
            '_csrf_token': 'token',
        }
        data.update(fields)
        return self.client.post(
            f'/leads/{self.lead_id}/reply', data=data,
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )

    @patch('dashboard.app.outlook_ingest.reply_message', return_value=['ada@example.com'])
    def test_dialog_reply_answers_without_navigating_and_keeps_the_card(self, reply_message):
        response = self._post_reply_from_the_dialog()

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['message'], 'Reply sent to ada@example.com.')
        # Nothing dropped: replying is part of working the lead, so its card
        # stays in the queue waiting for the answer.
        self.assertEqual(payload['dropped'], [])
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'pending_review')

    @patch('dashboard.app.outlook_ingest.reply_message')
    def test_dialog_reply_with_an_empty_body_is_reported_not_sent(self, reply_message):
        payload = self._post_reply_from_the_dialog(reply_body='  ').get_json()

        self.assertFalse(payload['ok'])
        self.assertEqual(payload['error'], 'Write a message before sending the reply.')
        reply_message.assert_not_called()

    @patch('dashboard.app.outlook_ingest.reply_message', return_value=['ada@example.com'])
    def test_successful_reply_keeps_the_lead_pending_and_is_recorded(self, reply_message):
        response = self._post_reply()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith(f'/leads/{self.lead_id}?replied=1'))
        reply_message.assert_called_once_with(
            'outlook-entry-id', 'Which shaft diameter?',
            to_addresses=['ada@example.com'], cc_addresses=[], thread_target=None,
            fallback_message_path=None,
        )
        # A reply is part of working the lead, not a disposal of it.
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'pending_review')

        replies = db.list_lead_replies(self.lead_id)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]['recipients'], ['ada@example.com'])
        self.assertEqual(replies[0]['body'], 'Which shaft diameter?')
        self.assertEqual(replies[0]['reply_all'], 0)

    @patch('dashboard.app.outlook_ingest.reply_message', return_value=['bill@balseal.com'])
    def test_reply_goes_only_to_the_people_ticked(self, reply_message):
        # The point of the tick-list: answering a colleague's follow-up must
        # not copy in the customer whose original they replied to.
        self._post_reply(recipients=['bill@balseal.com'])

        self.assertEqual(
            reply_message.call_args.kwargs['to_addresses'], ['bill@balseal.com'],
        )
        self.assertNotIn('ada@example.com', reply_message.call_args.kwargs['to_addresses'])
        self.assertEqual(db.list_lead_replies(self.lead_id)[0]['reply_all'], 0)

    @patch('dashboard.app.outlook_ingest.reply_message', return_value=['bill@balseal.com'])
    def test_someone_can_be_copied_instead_of_addressed(self, reply_message):
        self._post_reply(
            recipients=['bill@balseal.com'], cc_recipients=['ada@example.com'],
        )

        self.assertEqual(reply_message.call_args.kwargs['to_addresses'],
                         ['bill@balseal.com'])
        self.assertEqual(reply_message.call_args.kwargs['cc_addresses'],
                         ['ada@example.com'])

        reply = db.list_lead_replies(self.lead_id)[0]
        self.assertEqual(reply['recipients'], ['bill@balseal.com'])
        self.assertEqual(reply['cc_recipients'], ['ada@example.com'])

    @patch('dashboard.app.outlook_ingest.reply_message', return_value=['bill@balseal.com'])
    def test_a_typed_in_address_can_be_copied(self, reply_message):
        self._post_reply(
            recipients=['bill@balseal.com'], extra_cc_recipients='boss@balseal.com',
        )

        self.assertEqual(reply_message.call_args.kwargs['cc_addresses'],
                         ['boss@balseal.com'])

    @patch('dashboard.app.outlook_ingest.reply_message')
    def test_a_reply_with_only_a_copy_is_refused(self, reply_message):
        response = self._post_reply(recipients=[], cc_recipients=['ada@example.com'])

        reply_message.assert_not_called()
        self.assertIn(b'not only a copy', self.client.get(response.location).data)

    @patch('dashboard.app.outlook_ingest.reply_message', return_value=['bill@balseal.com'])
    def test_the_to_line_wins_when_someone_is_in_both(self, reply_message):
        self._post_reply(
            recipients=['bill@balseal.com'], cc_recipients=['bill@balseal.com'],
        )

        self.assertEqual(reply_message.call_args.kwargs['to_addresses'],
                         ['bill@balseal.com'])
        self.assertEqual(reply_message.call_args.kwargs['cc_addresses'], [])

    @patch('dashboard.app.outlook_ingest.reply_message', return_value=['x@y.com'])
    def test_several_typed_addresses_can_be_separated_any_sensible_way(self, reply_message):
        # What someone actually types: comma-and-space is the common one, but
        # a bare comma, a semicolon or a space all have to work -- the field
        # says "Add someone else" and gives no format.
        for typed in ('kkaess@balseal.com, kpkaess@gmail.com',
                      'kkaess@balseal.com,kpkaess@gmail.com',
                      'kkaess@balseal.com; kpkaess@gmail.com',
                      'kkaess@balseal.com kpkaess@gmail.com'):
            with self.subTest(typed=typed):
                reply_message.reset_mock()
                self._post_reply(recipients=[], extra_recipients=typed)
                self.assertEqual(
                    reply_message.call_args.kwargs['to_addresses'],
                    ['kkaess@balseal.com', 'kpkaess@gmail.com'], typed,
                )

    @patch('dashboard.app.outlook_ingest.reply_message', return_value=['a@x.com', 'b@x.com'])
    def test_typed_in_recipients_are_added_and_deduplicated(self, reply_message):
        self._post_reply(
            recipients=['ada@example.com'],
            extra_recipients='bob@example.com, ADA@example.com; carol@example.com',
        )

        self.assertEqual(
            reply_message.call_args.kwargs['to_addresses'],
            ['ada@example.com', 'bob@example.com', 'carol@example.com'],
        )
        self.assertEqual(db.list_lead_replies(self.lead_id)[0]['reply_all'], 1)

    @patch('dashboard.app.outlook_ingest.reply_message')
    def test_a_reply_with_nobody_selected_is_refused(self, reply_message):
        response = self._post_reply(recipients=[], extra_recipients='')

        reply_message.assert_not_called()
        self.assertIn(b'at least one person', self.client.get(response.location).data)

    @patch('dashboard.app.outlook_ingest.reply_message')
    def test_a_malformed_typed_address_is_refused(self, reply_message):
        response = self._post_reply(extra_recipients='not-an-email')

        reply_message.assert_not_called()
        self.assertIn(b'not a valid email address', self.client.get(response.location).data)

    @patch('dashboard.app.outlook_ingest.reply_message', return_value=['bill@balseal.com'])
    def test_replying_to_one_message_of_a_thread_targets_that_message(self, reply_message):
        self._post_reply(
            recipients=['bill@balseal.com'],
            thread_received_at='2026-08-07T12:04:00',
            thread_sender='bill@balseal.com',
        )

        self.assertEqual(reply_message.call_args.kwargs['thread_target'], {
            'received_at': '2026-08-07T12:04:00', 'sender_email': 'bill@balseal.com',
        })

    @patch('dashboard.app.outlook_ingest.reply_message')
    def test_empty_body_is_not_sent(self, reply_message):
        self._post_reply(reply_body='   ')

        reply_message.assert_not_called()
        self.assertEqual(db.list_lead_replies(self.lead_id), [])

    @patch('dashboard.app.outlook_ingest.reply_message',
           side_effect=outlook_ingest.OutlookReplyError('sending the reply', RuntimeError('x')))
    def test_send_failure_records_nothing(self, reply_message):
        response = self._post_reply()

        self.assertEqual(db.list_lead_replies(self.lead_id), [])
        self.assertEqual(db.get_lead(self.lead_id)['status'], 'pending_review')
        self.assertIn(b'was not sent', self.client.get(response.location).data)

    @patch('dashboard.app.outlook_ingest.reply_message')
    def test_already_decided_lead_cannot_be_replied_to(self, reply_message):
        db.reject_lead(self.lead_id, reviewed_by='dashboard')

        self.assertEqual(self._post_reply().status_code, 409)
        reply_message.assert_not_called()

    def test_pending_card_and_detail_offer_reply_button(self):
        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            index_response = self.client.get('/')
            detail_response = self.client.get(f'/leads/{self.lead_id}')

        self.assertIn(b'open-reply-dialog', index_response.data)
        self.assertIn(b'open-reply-dialog', detail_response.data)
        self.assertIn(f'/leads/{self.lead_id}/reply'.encode(), index_response.data)

    @patch('dashboard.app.outlook_ingest.reply_message', return_value=['ada@example.com'])
    def test_sent_reply_is_shown_on_the_lead(self, reply_message):
        self._post_reply()

        with patch('dashboard.app.db.get_lead_email_thread', return_value=[]):
            detail = self.client.get(f'/leads/{self.lead_id}')
            index = self.client.get('/')

        self.assertIn(b'Which shaft diameter?', detail.data)
        self.assertIn(b'Which shaft diameter?', index.data)
        self.assertIn(b'ada@example.com', detail.data)


if __name__ == '__main__':
    unittest.main()
