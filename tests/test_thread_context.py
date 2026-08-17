"""
A reply arrives on its own; the messages it answers were handled earlier and
tagged green, so the pipeline skipped them and they are nowhere in the
database. Without them a reviewer sees a bare "RE: ..." and cannot judge
whether to forward it. These tests cover keeping that context and, just as
importantly, not letting it duplicate a real message.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import db
import outlook_ingest


def _conversation_item(entry_id, sender, received_at, subject, body='', item_class=43):
    item = MagicMock()
    item.EntryID = entry_id
    item.Class = item_class
    item.SenderName = sender.split('@')[0]
    item.SenderEmailType = 'SMTP'
    item.SenderEmailAddress = sender
    item.ReceivedTime = received_at
    item.Subject = subject
    item.Body = body
    item.ConversationID = 'conv-1'
    return item


class ConversationWalkTests(unittest.TestCase):
    def _wire(self, connect, items, children_of=None):
        conversation = connect.return_value.GetItemFromID.return_value.GetConversation.return_value
        roots = MagicMock()
        roots.Count = 1
        roots.Item.side_effect = lambda i: items[0]
        conversation.GetRootItems.return_value = roots

        children_of = children_of or {}

        def children(node):
            kids = children_of.get(node.EntryID, [])
            collection = MagicMock()
            collection.Count = len(kids)
            collection.Item.side_effect = lambda i: kids[i - 1]
            return collection

        conversation.GetChildren.side_effect = children
        return conversation

    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_whole_thread_is_returned_oldest_first(self, connect, get_inbox, co_init):
        original = _conversation_item('id-1', 'buyer@example.com', '2026-08-07T08:01:00', 'HOT RFQ')
        follow_up = _conversation_item('id-2', 'buyer@example.com', '2026-08-07T11:10:00', 'RE: HOT RFQ')
        reply = _conversation_item('id-3', 'rep@balseal.com', '2026-08-07T12:04:00', 'RE: HOT RFQ')
        self._wire(connect, [original], {'id-1': [reply, follow_up]})

        thread = outlook_ingest.conversation_messages('id-3')

        self.assertEqual([m['subject'] for m in thread],
                         ['HOT RFQ', 'RE: HOT RFQ', 'RE: HOT RFQ'])
        self.assertEqual([m['received_at'][11:16] for m in thread],
                         ['08:01', '11:10', '12:04'])

    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._get_sales_inbox')
    @patch('outlook_ingest._connect')
    def test_non_mail_items_are_left_out(self, connect, get_inbox, co_init):
        original = _conversation_item('id-1', 'buyer@example.com', '2026-08-07T08:01:00', 'HOT RFQ')
        meeting = _conversation_item('id-2', 'buyer@example.com', '2026-08-07T09:00:00',
                                     'Invite', item_class=53)
        self._wire(connect, [original], {'id-1': [meeting]})

        thread = outlook_ingest.conversation_messages('id-1')

        self.assertEqual([m['subject'] for m in thread], ['HOT RFQ'])

    @patch('outlook_ingest.pythoncom.CoInitialize')
    @patch('outlook_ingest._connect', side_effect=RuntimeError('Outlook is shut'))
    def test_an_unreadable_conversation_is_not_fatal(self, connect, co_init):
        # Context is a nicety; losing it must not cost the run its lead.
        self.assertEqual(outlook_ingest.conversation_messages('id-1'), [])


class ThreadContextStorageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()

        self.message_id = db.insert_message(
            'entry-reply', subject='RE: HOT RFQ', sender_email='rep@balseal.com',
            received_at='2026-08-07T12:04:00', conversation_id='conv-1',
        )
        self.lead_id = db.insert_lead(
            {'first_name': 'Terri', 'email': 'buyer@example.com'},
            'email_ai_extract', message_id=self.message_id, status='pending_review',
        )

    def _store_context(self, source_id, sender, received_at, subject):
        return db.record_thread_message(
            source_id, conversation_id='conv-1', received_at=received_at,
            sender_email=sender, sender_name=sender.split('@')[0], subject=subject,
            body_text='...',
        )

    def test_earlier_messages_appear_in_the_thread_in_order(self):
        self._store_context('ctx-1', 'buyer@example.com', '2026-08-07T08:01:00', 'HOT RFQ')
        self._store_context('ctx-2', 'buyer@example.com', '2026-08-07T11:10:00', 'RE: HOT RFQ')

        thread = db.get_lead_email_thread(self.lead_id)

        self.assertEqual([m['subject'] for m in thread],
                         ['HOT RFQ', 'RE: HOT RFQ', 'RE: HOT RFQ'])
        self.assertEqual([m['is_context'] for m in thread], [True, True, False])
        # The oldest message heads the thread, whether or not it is the lead's.
        self.assertTrue(thread[0]['is_original'])

    def test_context_never_duplicates_an_ingested_message(self):
        # Outlook reports a different EntryID for an item reached through the
        # conversation, so the ingested message reappears under a new id.
        self._store_context('different-id', 'rep@balseal.com', '2026-08-07T12:04:00',
                            'RE: HOT RFQ')

        thread = db.get_lead_email_thread(self.lead_id)

        self.assertEqual(len(thread), 1)
        self.assertFalse(thread[0]['is_context'])

    def test_a_real_message_is_never_stored_as_context(self):
        self.assertIsNone(
            self._store_context('entry-reply', 'rep@balseal.com',
                                '2026-08-07T12:04:00', 'RE: HOT RFQ')
        )

    def test_context_is_not_a_lead_source(self):
        self._store_context('ctx-1', 'buyer@example.com', '2026-08-07T08:01:00', 'HOT RFQ')

        # It must never be picked up for ingestion, retry or submission.
        self.assertIsNone(db.get_message_by_source_id('ctx-1'))
        self.assertEqual(len(db.list_leads_by_message(self.message_id)), 1)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / 'test.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()

    @patch('pipeline_ingest.outlook_ingest.conversation_messages')
    def test_the_message_being_ingested_is_not_stored_as_its_own_context(self, siblings):
        import pipeline_ingest

        msg = {
            'id': 'entry-reply', 'subject': 'RE: HOT RFQ',
            'received_at': '2026-08-07T12:04:00', 'sender_email': 'rep@balseal.com',
            'conversation_id': 'conv-1',
        }
        siblings.return_value = [
            {'id': 'ctx-1', 'conversation_id': 'conv-1', 'received_at': '2026-08-07T08:01:00',
             'sender_email': 'buyer@example.com', 'sender_name': 'Terri',
             'subject': 'HOT RFQ', 'body_text': 'Please quote.'},
            # Same message, different EntryID -- how Outlook returns it.
            {'id': 'other-id', 'conversation_id': 'conv-1', 'received_at': '2026-08-07T12:04:00',
             'sender_email': 'rep@balseal.com', 'sender_name': 'Bill',
             'subject': 'RE: HOT RFQ', 'body_text': 'Process question.'},
        ]

        stored = pipeline_ingest._capture_thread_context(msg)

        self.assertEqual(stored, 1)
        self.assertEqual(
            [m['source_message_id'] for m in db.list_thread_messages('conv-1')], ['ctx-1'],
        )


if __name__ == '__main__':
    unittest.main()
