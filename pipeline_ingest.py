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
import hashlib
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import company_website
import db
import email_cleaner
import email_translator
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
    A scheduled Task Scheduler run has no console anyone is watching, so
    without a file handler here a 10am/5pm run's output would just vanish.
    Duplicated in pipeline_submit.py rather than shared, to keep each entry
    point's logging self-contained.
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
    short_id = hashlib.sha256(message_id.encode()).hexdigest()[:24]
    path = EMAIL_CACHE_DIR / f'{short_id}.txt'
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


def _export_message_file(msg, existing_row=None):
    """
    Export the original email to a .msg file so it can later be attached to
    the Oracle lead. Done here, at ingest, because this is the moment the
    message is known to still be sitting in the Sales inbox with Outlook
    reachable -- pipeline_submit.py runs much later and may have neither.

    Best-effort by design: leads still submit fine with only the cached body
    text, so a failed export is a warning, never an ingest failure. Retry
    paths (_message_from_row) don't come from Outlook at all and are skipped.
    """
    if existing_row and existing_row.get('message_file_path'):
        cached = Path(existing_row['message_file_path'])
        if cached.exists():
            return str(cached)
    try:
        path = outlook_ingest.save_message_file(msg['id'], subject=msg.get('subject'))
    except Exception as e:
        logger.warning(f"  Could not export original email to .msg: {e}")
        return None
    if not path:
        logger.warning(
            f"  Could not export original email {msg.get('subject')!r} to .msg; "
            'the cached body text will be attached to Oracle instead.'
        )
    return path


def _screen_and_stage(lead_id, lead, sender_email, subject=None, is_followup=False,
                      followup_reason=None, precomputed_llm_result=None):
    """Territory assignment + fraud screening for a lead that's already been
    inserted. All leads are staged in pending_review so the human user can review them."""
    tm, basis = territory_map.assign_tm(lead)
    db.set_territory_assignment(lead_id, tm, basis)

    result = fraud_screen.screen_lead(
        lead, sender_email=sender_email, subject=subject,
        is_followup=is_followup, followup_reason=followup_reason,
        precomputed_llm_result=precomputed_llm_result,
    )
    risk_level = 'reject' if result['auto_reject'] else result['fraud_risk']
    db.set_fraud_screen_result(lead_id, risk_level, result['fraud_reasons'], result['fraud_score'])
    db.update_lead_fields(lead_id, {'status': 'pending_review'})
    logger.info(f'  Lead {lead_id}: staged for review (risk={risk_level})')


def _fill_website(lead):
    """Sets lead['website'] from the contact's email domain when nobody gave
    us one outright, so Oracle's Website field arrives populated. Runs before
    the insert so the derived value is on the dashboard for review, not a
    surprise applied at submission time."""
    website = company_website.derive_website(lead)
    if website:
        lead['website'] = website
    return lead


def _fill_market(lead):
    """Use Oracle's general-purpose market when no specific one was found."""
    if not (lead.get('market') or '').strip():
        lead['market'] = 'INDUSTRIAL'
    return lead


def _process_excel_attachment(message_row_id, path, sender_email, subject=None, force=False):
    """Spreadsheet attached to a Sales inbox email -- one lead per row, parsed
    by excel_parser.py."""
    rows = excel_parser.parse_excel(path)
    for row in rows:
        _fill_website(row)
        _fill_market(row)
        ingest_key = (
            f'message:{message_row_id}:excel:{Path(path).name}:'
            f'{row.get("row_number")}'
        )
        lead_id = db.insert_lead(row, source='email_excel_attachment',
                                  message_id=message_row_id,
                                  source_row_number=row.get('row_number'),
                                  ingest_key=ingest_key,
                                  replace_existing=force)
        _screen_and_stage(lead_id, row, sender_email, subject=subject)
    return len(rows)


def _process_body_text(message_row_id, msg, force=False):
    """AI-extraction path for messages without an Excel attachment.
    Always inserts a lead record so every email is visible on the dashboard for human review."""
    extraction = lead_extractor.extract_lead(
        subject=msg['subject'], sender_email=msg['sender_email'],
        sender_name=msg['sender_name'], body=msg['body_text'],
    )

    display_body = msg['body_text']
    source_language = email_translator.normalize_language(
        extraction.get('source_language')
    )
    if email_translator.needs_translation(source_language):
        try:
            translated = email_translator.translate_email(
                msg.get('subject'), msg.get('body_text'), source_language,
            )
            display_body = translated['body']
            translated_cache_path = _cache_body(
                f"{msg['id']}:english", display_body,
                msg_dict={**msg, 'subject': translated['subject']},
            )
            db.update_message_fields(message_row_id, {
                'original_language': source_language,
                'translated_subject': translated['subject'],
                'translated_body_cache_path': translated_cache_path,
            })
            logger.info('  Translated message %s from %s to English.',
                        msg['id'], source_language)
        except Exception as exc:
            logger.warning('  Could not translate message %s from %s: %s',
                           msg['id'], source_language, exc)

    lead = dict(extraction.get('fields') or {})
    if not lead.get('email'):
        lead['email'] = msg['sender_email']

    lead['lead_details'] = email_cleaner.clean_email_body(display_body)
    _fill_website(lead)
    _fill_market(lead)
    lead_id = db.insert_lead(
        lead, source='email_ai_extract', message_id=message_row_id,
        ingest_key=f'message:{message_row_id}:body', replace_existing=force,
    )
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
        precomputed_llm_result=extraction.get('plausibility'),
    )


def process_message(msg, force=False, ingest_batch_id=None):
    """Handles one Sales inbox message dict (see
    outlook_ingest.fetch_new_messages). Idempotent unless force=True."""

    # -----------------------------------------------------------
    # Green-category gate: an email tagged green in Outlook has already been
    # entered, so skip it entirely rather than re-extracting it into a fresh
    # lead. Green means the category's COLOUR, whatever it is named -- this
    # mailbox uses "Completed". Read status is irrelevant: merely opening an
    # email still leaves it to be ingested normally.
    #
    # Messages rebuilt from the database for a retry (_message_from_row) have
    # no category information, so ask Outlook directly rather than letting
    # them through ungated -- that gap was re-ingesting already-entered mail
    # on every recovery run.
    # -----------------------------------------------------------
    reviewed = msg.get('reviewed_in_outlook')
    if reviewed is None:
        reviewed = outlook_ingest.is_reviewed(msg['id'])
    if reviewed and not force:
        logger.info(
            f"  Skipping message {msg['subject']!r} -- already tagged green "
            f"(entered) in Outlook.  Use --force to re-ingest."
        )
        # If a previous run had left this message in new/error, close it out so
        # retry_pending_messages() stops picking it up (and re-querying Outlook)
        # on every future run.
        prior = db.get_message_by_source_id(msg['id'])
        if prior and prior['status'] in ('new', 'error', 'processing'):
            db.update_message_status(
                prior['id'], 'processed',
                error_detail='Skipped: tagged green (already entered) in Outlook.',
            )
        return True

    existing = db.get_message_by_source_id(msg['id'])
    body_cache_path = (
        existing.get('body_cache_path') if existing else
        _cache_body(msg['id'], msg['body_text'], msg_dict=msg)
    )
    message_file_path = _export_message_file(msg, existing)
    message_row_id = db.insert_message(
        source_message_id=msg['id'], received_at=msg['received_at'],
        sender_email=msg['sender_email'], sender_name=msg['sender_name'],
        subject=msg['subject'], has_excel_attachment=msg['has_attachments'],
        body_cache_path=body_cache_path, message_file_path=message_file_path,
        conversation_id=msg.get('conversation_id'),
        ingest_batch_id=ingest_batch_id,
    )
    if message_row_id is not None:
        db.record_attachments(message_row_id, msg.get('attachments'))
    else:
        existing = existing or db.get_message_by_source_id(msg['id'])
        if not existing:
            return False
        message_row_id = existing['id']
        # Backfill for rows inserted before attachments were recorded.
        if msg.get('attachments') and not existing.get('attachments'):
            db.record_attachments(message_row_id, msg['attachments'])
        # Backfill for rows inserted before .msg export existed, or whose
        # earlier export attempt failed.
        if message_file_path and not existing.get('message_file_path'):
            db.set_message_file_path(message_row_id, message_file_path)
        if existing['status'] == 'processed' and not force:
            return True
        if force:
            db.update_message_status(message_row_id, 'error', 'Forced reprocessing requested')

    if not db.claim_message(message_row_id):
        return db.get_message(message_row_id).get('status') == 'processed'

    try:
        handled_as_excel = False
        if msg['has_attachments']:
            paths = outlook_ingest.download_excel_attachments(msg['id'])
            for path in paths:
                _process_excel_attachment(
                    message_row_id, path, msg['sender_email'],
                    subject=msg.get('subject'), force=force,
                )
                handled_as_excel = True

        if not handled_as_excel:
            _process_body_text(message_row_id, msg, force=force)

        db.update_message_status(message_row_id, 'processed')
        return True
    except Exception as e:
        logger.exception(f"  Message {msg['id']} failed: {e}")
        db.update_message_status(message_row_id, 'error', error_detail=str(e))
        return False
    # Note: the email is NOT marked in Outlook here anymore -- that now only
    # happens once a human has approved/rejected every lead pulled from it,
    # via dashboard/app.py's _maybe_mark_email_reviewed(). Ingestion alone
    # (this function) doesn't mean it's been reviewed.


def _message_from_row(row):
    cache_path = Path(row.get('body_cache_path') or '')
    if not cache_path.exists():
        raise FileNotFoundError(f'Cached body is missing: {cache_path}')
    cached = cache_path.read_text(encoding='utf-8', errors='replace')
    # Cached files include a small metadata header followed by this divider.
    marker = ('=' * 60) + '\n\n'
    body = cached.split(marker, 1)[1] if marker in cached else cached
    return {
        'id': row['source_message_id'],
        'conversation_id': row.get('conversation_id'),
        'received_at': row.get('received_at'),
        'sender_email': row.get('sender_email'),
        'sender_name': row.get('sender_name'),
        'subject': row.get('subject'),
        'body_text': body,
        'has_attachments': bool(row.get('has_excel_attachment')),
    }


def retry_pending_messages():
    """Retry durable work left in new/error by a prior crash or transient error."""
    all_ok = True
    retry_batch_ids = set()
    for row in db.list_messages(status=['new', 'error']):
        try:
            msg = _message_from_row(row)
        except Exception as e:
            logger.error(f'Could not reconstruct message {row["id"]} for retry: {e}')
            db.update_message_status(row['id'], 'error', str(e))
            all_ok = False
            continue
        all_ok = process_message(msg, ingest_batch_id=row.get('ingest_batch_id')) and all_ok
        if row.get('ingest_batch_id'):
            retry_batch_ids.add(row['ingest_batch_id'])
    for batch_id in retry_batch_ids:
        removed = db.consolidate_ingest_batch_threads(batch_id)
        if removed:
            logger.info('Consolidated %s older thread lead(s) from retry batch %s.',
                        removed, batch_id)
    return all_ok


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

    # Recover work that was fetched and stored by an earlier run but never
    # completed. This includes rows that predate the durable-watermark change.
    recovered = db.release_stale_message_claims()
    if recovered:
        logger.warning(f'Recovered {recovered} stale message-processing claim(s).')
    retry_pending_messages()

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
    ingest_batch_id = uuid.uuid4().hex
    all_ok = True
    for msg in messages:
        all_ok = process_message(
            msg, force=args.force, ingest_batch_id=ingest_batch_id,
        ) and all_ok

    removed = db.consolidate_ingest_batch_threads(ingest_batch_id)
    if removed:
        logger.info('Consolidated %s older lead(s) from same-batch email threads.', removed)

    if not args.recent:
        if all_ok:
            outlook_ingest.commit_fetch_watermark()
        else:
            logger.warning(
                'At least one message failed; Outlook watermark was not advanced '
                'so it can be retried safely.'
            )
        # A bounded --recent run doesn't touch last_ingest_run_at -- it isn't
        # a real scheduled cycle, and shouldn't look like one on /status.
        db.set_run_state('last_ingest_run_at', datetime.now(timezone.utc).isoformat())
    logger.info('Ingest run complete.')


if __name__ == '__main__':
    main()
