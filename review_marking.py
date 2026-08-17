"""
Shared logic for marking a source email's Outlook category once every lead
pulled from it has reached a decided state -- approved or rejected, whether
by a human via the dashboard or the rule-based auto-reject during ingestion
(fraud_screen.py's narrow "unambiguous junk" path also sets status='rejected'
without ever going through the dashboard).

Used by both dashboard/app.py (human approve/reject) and pipeline_ingest.py
(rule-based auto-reject), so the "has everything from this email been
decided yet" check only lives in one place.
"""

import json
import logging
from pathlib import Path

import db
import outlook_ingest

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / 'config.json'

# Statuses that mean "a decision has been made" -- reachable only after an
# approve/reject (human or automatic), not the earlier pipeline stages.
REVIEWED_STATUSES = {'approved', 'rejected', 'submitted', 'failed'}


def marking_enabled():
    """
    Whether to write the green category back to Outlook at all
    (config.json -> outlook.mark_reviewed, default on).

    Turned off while the pipeline is being evaluated, so a test run never
    alters the real Sales mailbox. Note the side effect: the green tag is also
    what stops an email being ingested again, so with marking off, approved
    emails keep coming back into the review queue on every ingest. The
    submission-time duplicate guard is what stops those reaching Oracle.
    """
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            config = json.load(f)
    except (OSError, ValueError):
        return True
    return bool(config.get('outlook', {}).get('mark_reviewed', True))


def ingest_marking_enabled():
    """
    Whether to green-tag an email as soon as the pipeline has ingested it
    (config.json -> outlook.mark_ingested, default on).

    Separate switch from mark_reviewed: that one tags an email once every lead
    from it has been decided, this one tags it the moment its leads are pulled.
    Both write the same green category, so with this on, the tag means "the
    pipeline has read this email", not "this lead has been entered in Oracle".
    """
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            config = json.load(f)
    except (OSError, ValueError):
        return True
    return bool(config.get('outlook', {}).get('mark_ingested', True))


def mark_email_ingested(source_message_id):
    """
    Green-tag a just-ingested email. Never raises: a mailbox that could not be
    tagged is not a reason to fail an ingest run that has already stored its
    leads. The cost of a miss is that the email is read again on a later run,
    which message and lead idempotency already handle.
    """
    if not source_message_id or not ingest_marking_enabled():
        return False
    try:
        outlook_ingest.mark_ingested(source_message_id)
        return True
    except Exception as e:
        logger.warning(f'Could not mark message {source_message_id} ingested in Outlook: {e}')
        return False


def maybe_mark_email_reviewed(lead_id):
    """
    Once every lead tied to a given source email is in a decided state --
    not just this one, since an Excel attachment can produce several leads
    from a single email -- tag that email green in Outlook. Purely cosmetic:
    never raises, so a hiccup here can't break the caller's actual
    approve/reject/ingest action.

    No-op when outlook.mark_reviewed is off -- see marking_enabled().
    """
    if not marking_enabled():
        logger.info(
            'Outlook green-tagging is off (outlook.mark_reviewed=false in '
            'config.json); leaving lead %s\'s source email untagged.', lead_id,
        )
        return

    lead = db.get_lead(lead_id)
    if not lead or not lead.get('message_id'):
        return
    siblings = db.list_leads_by_message(lead['message_id'])
    if not siblings or not all(s['status'] in REVIEWED_STATUSES for s in siblings):
        return
    message = db.get_message(lead['message_id'])
    if not message:
        return
    try:
        outlook_ingest.mark_reviewed(message['source_message_id'])
    except Exception as e:
        logger.warning(f'Could not mark message {message["source_message_id"]} reviewed in Outlook: {e}')
