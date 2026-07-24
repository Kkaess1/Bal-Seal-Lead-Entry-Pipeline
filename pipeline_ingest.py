"""
Scheduled entry point: ingest new mail, extract lead fields, screen for
fraud, assign territory, and stage everything in SQLite as pending_review
(or auto-rejected for clear-cut junk). Run twice daily via Windows Task
Scheduler.

Does NOT touch Oracle -- that's pipeline_submit.py, run separately, and it
only ever acts on leads a human has already approved via the dashboard.

Usage:  py pipeline_ingest.py
        py pipeline_ingest.py --recent 10   (bounded test run, see main())
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import db
import email_cleaner
import excel_parser
import fraud_screen
import lead_extractor
import outlook_ingest
import review_marking
import territory_map

logger = logging.getLogger(__name__)

EMAIL_CACHE_DIR = Path(__file__).parent / 'email_cache'
LOG_DIR = Path(__file__).parent / 'logs'


def setup_logging():
    """
    Same pattern as main.py's setup_logging() -- a scheduled Task Scheduler
    run has no console anyone is watching, so without a file handler here,
    a 10am/5pm run's output would just vanish. Mirrors main.py rather than
    reusing it directly to keep each entry point's logging self-contained.
    """
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = LOG_DIR / f'ingest_{stamp}.log'
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-7s  %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


def _cache_body(message_id, body_text, msg_dict=None):
    EMAIL_CACHE_DIR.mkdir(exist_ok=True)
    path = EMAIL_CACHE_DIR / f'{message_id}.txt'
    if msg_dict:
        header = (
            f"From: {msg_dict.get('sender_name', '')} <{msg_dict.get('sender_email', '')}>\n"
            f"Date: {msg_dict.get('received_at', '')}\n"
            f"Subject: {msg_dict.get('subject', '')}\n"
            f"{'='*60}\n\n"
        )
        content = header + (body_text or '')
    else:
        content = body_text or ''
    path.write_text(content, encoding='utf-8')
    return str(path)


def _screen_and_stage(lead_id, lead, sender_email, subject=None, is_followup=False, followup_reason=None):
    """Territory assignment + fraud screening for a lead that's already been
    inserted. All leads are staged in pending_review so the human user can review them."""
    tm, basis = territory_map.assign_tm(lead)
    db.set_territory_assignment(lead_id, tm, basis)

    result = fraud_screen.screen_lead(
        lead, sender_email=sender_email, subject=subject,
        is_followup=is_followup, followup_reason=followup_reason
    )
    risk_level = 'reject' if result['auto_reject'] else result['fraud_risk']
    db.set_fraud_screen_result(lead_id, risk_level, result['fraud_reasons'], result['fraud_score'])
    db.update_lead_fields(lead_id, {'status': 'pending_review'})
    logger.info(f'  Lead {lead_id}: staged for review (risk={risk_level})')


def _process_excel_attachment(message_row_id, path, sender_email, subject=None):
    """Existing excel_parser.py path -- one lead per spreadsheet row, same as
    main.py's manual flow already produces."""
    rows = excel_parser.parse_excel(path)
    for row in rows:
        lead_id = db.insert_lead(row, source='email_excel_attachment',
                                  message_id=message_row_id, source_row_number=row.get('row_number'))
        _screen_and_stage(lead_id, row, sender_email, subject=subject)
    return len(rows)


def _process_body_text(message_row_id, msg):
    """AI-extraction path for messages without an Excel attachment.
    Always inserts a lead record so every email is visible on the dashboard for human review."""
    extraction = lead_extractor.extract_lead(
        subject=msg['subject'], sender_email=msg['sender_email'],
        sender_name=msg['sender_name'], body=msg['body_text'],
    )

    lead = dict(extraction.get('fields') or {})
    if not lead.get('email'):
        lead['email'] = msg['sender_email']

    lead['lead_details'] = email_cleaner.clean_email_body(msg['body_text'])
    lead_id = db.insert_lead(lead, source='email_ai_extract', message_id=message_row_id)
    db.set_extraction_result(
        lead_id,
        extraction.get('confidence', 0.0) if extraction.get('is_lead') else 0.1,
        extraction.get('notes', 'Flagged by AI extraction for human review')
    )
    _screen_and_stage(
        lead_id, lead, msg['sender_email'],
        subject=msg.get('subject'),
        is_followup=extraction.get('is_followup', False),
        followup_reason=extraction.get('followup_reason'),
    )


def process_message(msg, force=False):
    """Handles one ingestion-backend message dict (see
    outlook_ingest.fetch_new_messages -- same shape email_ingest.py's Graph
    path also produces). Idempotent unless force=True."""
    body_cache_path = _cache_body(msg['id'], msg['body_text'], msg_dict=msg)
    message_row_id = db.insert_message(
        source_message_id=msg['id'], received_at=msg['received_at'],
        sender_email=msg['sender_email'], sender_name=msg['sender_name'],
        subject=msg['subject'], has_excel_attachment=msg['has_attachments'],
        body_cache_path=body_cache_path,
    )
    if message_row_id is None:
        if force:
            existing = db.get_message_by_source_id(msg['id'])
            if existing:
                message_row_id = existing['id']
            else:
                return
        else:
            return  # already processed in a prior run (or a dedup race)

    try:
        handled_as_excel = False
        if msg['has_attachments']:
            paths = outlook_ingest.download_excel_attachments(msg['id'])
            for path in paths:
                _process_excel_attachment(message_row_id, path, msg['sender_email'])
                handled_as_excel = True

        if not handled_as_excel:
            _process_body_text(message_row_id, msg)

        db.update_message_status(message_row_id, 'processed')
    except Exception as e:
        logger.error(f"  Message {msg['id']} failed: {e}")
        db.update_message_status(message_row_id, 'error', error_detail=str(e))
    # Note: the email is NOT marked in Outlook here anymore -- that now only
    # happens once a human has approved/rejected every lead pulled from it,
    # via dashboard/app.py's _maybe_mark_email_reviewed(). Ingestion alone
    # (this function) doesn't mean it's been reviewed.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--recent', type=int, default=None, metavar='N',
        help='Process only the N most recent messages in the Sales inbox, ignoring '
             'the last-checked watermark. For bounded test/backfill runs -- does not '
             'affect what a normal run picks up next.',
    )
    parser.add_argument(
        '--force', action='store_true',
        help='Force re-extraction and processing even if the message was previously ingested.',
    )
    args = parser.parse_args()

    log_file = setup_logging()
    logger.info(f'Log file: {log_file}')
    load_dotenv(Path(__file__).parent / '.env', override=False)
    db.init_db()

    try:
        if args.recent:
            logger.info(f'Fetching the {args.recent} most recent message(s) (bounded run, force={args.force})...')
            messages = outlook_ingest.fetch_recent_messages(args.recent)
        else:
            logger.info('Fetching new mail...')
            messages = outlook_ingest.fetch_new_messages()
    except RuntimeError as e:
        logger.error(f'Could not fetch mail: {e}')
        sys.exit(1)

    logger.info(f'{len(messages)} message(s).')
    for msg in messages:
        process_message(msg, force=args.force)

    if not args.recent:
        # A bounded --recent run doesn't touch last_ingest_run_at -- it isn't
        # a real scheduled cycle, and shouldn't look like one on /status.
        db.set_run_state('last_ingest_run_at', datetime.now(timezone.utc).isoformat())
    logger.info('Ingest run complete.')


if __name__ == '__main__':
    main()
