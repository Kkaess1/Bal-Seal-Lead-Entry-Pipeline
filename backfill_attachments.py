"""
Backfill attachment metadata for existing messages in pipeline.db using their
saved .msg email files.

Usage: python backfill_attachments.py
"""

import os
import glob
import json
import logging
import sqlite3
from pathlib import Path
import win32com.client as win32

import db
import outlook_ingest

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-7s  %(message)s')
logger = logging.getLogger(__name__)

def backfill_attachments(db_path='pipeline.db'):
    conn = db.get_connection(db_path)
    try:
        rows = conn.execute("SELECT id, source_message_id, subject, message_file_path, attachments FROM messages").fetchall()
        logger.info(f"Checking {len(rows)} messages in {db_path}...")
        
        need_backfill = rows
        logger.info(f"Re-evaluating attachments for {len(need_backfill)} messages.")

        outlook = win32.Dispatch('Outlook.Application')
        ns = outlook.GetNamespace('MAPI')

        updated_count = 0
        total_attachments_found = 0

        for r in need_backfill:
            msg_id = r['id']
            msg_file = r['message_file_path']
            if not msg_file or not os.path.exists(msg_file):
                # Try locating by prefix in email_messages/
                short_id = r['source_message_id']
                matches = glob.glob(f"email_messages/*{short_id[:16]}*.msg")
                if matches:
                    msg_file = matches[0]
            
            if not msg_file or not os.path.exists(msg_file):
                continue
            
            try:
                item = ns.OpenSharedItem(os.path.abspath(msg_file))
                atts = outlook_ingest.list_attachments(item)
                item.Close(1)
                
                if atts:
                    db.record_attachments(msg_id, atts, conn=conn)
                    updated_count += 1
                    total_attachments_found += len(atts)
                    logger.info(f"Message {msg_id} ({r['subject']!r}): backfilled {len(atts)} attachment(s): {[a['name'] for a in atts]}")
            except Exception as e:
                logger.warning(f"Error processing .msg for message {msg_id}: {e}")
                
        logger.info(f"Backfill complete! Updated {updated_count} messages with {total_attachments_found} total attachments.")
        return updated_count, total_attachments_found
    finally:
        conn.close()

if __name__ == '__main__':
    backfill_attachments()
