"""
Moving the pipeline's data to another computer (transfer_data.py).

A clone from GitHub has the code and none of the data, on purpose. What is
pinned here is that an export taken on one computer comes back whole on
another: every lead, and every cached email still reachable even though the
database recorded each one by a full path under the old user's folder.
"""

import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import db
import transfer_data


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        base = Path(self.tempdir.name)
        # Two project folders under different "users", and somewhere outside
        # both for the zip -- the Desktop, in real use.
        self.old = base / 'Users' / 'kkaess' / 'Lead Input pipeline'
        self.new = base / 'Users' / 'jsmith' / 'Lead Input pipeline'
        self.outside = base / 'Desktop'
        for folder in (self.old, self.new, self.outside):
            folder.mkdir(parents=True)

        self.original_db_path = db.DB_PATH
        db.DB_PATH = self.old / 'pipeline.db'
        self.addCleanup(lambda: setattr(db, 'DB_PATH', self.original_db_path))
        db.init_db()

        (self.old / 'email_cache').mkdir()
        (self.old / 'email_messages').mkdir()
        (self.old / 'reference').mkdir()
        body = self.old / 'email_cache' / 'abc123.txt'
        body.write_text('Hello, we need 40mm rotary seals.', encoding='utf-8')
        msg = self.old / 'email_messages' / 'abc123.msg'
        msg.write_bytes(b'fake msg bytes')
        (self.old / 'reference' / 'Sales_Map.png').write_bytes(b'png')
        (self.old / 'reference' / 'README.md').write_text('tracked in git')

        self.message_id = db.insert_message(
            'outlook-1', subject='Quote', body_cache_path=str(body),
            message_file_path=str(msg),
        )
        self.lead_id = db.insert_lead(
            {'first_name': 'Dana', 'organization': 'Reed Instruments'},
            source='email_ai_extract', message_id=self.message_id,
            status='pending_review',
        )
        conn = db.get_connection()
        conn.execute("INSERT INTO run_state (key, value, updated_at) VALUES (?, ?, 'now')",
                     ('job:manual_ingest', json.dumps({'status': 'running', 'pid': 4242})))
        conn.execute("INSERT INTO run_state (key, value, updated_at) VALUES (?, ?, 'now')",
                     ('last_ingest_run_at', '2026-09-28T16:38:43'))
        conn.commit()
        conn.close()

    def _export(self):
        return transfer_data.export_data(self.outside / 'data.zip', root=self.old, log=lambda *_: None)

    def _import(self, **kwargs):
        return transfer_data.import_data(self.outside / 'data.zip', root=self.new,
                                         log=lambda *_: None, **kwargs)

    def test_leads_and_emails_arrive_whole_on_a_computer_with_another_username(self):
        """The point of the tool. A plain copy left every email pointing at
        C:\\Users\\<old user>\\..., which does not exist on the new computer."""
        manifest = self._export()
        self.assertEqual(manifest['counts'], {'leads': 1, 'messages': 1})

        summary = self._import()
        self.assertEqual(summary['counts'], {'leads': 1, 'messages': 1})
        self.assertEqual(summary['paths_rewritten'], 2)
        self.assertEqual(summary['files_missing'], 0)

        conn = sqlite3.connect(self.new / 'pipeline.db')
        body_path, msg_path = conn.execute(
            'SELECT body_cache_path, message_file_path FROM messages').fetchone()
        conn.close()
        self.assertTrue(body_path.startswith(str(self.new)))
        self.assertEqual(Path(body_path).read_text(encoding='utf-8'),
                         'Hello, we need 40mm rotary seals.')
        self.assertEqual(Path(msg_path).read_bytes(), b'fake msg bytes')
        self.assertTrue((self.new / 'reference' / 'Sales_Map.png').exists())

    def test_an_old_entry_id_filename_is_shortened_so_it_fits_windows_path_limit(self):
        """Emails cached before 2026-08 were named after Outlook's ~150
        character entry ID. Under a slightly deeper folder on the new computer
        that passed 260 characters and the import could not write the file.
        It lands under a short hashed name, and the database follows it."""
        long_name = '00000000BF82EDB46A1515488C44278DB226B2D8' * 2 + '.txt'
        legacy = self.old / 'email_cache' / long_name
        legacy.write_text('An old email body.', encoding='utf-8')
        db.update_message_fields(self.message_id, {'translated_body_cache_path': str(legacy)})

        self._export()
        summary = self._import()
        self.assertEqual(summary['files_renamed'], 1)
        self.assertEqual(summary['files_missing'], 0)
        conn = sqlite3.connect(self.new / 'pipeline.db')
        (moved,) = conn.execute('SELECT translated_body_cache_path FROM messages').fetchone()
        conn.close()
        self.assertLess(len(Path(moved).name), 40)
        self.assertEqual(Path(moved).read_text(encoding='utf-8'), 'An old email body.')
        # Reference documents keep their names: the tab shows them.
        self.assertTrue((self.new / 'reference' / 'Sales_Map.png').exists())

    def test_a_folder_too_deep_for_windows_is_refused_before_anything_changes(self):
        self._export()
        # Not created: Windows would refuse, and the check must fire before
        # anything is written anyway.
        deep = self.new / ('d' * 100) / ('e' * 100)
        with self.assertRaisesRegex(transfer_data.TransferError, 'shorter folder'):
            transfer_data.import_data(self.outside / 'data.zip', root=deep, log=lambda *_: None)
        self.assertFalse((deep / 'pipeline.db').exists())

    def test_a_job_running_on_the_old_computer_does_not_block_the_new_one(self):
        """Its pid means nothing here -- or worse, belongs to some other
        process -- and would make the dashboard think an ingest is running."""
        self._export()
        summary = self._import()
        self.assertEqual(summary['leases_cleared'], 1)
        conn = sqlite3.connect(self.new / 'pipeline.db')
        keys = {k for (k,) in conn.execute('SELECT key FROM run_state')}
        conn.close()
        self.assertNotIn('job:manual_ingest', keys)
        self.assertIn('last_ingest_run_at', keys)

    def test_a_write_still_in_the_wal_is_in_the_export(self):
        """pipeline.db is in WAL mode while the dashboard runs; a copy of the
        .db file alone misses whatever has not been checkpointed yet."""
        writer = db.get_connection()
        writer.execute('PRAGMA wal_autocheckpoint = 0')
        db.insert_lead({'first_name': 'Late', 'organization': 'Arrival Co'},
                       source='manual_entry', status='pending_review', conn=writer)
        writer.commit()
        try:
            manifest = self._export()
        finally:
            writer.close()
        self.assertEqual(manifest['counts']['leads'], 2)

    def test_the_export_is_refused_inside_the_project_folder(self):
        """One `git add -A` away from publishing every customer record."""
        with self.assertRaisesRegex(transfer_data.TransferError, 'outside the project folder'):
            transfer_data.export_data(self.old / 'data.zip', root=self.old, log=lambda *_: None)
        self.assertFalse((self.old / 'data.zip').exists())

    def test_existing_leads_are_not_overwritten_without_replace_and_are_kept_with_it(self):
        self._export()
        db.init_db(self.new / 'pipeline.db')
        conn = db.get_connection(self.new / 'pipeline.db')
        db.insert_lead({'first_name': 'Already', 'organization': 'Here Co'},
                       source='manual_entry', status='pending_review', conn=conn)
        conn.commit()
        conn.close()

        with self.assertRaisesRegex(transfer_data.TransferError, '--replace'):
            self._import()

        summary = self._import(replace=True)
        self.assertEqual(summary['counts']['leads'], 1)
        backup = Path(summary['backup'])
        self.assertTrue(backup.exists())
        conn = sqlite3.connect(backup)
        self.assertEqual(conn.execute('SELECT first_name FROM leads').fetchone()[0], 'Already')
        conn.close()

    def test_a_zip_that_would_write_outside_the_data_folders_is_refused(self):
        """A tampered export must not be able to overwrite the code it is
        imported beside."""
        self._export()
        with zipfile.ZipFile(self.outside / 'data.zip', 'a') as archive:
            archive.writestr('../evil.py', 'print("pwned")')
            archive.writestr('dashboard/app.py', 'print("pwned")')
        with self.assertRaisesRegex(transfer_data.TransferError, 'outside the data folders'):
            self._import()
        self.assertFalse((self.new / 'pipeline.db').exists())
        self.assertFalse((self.new.parent / 'evil.py').exists())

    def test_a_zip_that_is_not_an_export_is_refused(self):
        (self.outside / 'data.zip').write_bytes(b'not a zip')
        with self.assertRaisesRegex(transfer_data.TransferError, 'not a pipeline export'):
            self._import()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('notes.txt', 'hi')
        (self.outside / 'data.zip').write_bytes(buffer.getvalue())
        with self.assertRaisesRegex(transfer_data.TransferError, 'not a pipeline export'):
            self._import()

    def test_paths_match_whatever_case_or_slash_windows_stored_them_in(self):
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE messages (body_cache_path TEXT, subject TEXT)')
        conn.executemany('INSERT INTO messages VALUES (?, ?)', [
            (r'C:\Users\KKAESS\Lead Input pipeline\email_cache\a.txt', 'x'),
            ('c:/users/kkaess/lead input pipeline/email_cache/b.txt', 'x'),
            (r'D:\Elsewhere\c.txt', 'x'),
        ])
        rewritten, left = transfer_data._rewrite_paths(
            conn, r'C:\Users\kkaess\Lead Input pipeline', r'C:\Users\jsmith\Pipeline')
        self.assertEqual((rewritten, left), (2, 1))
        paths = [p for (p,) in conn.execute('SELECT body_cache_path FROM messages')]
        self.assertEqual(paths, [r'C:\Users\jsmith\Pipeline\email_cache\a.txt',
                                 r'C:\Users\jsmith\Pipeline\email_cache\b.txt',
                                 r'D:\Elsewhere\c.txt'])

    def test_the_command_line_reports_a_problem_without_a_traceback(self):
        missing = str(self.outside / 'nope.zip')
        self.assertEqual(transfer_data.main(['import', missing]), 1)


if __name__ == '__main__':
    unittest.main()
