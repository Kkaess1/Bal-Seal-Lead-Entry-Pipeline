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


def _cache_body(message_id, body_text):
    EMAIL_CACHE_DIR.mkdir(exist_ok=True)
    path = EMAIL_CACHE_DIR / f'{message_id}.txt'
    path.write_text(body_text or '', encoding='utf-8')
    return str(path)


def _screen_and_stage(lead_id, lead, sender_email):
    """Territory assignment + fraud screening for a lead that's already been
    inserted. Shared by both the Excel-attachment path and the AI-extraction
    path so they're screened identically."""
    tm, basis = territory_map.assign_tm(lead)
    db.set_territory_assignment(lead_id, tm, basis)

    result = fraud_screen.screen_lead(lead, sender_email=sender_email)
    if result['auto_reject']:
        db.set_fraud_screen_result(lead_id, 'reject', result['fraud_reasons'], result['fraud_score'])
        db.update_lead_fields(lead_id, {
            'status': 'rejected',
            'review_notes': 'Auto-rejected by rule engine (unambiguous junk) -- see fraud_reasons.',
        })
        logger.info(f'  Lead {lead_id}: auto-rejected')
        # This is a decided state too, same as a human rejecting via the
        # dashboard -- mark the source email reviewed if everything else
        # pulled from it (e.g. other rows in the same Excel attachment) is
        # also decided.
        review_marking.maybe_mark_email_reviewed(lead_id)
    else:
        db.set_fraud_screen_result(lead_id, result['fraud_risk'], result['fraud_reasons'], result['fraud_score'])
        logger.info(f'  Lead {lead_id}: staged for review (risk={result["fraud_risk"]})')


def _process_excel_attachment(message_row_id, path, sender_email):
    """Existing excel_parser.py path -- one lead per spreadsheet row, same as
    main.py's manual flow already produces."""
    rows = excel_parser.parse_excel(path)
    for row in rows:
        lead_id = db.insert_lead(row, source='email_excel_attachment',
                                  message_id=message_row_id, source_row_number=row.get('row_number'))
        _screen_and_stage(lead_id, row, sender_email)
    return len(rows)


def _process_body_text(message_row_id, msg):
    """AI-extraction path for messages without an Excel attachment (or where
    the attachment wasn't an Excel file)."""
    extraction = lead_extractor.extract_lead(
        subject=msg['subject'], sender_email=msg['sender_email'],
        sender_name=msg['sender_name'], body=msg['body_text'],
    )

    if extraction['is_lead'] is None:
        # Extraction itself couldn't run (no API key / call failed) -- still
        # worth a human look, never silently dropped.
        lead = {'email': msg['sender_email'], 'lead_details': email_cleaner.clean_email_body(msg['body_text'])}
        lead_id = db.insert_lead(lead, source='email_ai_extract', message_id=message_row_id)
        db.set_extraction_result(lead_id, 0.0, extraction['notes'])
        _screen_and_stage(lead_id, lead, msg['sender_email'])
        return

    if not extraction['is_lead']:
        logger.info(f"  Message {msg['id']}: not a lead ({extraction['notes']})")
        return

    lead = dict(extraction['fields'])
    # Cleaned of clutter (security banners, tracking/SafeLinks URLs, embedded-image
    # references, disclaimers, quoted reply history, automated-system footers --
    # see email_cleaner.py), but NOT summarized or paraphrased -- the customer's
    # actual words (including their signature) are untouched. This is what lands
    # in Oracle's "Lead Details" field, and what a rep sees is what actually gets
    # submitted -- the dashboard already showed this same cleaned version during
    # review, so the two need to match, not just look similar.
    lead['lead_details'] = email_cleaner.clean_email_body(msg['body_text'])
    lead_id = db.insert_lead(lead, source='email_ai_extract', message_id=message_row_id)
    db.set_extraction_result(lead_id, extraction['confidence'], extraction['notes'])
    _screen_and_stage(lead_id, lead, msg['sender_email'])


def process_message(msg):
    """Handles one ingestion-backend message dict (see
    outlook_ingest.fetch_new_messages -- same shape email_ingest.py's Graph
    path also produces). Idempotent: re-running against an already-processed
    message is a no-op, via the hard unique constraint on
    messages.source_message_id."""
    body_cache_path = _cache_body(msg['id'], msg['body_text'])
    message_row_id = db.insert_message(
        source_message_id=msg['id'], received_at=msg['received_at'],
        sender_email=msg['sender_email'], sender_name=msg['sender_name'],
        subject=msg['subject'], has_excel_attachment=msg['has_attachments'],
        body_cache_path=body_cache_path,
    )
    if message_row_id is None:
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
    args = parser.parse_args()

    log_file = setup_logging()
    logger.info(f'Log file: {log_file}')
    load_dotenv(Path(__file__).parent / '.env', override=False)
    db.init_db()

    try:
        if args.recent:
            logger.info(f'Fetching the {args.recent} most recent message(s) (bounded run)...')
            messages = outlook_ingest.fetch_recent_messages(args.recent)
        else:
            logger.info('Fetching new mail...')
            messages = outlook_ingest.fetch_new_messages()
    except RuntimeError as e:
        logger.error(f'Could not fetch mail: {e}')
        sys.exit(1)

    logger.info(f'{len(messages)} message(s).')
    for msg in messages:
        process_message(msg)

    if not args.recent:
        # A bounded --recent run doesn't touch last_ingest_run_at -- it isn't
        # a real scheduled cycle, and shouldn't look like one on /status.
        db.set_run_state('last_ingest_run_at', datetime.now(timezone.utc).isoformat())
    logger.info('Ingest run complete.')


if __name__ == '__main__':
    main()
