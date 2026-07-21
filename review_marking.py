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

import logging

import db
import outlook_ingest

logger = logging.getLogger(__name__)

# Statuses that mean "a decision has been made" -- reachable only after an
# approve/reject (human or automatic), not the earlier pipeline stages.
REVIEWED_STATUSES = {'approved', 'rejected', 'submitted', 'failed'}


def maybe_mark_email_reviewed(lead_id):
    """
    Once every lead tied to a given source email is in a decided state --
    not just this one, since an Excel attachment can produce several leads
    from a single email -- tag that email green in Outlook. Purely cosmetic:
    never raises, so a hiccup here can't break the caller's actual
    approve/reject/ingest action.
    """
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
