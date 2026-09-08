"""
Re-clean and backfill lead_details for existing leads in pipeline.db from their
cached message bodies, restoring the full email thread history.

Usage: python backfill_lead_details.py
"""

import logging
import sqlite3
from pathlib import Path

import db
import email_cleaner

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-7s  %(message)s')
logger = logging.getLogger(__name__)

def backfill_lead_details(db_path='pipeline.db'):
    conn = db.get_connection(db_path)
    try:
        leads = conn.execute("""
            SELECT l.id, l.message_id, l.lead_details, m.body_cache_path, m.translated_body_cache_path
            FROM leads l
            JOIN messages m ON l.message_id = m.id
        """).fetchall()
        logger.info(f"Checking {len(leads)} leads in {db_path}...")

        updated_count = 0
        for row in leads:
            lead_id = row['id']
            cache_path = row['translated_body_cache_path'] or row['body_cache_path']
            if not cache_path:
                continue
            path = Path(cache_path)
            if not path.is_file():
                continue

            try:
                raw_text = path.read_text(encoding='utf-8', errors='replace')
                # Remove cached header if present
                divider = ('=' * 60) + '\n\n'
                body = raw_text.split(divider, 1)[1] if divider in raw_text else raw_text

                cleaned_details = email_cleaner.clean_email_body(body)
                if cleaned_details and cleaned_details != row['lead_details']:
                    conn.execute("UPDATE leads SET lead_details = ? WHERE id = ?", (cleaned_details, lead_id))
                    updated_count += 1
                    logger.info(f"Updated lead {lead_id}: restored full email details ({len(cleaned_details)} chars)")
            except Exception as exc:
                logger.warning(f"Could not backfill details for lead {lead_id}: {exc}")

        conn.commit()
        logger.info(f"Backfill complete! Updated {updated_count} leads.")
        return updated_count
    finally:
        conn.close()

if __name__ == '__main__':
    backfill_lead_details()
