"""
Entry point: submit approved leads to Oracle Sales Cloud via its REST API.

Only ever acts on leads with status='approved' (set via the dashboard's
Approve button). Each lead is atomically claimed before submission, so two
overlapping runs can't send the same one twice.

Before contacting Oracle, every lead is checked against the local record of
what has already been submitted (db.find_submitted_duplicate). That guard is
the one that holds: Oracle's own dedup query can't see leads it hasn't
assigned an owner to yet, including ones this pipeline created minutes ago.

Credentials come from secrets_store.py (ORACLE_USERNAME/ORACLE_PASSWORD in the
OS credential store), never hardcoded.

A Playwright browser-automation submitter used to live alongside this as a
fallback and was removed on 2026-07-31. The REST path is the only way leads
reach Oracle.

Usage:  py pipeline_submit.py
"""

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import company_website
import db
import secrets_store
from oracle_api import OracleApiError, OracleLeadApiClient, submission_blockers

logger = logging.getLogger(__name__)
LOG_DIR = Path(__file__).parent / 'logs'


def setup_logging():
    """Same pattern as pipeline_ingest.py -- a scheduled Task Scheduler run has
    no console anyone is watching, so this is the only record of what an
    unattended submit run actually did (which leads, any failures)."""
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = LOG_DIR / f'submit_{stamp}.log'
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-7s  %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file

# Where the lead came from, spelled out in the Oracle project name. A
# spreadsheet attached to a Sales inbox email is a trade-show batch and gets
# labeled accordingly; a lead extracted from the email body itself is a direct
# inquiry; one typed into the dashboard by hand (see manual_lead.py) arrived
# some other way entirely -- a phone call, a card, a colleague's forward --
# and says so rather than claiming to have come from the website.
_SOURCE_LABELS = {
    'email_excel_attachment': ('Tradeshow', 'Email Import'),
    'email_ai_extract': ('Inquiry', 'Website/Email'),
    'manual_entry': ('Inquiry', 'Manual Entry'),
}
_DEFAULT_SOURCE_LABEL = ('Inquiry', 'Website/Email')


def _project_label(lead):
    return _SOURCE_LABELS.get(lead.get('source'), _DEFAULT_SOURCE_LABEL)


def _load_oracle_config():
    with open(Path(__file__).parent / 'config.json') as f:
        config = json.load(f)
    oracle_config = dict(config['oracle'])
    # Every lead this pipeline submits comes from the Sales inbox (website
    # inquiries/form submissions), never a physical trade show. Forced here so
    # it holds regardless of what config.json carries.
    oracle_config['lead_source'] = 'Website'
    return oracle_config



def _attachment_file_name(lead, path):
    """Name the attachment after the email's subject line so the Oracle
    Attachments row is recognisable, keeping the real file's extension (.msg
    for an exported Outlook message, .txt for a cached body)."""
    subject = ''
    if lead.get('message_id'):
        msg_row = db.get_message(lead['message_id']) or {}
        subject = msg_row.get('subject') or ''
    safe_subject = re.sub(r'[^\w\s\-]', '', subject).strip()[:80]
    return f'{safe_subject or "source_email"}{Path(path).suffix or ".txt"}'


def _attach_source_email(client, lead, oracle_id, result):
    """
    Attach the lead's source email to the Oracle lead record that was just
    created. Returns a warning string to store on the lead, or None.

    Wrapped end-to-end in a catch-all on purpose: this runs *after* Oracle has
    already accepted the lead, and _run_api_submit's outer handler responds to
    an exception by scheduling a retry. Anything escaping from here would
    therefore resubmit a lead that already exists in Oracle. Failing to attach
    a file is never worth a duplicate lead.
    """
    if not oracle_id:
        return None
    try:
        attachment_path = db.get_lead_email_attachment(lead['id'])
        if not attachment_path:
            logger.warning(
                f'  No source email on file for lead {lead["id"]}; nothing to attach.'
            )
            return None

        file_name = _attachment_file_name(lead, attachment_path)
        if client.upload_attachment(
            oracle_id, attachment_path, file_name=file_name,
            oracle_response=result.get('response'),
        ):
            return None
        return (
            'Source email could not be attached to the Oracle lead; '
            'attach it by hand if it is needed.'
        )
    except Exception as e:
        logger.exception(f'  Attaching source email to lead {lead["id"]} failed: {e}')
        return f'Source email attachment step errored ({e}); attach it by hand if needed.'


def _run_api_submit(client, leads):
    """Full unattended submission through Oracle's REST API."""
    # 'returned' is counted apart from 'fail': nothing failed on Oracle's side,
    # the lead simply came back for a correction and is waiting in the queue.
    results = {'ok': 0, 'fail': 0, 'returned': 0}
    for lead in leads:
        if not db.claim_lead_for_submission(lead['id']):
            logger.info(
                f'  Skipped lead {lead["id"]}; it was claimed or changed by another run.'
            )
            continue

        source_label, trade_show_name = _project_label(lead)
        label = f"{lead.get('organization') or '?'} - {lead.get('first_name') or ''} {lead.get('last_name') or ''}"

        # Backstop. The dashboard refuses to approve a lead with any of these
        # missing (see dashboard/app.py approve()), so reaching here means the
        # data changed after approval. Every blocker is a field a human can
        # correct, so the lead goes back to Pending Review with the reason
        # attached rather than to History as 'failed', where it had to be found
        # and manually returned before it could be fixed.
        blockers = submission_blockers(lead)
        if blockers:
            detail = 'Submission blocked: ' + ' '.join(blockers)
            db.return_lead_for_correction(lead['id'], detail)
            logger.error(f'  Returned for correction, not submitted: {label} -- {detail}')
            results['returned'] += 1
            continue

        force_submit = bool(lead.get('force_submit'))

        # Local duplicate guard, checked before Oracle is touched at all.
        # Oracle's own dedup query cannot see leads it has not yet assigned an
        # owner to -- including ones this pipeline created minutes earlier --
        # so it silently misses the most likely duplicates. See
        # db.find_submitted_duplicate().
        if not force_submit:
            prior = db.find_submitted_duplicate(lead['id'])
            if prior:
                detail = (
                    f'Not sent to Oracle: lead {prior["id"]} '
                    f'({prior.get("oracle_project_name") or "no project name"}) was already '
                    f'submitted as Oracle record {prior["oracle_record_id"]} on '
                    f'{(prior.get("submitted_at") or "")[:10]}.'
                )
                db.mark_lead_duplicate_of_submitted(lead['id'], prior, detail)
                logger.warning(f'  Skipped as duplicate: {label} -- {detail}')
                results['ok'] += 1
                continue
        else:
            logger.info(f'  Overriding duplicate check for lead {lead["id"]} (force_submit set)')

        # Backstop for leads staged before website derivation existed (and for
        # any source that bypasses pipeline_ingest): fill it in here so they
        # aren't submitted with an empty Website field. Persisted, so the
        # dashboard shows what Oracle was actually sent.
        if not (lead.get('website') or '').strip():
            website = company_website.derive_website(lead)
            if website:
                lead['website'] = website
                db.update_lead_fields(lead['id'], {'website': website})

        try:
            result = client.submit_lead(
                lead, trade_show_name, source_label=source_label,
                bypass_dedup=force_submit
            )
            all_warnings = list(result.get('field_warnings') or [])

            oracle_id = result.get('oracle_record_id')
            warning = _attach_source_email(client, lead, oracle_id, result)
            if warning:
                all_warnings.append(warning)

            db.mark_lead_submitted(
                lead['id'],
                result['project_name'],
                oracle_record_id=oracle_id,
                field_warnings=all_warnings or None,
                oracle_payload=result.get('payload'),
            )
            if result.get('oracle_lead_number'):
                logger.info(
                    f'  Submitted via API: {label} '
                    f'({result["oracle_lead_number"]}, {result["project_name"]})'
                )
            else:
                logger.info(f'  Submitted via API: {label} ({result["project_name"]})')
            for warning in all_warnings:
                logger.warning(f'  Field warning for lead {lead["id"]}: {warning}')
            results['ok'] += 1
        except OracleApiError as e:
            detail = str(e)
            # A transport failure after POST may mean Oracle created the lead
            # but the response never reached us. Never retry that blindly.
            if e.outcome_unknown:
                db.mark_submission_unknown(lead['id'], detail)
            elif e.status_code is not None and e.status_code < 500:
                db.mark_lead_failed(lead['id'], detail)
            else:
                db.schedule_lead_retry(lead['id'], detail)
            logger.error(f'  API failed: {label} -- {detail}')
            results['fail'] += 1
        except Exception as e:
            detail = f'Unexpected API submission error: {e}'
            db.schedule_lead_retry(lead['id'], detail)
            logger.exception(f'  Exception submitting {label}: {e}')
            results['fail'] += 1
    return results


def main():
    log_file = setup_logging()
    logger.info(f'Log file: {log_file}')
    load_dotenv(Path(__file__).parent / '.env', override=False)
    db.init_db()
    recovered = db.release_stale_submission_claims()
    if recovered:
        logger.warning(
            f'{recovered} stale submission claim(s) moved to submission_unknown '
            'for manual Oracle reconciliation.'
        )

    oracle_config = _load_oracle_config()

    leads = db.list_ready_for_submission()
    if not leads:
        logger.info('No approved leads waiting for submission.')
        return

    logger.info(f'{len(leads)} approved lead(s) to submit.')

    username = secrets_store.get_secret('ORACLE_USERNAME')
    password = secrets_store.get_secret('ORACLE_PASSWORD')
    if not username or not password:
        logger.error(
            'Missing ORACLE_USERNAME/ORACLE_PASSWORD in Windows Credential Manager or .env.'
        )
        sys.exit(1)

    client = OracleLeadApiClient(oracle_config, username=username, password=password)
    results = _run_api_submit(client, leads)
    db.set_run_state('oracle_session_status', 'api')
    db.set_run_state('last_submit_run_at', datetime.now(timezone.utc).isoformat())
    logger.info(
        f"Submit run complete. {results['ok']} ok, {results['fail']} failed, "
        f"{results.get('returned', 0)} returned to the queue for correction."
    )


if __name__ == '__main__':
    main()
