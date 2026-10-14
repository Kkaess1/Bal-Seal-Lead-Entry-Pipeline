"""
Local lead-review dashboard.
Run:  py dashboard/app.py          (dev, Flask's own server)
      py -m dashboard.serve        (production-ish, via waitress) -- added later

Binds to 127.0.0.1 only -- single-user local tool, not exposed on the network.
"""

import io
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (
    Flask, Request, abort, flash, jsonify, redirect, render_template, request, send_file,
    send_from_directory, session, url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / '.env', override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import email_cleaner  # noqa: E402
import hq_lookup  # noqa: E402
import lead_stages  # noqa: E402
# Aliased: `manual_lead` is also the natural name for a lead variable in
# the routes below, and shadowing the module there would be a live bug.
import manual_lead as manual_lead_module  # noqa: E402
import outlook_ingest  # noqa: E402
import retention  # noqa: E402
import review_marking  # noqa: E402
import secrets_store  # noqa: E402
import spam_folder  # noqa: E402
import spreadsheet_import  # noqa: E402
import territory_map  # noqa: E402
# The same rules the submitter enforces, so the dashboard cannot approve a
# lead that Oracle would then refuse. One definition, checked in both places.
from oracle_api import (  # noqa: E402
    LEAD_SOURCE_VALUES, OracleApiError, OracleLeadApiClient,
    rank_company_accounts, submission_blockers,
)

class _Request(Request):
    """
    Keeps an uploaded spreadsheet of leads in memory while the form is parsed.

    Werkzeug spools any upload over 500 KB to a temporary file, so a customer
    list "parsed from memory" was in fact written to %TEMP% first -- deleted
    on close, but left behind by a crash, and a folder of PII that retention
    knows nothing about. MAX_CONTENT_LENGTH bounds what this can hold.
    """

    def _get_file_stream(self, total_content_length, content_type,
                         filename=None, content_length=None):
        if self.endpoint == 'import_leads':
            return io.BytesIO()
        return super()._get_file_stream(
            total_content_length, content_type, filename, content_length,
        )


app = Flask(__name__)
app.request_class = _Request
app.secret_key = secrets_store.get_secret('DASHBOARD_SECRET_KEY') or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Strict',
)

# Review order follows the mailbox (newest email first), not fraud risk --
# see index(). The old RISK_ORDER map was removed on 2026-08-10 with it.
EMAIL_ADDRESS_RE = re.compile(r'^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$')

# Drop-folder for reference material a reviewer may need to check against --
# territory maps, price lists, product sheets. Contents are read-only to the
# dashboard; adding or replacing a document is a matter of putting a file here,
# with no code change and nothing to register.
REFERENCE_DIR = Path(__file__).resolve().parent.parent / 'reference'

# Where an attachment lands when someone asks to open one. Gitignored, same as
# the ingest cache: these are customers' own files. Nothing is written here
# until a reviewer actually clicks an attachment.
ATTACHMENT_CACHE_DIR = Path(__file__).resolve().parent.parent / 'email_attachments'

# Clicking the button is the only thing that ingests email -- the scheduled
# ingest tasks were removed on 2026-08-07. Two overlapping runs are safe --
# messages are claimed one at a time in the database -- but they are wasted AI
# calls and a confusing log, so a second click inside this window is turned
# away rather than started.
_INGEST_JOB_KEY = 'job:manual_ingest'
_ORACLE_SYNC_JOB_KEY = 'job:manual_oracle_sync'

# What the Reference tab will render, by extension. Everything else in the
# folder is listed with a download link but never served inline.
#
# SVG is deliberately absent: it is an image to a reviewer but a scriptable
# document to a browser, and these files are served from the dashboard's own
# origin, where a script could act as the logged-in operator. PDFs and raster
# images cannot do that.
REFERENCE_IMAGE_TYPES = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
REFERENCE_TEXT_TYPES = {'.txt', '.md', '.csv'}
REFERENCE_PDF_TYPES = {'.pdf'}
REFERENCE_TEXT_PREVIEW_LIMIT = 200_000   # characters rendered inline

# Only what the tab can actually display may be added through it. Anything
# else would be uploaded, stored and then never shown -- and the exclusions
# that matter (.svg, .html) are the ones that would run as script in the
# dashboard's own origin.
REFERENCE_UPLOAD_TYPES = REFERENCE_IMAGE_TYPES | REFERENCE_PDF_TYPES | REFERENCE_TEXT_TYPES
MAX_REFERENCE_UPLOAD_BYTES = 25 * 1024 * 1024
# Caps request bodies, which in this app means reference-document uploads.
app.config['MAX_CONTENT_LENGTH'] = MAX_REFERENCE_UPLOAD_BYTES

LEAD_FIELD_GROUPS = [
    ('Contact', ['first_name', 'last_name', 'title', 'email', 'phone', 'phone_mobile']),
    ('Company', ['organization', 'website', 'market', 'campaign', 'rank', 'trade_show',
                 'lead_source']),
    ('Location', ['address1', 'address2', 'city', 'state', 'zip', 'country']),
    ('Qualification', [
        'notes_action', 'notes_body', 'survey_answers', 'qualifiers',
        'scan_device', 'lead_details',
    ]),
    ('Routing', ['assigned_tm']),
]

# Account names change much less often than leads are reviewed.  Keep one
# short-lived in-process copy so opening several lead editors does not download
# the full OSC account book each time.  The dashboard is restarted daily/on
# deployment, and a 15-minute lifetime keeps newly-created accounts discoverable.
_ORACLE_ACCOUNT_CACHE_SECONDS = 15 * 60
_oracle_account_cache = {'loaded_at': 0.0, 'accounts': None}
_oracle_account_cache_lock = threading.Lock()


def _oracle_client():
    config_path = Path(__file__).resolve().parent.parent / 'config.json'
    with config_path.open(encoding='utf-8') as config_file:
        oracle_config = dict(json.load(config_file)['oracle'])
    username = secrets_store.get_secret('ORACLE_USERNAME')
    password = secrets_store.get_secret('ORACLE_PASSWORD')
    if not username or not password:
        raise OracleApiError('OSC credentials are not configured')
    return OracleLeadApiClient(oracle_config, username=username, password=password)


def _all_oracle_accounts():
    now = time.monotonic()
    cached = _oracle_account_cache['accounts']
    if (cached is not None and
            now - _oracle_account_cache['loaded_at'] < _ORACLE_ACCOUNT_CACHE_SECONDS):
        return cached
    with _oracle_account_cache_lock:
        now = time.monotonic()
        cached = _oracle_account_cache['accounts']
        if (cached is None or
                now - _oracle_account_cache['loaded_at'] >= _ORACLE_ACCOUNT_CACHE_SECONDS):
            cached = _oracle_client().list_accounts()
            _oracle_account_cache.update(loaded_at=now, accounts=cached)
        return cached


def _parse_reasons(lead):
    raw = lead.get('fraud_reasons')
    if not raw:
        return []
    try:
        reasons = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(reasons, list):
        return []
    informational = {
        'personal_email_domain', 'internal_balseal_reply', 'followup_email',
        'no_engineering_specs',
    }
    normalized = []
    for reason in reasons:
        if not isinstance(reason, dict):
            continue
        reason = dict(reason)
        severity = reason.get('severity')
        if severity not in {'info', 'caution', 'high', 'critical'}:
            severity = 'info' if reason.get('tag') in informational else 'caution'
        reason['severity'] = severity
        normalized.append(reason)
    return normalized


def _sales_address():
    """The shared mailbox's own address, lowercased, or '' if unreadable."""
    try:
        return (outlook_ingest.forward_from_address() or '').strip().lower()
    except (OSError, ValueError):
        return ''


def _other_recipients(recipients):
    """
    Everyone on the original email apart from the Sales mailbox itself.

    Our own address is on every inbound lead by definition, so listing it
    answers the wrong question: what the operator needs before choosing
    reply-all is who ELSE would receive the answer.
    """
    if not recipients:
        return []
    ours = _sales_address()
    others = []
    for kind in ('to', 'cc'):
        for entry in recipients.get(kind) or []:
            address = (entry.get('address') or '').strip()
            name = (entry.get('name') or '').strip()
            if address and address.lower() == ours:
                continue
            if not address and not name:
                continue
            others.append({'name': name, 'address': address, 'kind': kind})
    return others


def _split_addresses(raw):
    """Split a typed-in address field on the separators people actually use."""
    return re.split(r'[,;\s]+', raw or '')


def _clean_addresses(supplied, seen):
    """
    Validate and de-duplicate addresses headed for an outbound message.

    `seen` is shared across the To and CC lists of one message so nobody is
    addressed twice, and carries over between calls: whichever list is
    collected first keeps the address. Raises ValueError naming the first
    address that is not usable, rather than quietly dropping it -- silently
    sending to fewer people than were typed is worse than refusing.
    """
    addresses = []
    for address in (a.strip() for a in supplied):
        if not address or address.lower() in seen:
            continue
        if not EMAIL_ADDRESS_RE.fullmatch(address) or '\r' in address or '\n' in address:
            raise ValueError(address)
        seen.add(address.lower())
        addresses.append(address)
    return addresses


def _sent_to_label(to_addresses, cc_addresses):
    """'a@x.com, b@x.com (cc c@x.com)' -- who a message actually went to."""
    label = ', '.join(to_addresses)
    if cc_addresses:
        label += f' (cc {", ".join(cc_addresses)})'
    return label


def _recipients_label(others):
    """One human-readable line naming everyone else on the email."""
    def render(entry):
        name, address = entry['name'], entry['address']
        if name and address and name.lower() != address.lower():
            return f'{name} <{address}>'
        return address or name

    to_line = ', '.join(render(e) for e in others if e['kind'] == 'to')
    cc_line = ', '.join(render(e) for e in others if e['kind'] == 'cc')
    parts = [part for part in (to_line, f'CC: {cc_line}' if cc_line else '') if part]
    return ' · '.join(parts)


def _quoted_history(message):
    """
    The earlier thread quoted inside this message, for the lead page.

    A reply is usually the only place that history exists: the messages it
    answers were often exchanged from a rep's own mailbox, so they never
    reached the Sales mailbox and no amount of looking in Outlook will find
    them. The reply carries them anyway, and clean_email_body() strips them
    from the Details view -- which is why a "Follow-Up Email" could show no
    thread at all.
    """
    if not message:
        return None
    path = Path(
        message.get('translated_body_cache_path') or message.get('body_cache_path') or ''
    )
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None
    # Cached bodies carry a metadata header above this divider.
    divider = ('=' * 60) + '\n\n'
    body = text.split(divider, 1)[1] if divider in text else text
    return email_cleaner.extract_quoted_history(body) or None


def _message_for_lead(lead):
    """The source email row behind a lead, or None."""
    if not lead or not lead.get('message_id'):
        return None
    return db.get_message(lead['message_id'])


# --- Answering an email action ---------------------------------------------
#
# Deleting, forwarding and replying are all things done to a card while working
# down the review queue, so the dialogs there post by fetch and stay where they
# are: the outcome comes back as JSON, becomes a toast, and takes its card with
# it. The same routes still answer a plain form post with the flash-and-redirect
# the lead page relies on -- that page has nowhere else to put the news.

def _wants_json():
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _action_failed(text, category, detail_url):
    """Report an action that did not happen, in the form the caller asked for."""
    if _wants_json():
        # 200 with ok=false, not an error status: the caller falls back to
        # posting the form for real when the request itself fails, and
        # re-sending something Outlook already refused helps nobody.
        return {'ok': False, 'error': text}
    flash(text, category)
    return redirect(detail_url)


def _action_done(text, category, redirect_url, dropped=None):
    """
    Report a finished action. `dropped` names the leads that have left the
    review queue because of it, so the page can clear their cards -- empty for
    a reply, which leaves the lead exactly where it was.
    """
    if _wants_json():
        return {'ok': True, 'message': text, 'dropped': dropped or []}
    flash(text, category)
    return redirect(redirect_url)


@app.route('/leads/<int:lead_id>/attachment/<int:index>')
def lead_attachment(lead_id, index):
    """
    Hand over one attachment from the source email.

    Always as a download, never rendered in the page. These files are whatever
    a stranger chose to email us; the dashboard passing one to the browser to
    display would make it a delivery mechanism for it. Downloading leaves the
    file to Windows and whatever opens it.

    Worth knowing: a file fetched this way does not carry the mark-of-the-web
    an emailed attachment does, so Office opens it without Protected View.
    Opening the email in Outlook instead (the button beside this one) keeps
    that protection.
    """
    lead = db.get_lead(lead_id)
    if not lead:
        abort(404)
    message = _message_for_lead(lead)
    if not message or not message.get('source_message_id'):
        abort(404)

    # Bounded by what was recorded at ingest, so a guessed index cannot reach
    # anything the reviewer was not already shown.
    recorded = db.get_message_attachments(lead['message_id'])
    if not 0 <= index < len(recorded):
        abort(404)

    try:
        path, filename = outlook_ingest.save_attachment(
            message['source_message_id'], index, ATTACHMENT_CACHE_DIR,
            fallback_message_path=message.get('message_file_path'),
        )
    except Exception:
        app.logger.exception('Could not fetch attachment %s for lead %s', index, lead_id)
        flash(
            'Could not open that attachment. The email may have been moved or '
            'deleted — open it in Outlook to check.',
            'attachment_error',
        )
        return redirect(url_for('lead_detail', lead_id=lead_id))

    response = send_file(path, as_attachment=True, download_name=filename)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.route('/leads/<int:lead_id>/open-in-outlook', methods=['POST'])
def open_lead_in_outlook(lead_id):
    """Open the source email in Outlook, where its attachments keep the
    protections Outlook and the mail gateway apply to them."""
    lead = db.get_lead(lead_id)
    if not lead:
        abort(404)
    message = _message_for_lead(lead)
    if not message or not message.get('source_message_id'):
        flash('The original Outlook email is not available.', 'attachment_error')
        return redirect(url_for('lead_detail', lead_id=lead_id))

    try:
        outlook_ingest.open_in_outlook(message['source_message_id'])
    except Exception:
        app.logger.exception('Could not open email in Outlook for lead %s', lead_id)
        flash(
            'Outlook could not open that email. Check that Outlook is running, '
            'and that the email is still in the Sales inbox.',
            'attachment_error',
        )
        return redirect(url_for('lead_detail', lead_id=lead_id))

    flash('Opened in Outlook.', 'attachment_success')
    return redirect(url_for('lead_detail', lead_id=lead_id))


def _reply_candidates(lead):
    """
    Everyone a reply to this lead could sensibly go to: whoever wrote each
    message in the thread, plus anyone the original was addressed to.

    Ordered newest-sender first so the person most likely to be answered is at
    the top, de-duplicated by address, and never including the Sales mailbox
    itself -- replying to ourselves is not a choice worth offering.
    """
    ours = _sales_address()
    candidates = {}

    for message in reversed(lead.get('email_thread') or []):
        address = (message.get('sender_email') or '').strip()
        if not address or address.lower() == ours:
            continue
        candidates.setdefault(address.lower(), {
            'address': address,
            'name': (message.get('sender') or '').split('<')[0].strip(),
        })

    for entry in lead.get('other_recipients') or []:
        address = (entry.get('address') or '').strip()
        if not address or address.lower() == ours:
            continue
        candidates.setdefault(address.lower(), {
            'address': address,
            'name': entry.get('name') or '',
        })

    return list(candidates.values())


def _newest_first(lead):
    """
    Sort key putting the most recently received email first, the way the
    Outlook inbox is ordered.

    Timestamps arrive both with and without a timezone (Outlook supplies one,
    a lead's created_at fallback may not), and Python refuses to compare the
    two -- so a naive value is read as UTC rather than crashing the page.
    Anything undateable sorts last instead of jumping to the top.
    """
    raw = lead.get('received_at_raw')
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _format_timestamp(raw):
    """ISO timestamp -> 'Jul 23, 2026 11:08 AM', or the raw value if it will
    not parse. Never raises: a display string is not worth failing a page for."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).strftime('%b %d, %Y %I:%M %p')
    except (AttributeError, ValueError):
        return str(raw)


def _finished_at(lead):
    """
    When a History lead finished, mirroring db.LEAD_ORDERS['finished'].

    The column History sorts on has to be the column History shows, or the
    order looks arbitrary to the person reading it. Kept in step with the SQL
    by tests/test_review_order.py, which asserts the rendered dates descend.
    """
    return (
        lead.get('submitted_at')
        or lead.get('reviewed_at')
        or lead.get('updated_at')
        or lead.get('created_at')
    )


# How long a lead may sit claimed by a submit run before the queue offers a
# way out of it. A real submission takes seconds; anything past this means the
# run that claimed it is gone. Well under db.release_stale_submission_claims()'s
# 90 minutes, which only fires when some later run happens to start -- and
# nothing here runs on a timer, so that may be never.
_STUCK_SENDING_MINUTES = 15


def _sending_since(lead):
    """When the current submit attempt claimed this lead, for display."""
    if lead.get('status') != 'submitting':
        return None
    return _format_timestamp(lead.get('submitting_started_at'))


def _sending_is_stuck(lead):
    """Whether a claimed lead has been claimed long enough to be abandoned."""
    if lead.get('status') != 'submitting':
        return False
    started = lead.get('submitting_started_at')
    if not started:
        return True
    try:
        claimed = datetime.fromisoformat(str(started).replace('Z', '+00:00'))
    except ValueError:
        return True
    if not claimed.tzinfo:
        claimed = claimed.replace(tzinfo=timezone.utc)
    return claimed < (datetime.now(timezone.utc)
                      - timedelta(minutes=_STUCK_SENDING_MINUTES))


def _decorate(lead):
    """
    Adds display-only derived fields for the lead-info table. Deliberately
    does NOT surface the raw email/form body text anywhere -- the "Details"
    row runs lead_details through email_cleaner (strips tracking/SafeLinks
    URLs, mailto links, embedded-image references, security/automated-system
    banners, and disclaimers -- see email_cleaner.py) and then condenses it
    with paragraph breaks preserved (see condense_paragraphs), so nothing
    resembling the original email or web-form layout is ever rendered -- just
    the actual content, with the message and signature visibly separated.

    pipeline_ingest.py now cleans lead_details the same way *before* it's ever
    stored (so the dashboard and what actually gets typed into Oracle match --
    they didn't for a while, which was a real bug, not a design choice: the
    dashboard showed a cleaned preview while the raw/dirty version was what
    actually got submitted). The clean_email_body() call here is a defensive
    backstop for leads from before that fix, or any future source that skips
    pipeline_ingest.py -- harmless no-op on text that's already clean.
    """
    lead = dict(lead)
    lead['reasons'] = _parse_reasons(lead)
    lead['display_name'] = (
        f"{lead.get('first_name') or ''} {lead.get('last_name') or ''}".strip()
        or lead.get('organization') or '(no name)'
    )
    location_parts = [p for p in (lead.get('city'), lead.get('state'), lead.get('country')) if p]
    lead['location'] = ', '.join(location_parts) if location_parts else None
    lead['details_flat'] = None
    if lead.get('lead_details'):
        cleaned = email_cleaner.clean_email_body(lead['lead_details'])
        lead['details_flat'] = email_cleaner.condense_paragraphs(cleaned) if cleaned else None
    # Reply, Forward and Delete all act on an Outlook message. A lead entered
    # by hand (manual_lead.py) has none, so those buttons are not offered on
    # it -- every one of them would fail, and two of them say "email" in the
    # confirmation dialog for an email that does not exist.
    lead['has_source_email'] = bool(lead.get('message_id'))
    lead['email_thread'] = db.get_lead_email_thread(lead['id'])
    lead['has_followup'] = len(lead['email_thread']) > 1
    # Each message is stamped with when it arrived: the thread is read top to
    # bottom as a sequence, and a card with no date leaves its place in that
    # sequence to be inferred.
    for message in lead['email_thread']:
        message['received_at_display'] = _format_timestamp(message.get('received_at'))
    # Replies the operator sent from the dashboard. Shown alongside the lead
    # because they are the one part of the conversation ingestion cannot see.
    lead['replies'] = db.list_lead_replies(lead['id'])
    for reply in lead['replies']:
        reply['sent_at_display'] = _format_timestamp(reply.get('sent_at'))
    source_sender = None
    if lead['email_thread']:
        source_sender = lead['email_thread'][0].get('sender_email')
    lead['reply_to_address'] = source_sender or lead.get('email')
    # How many leads share this lead's source email -- the delete-email confirm
    # dialog warns when deleting would drop more than the one being looked at.
    lead['message_lead_count'] = (
        len(db.list_leads_by_message(lead['message_id'])) if lead.get('message_id') else 1
    )

    # Only an http(s) URL becomes a clickable link in the template. The field
    # is editable on the review form, so this decides here rather than trusting
    # whatever is stored -- a "javascript:" value typed into that box would
    # otherwise become a live link on the reviewer's own dashboard.
    # Metadata only, and rendered as inert text -- the files are never stored
    # or served. A customer's attachment is a stranger's file; describing it is
    # useful, hosting it would make the dashboard a delivery mechanism for it.
    lead['attachments'] = db.get_message_attachments(lead.get('message_id'))
    source_message = db.get_message(lead.get('message_id')) if lead.get('message_id') else None
    lead['translated_from'] = (
        source_message.get('original_language')
        if source_message and source_message.get('translated_body_cache_path') else None
    )

    # Who else the original email was addressed to, for the reply dialog.
    # None means it was never recorded (a message ingested before this was
    # captured); the dialog then looks it up from Outlook when opened, rather
    # than implying the customer emailed us alone.
    stored_recipients = db.parse_message_recipients(
        source_message.get('recipients') if source_message else None
    )
    lead['recipients_known'] = stored_recipients is not None
    lead['other_recipients'] = _other_recipients(stored_recipients)
    lead['other_recipients_label'] = _recipients_label(lead['other_recipients'])

    if lead['other_recipients_label']:
        lead['recipients_display'] = lead['other_recipients_label']
    elif lead['recipients_known']:
        lead['recipients_display'] = 'None (Sales inbox only)'
    elif lead.get('source') in ('email_excel_attachment', spreadsheet_import.SOURCE):
        lead['recipients_display'] = 'N/A (Spreadsheet upload)'
    elif lead.get('source') == manual_lead_module.SOURCE:
        lead['recipients_display'] = 'N/A (Entered by hand)'
    else:
        lead['recipients_display'] = 'Not recorded'

    # Everyone a reply could go to, for the dialog's tick-list. The person
    # being replied to is ticked; everyone else starts off, because answering
    # a colleague's follow-up should not copy in the customer by default.
    lead['reply_candidates'] = _reply_candidates(lead)

    # The rest of the conversation, when the earlier messages never reached the
    # Sales mailbox and only exist quoted inside this reply.
    lead['quoted_history'] = _quoted_history(source_message)

    # ...but only worth showing when it is the ONLY copy. Once the thread holds
    # more than one message, this block is those same messages again -- each
    # reply quotes everything before it -- so rendering both printed the
    # conversation twice, interleaved: message, then a copy of the message
    # above it, then the next message. The thread cards are the readable
    # version; this is the fallback for a reply that arrived alone.
    lead['show_quoted_history'] = bool(
        lead['quoted_history'] and len(lead['email_thread']) <= 1
    )

    # Who to talk to about this lead. Worked out here for display rather than
    # stored on the lead, so it cannot end up in an Oracle payload: Oracle's
    # Owner is the Territory Manager above, and this is reference only.
    lead['sales_rep'], lead['sales_rep_basis'] = territory_map.assign_crr(
        lead, lead.get('assigned_tm')
    )

    website = (lead.get('website') or '').strip()
    lead['website_url'] = website if website.lower().startswith(('http://', 'https://')) else None

    # When THIS email arrived -- the lead's own message first. The thread's
    # first entry is the oldest message in the conversation, which since
    # earlier messages are kept as context is usually not this email at all:
    # a reply to a week-old thread would date itself to the week-old original,
    # and sort as though it arrived then.
    raw_date = (
        (source_message or {}).get('received_at')
        or (lead['email_thread'][0].get('received_at') if lead['email_thread'] else None)
        or lead.get('created_at')
    )
    lead['received_at_raw'] = raw_date

    if raw_date:
        try:
            # Parse ISO date string and format nicely (e.g., Jul 23, 2026 11:08 AM)
            dt = datetime.fromisoformat(raw_date.replace('Z', '+00:00'))
            lead['received_date_formatted'] = dt.strftime('%b %d, %Y %I:%M %p')
            lead['received_date_short'] = dt.strftime('%b %d, %Y')
        except Exception:
            lead['received_date_formatted'] = raw_date[:16].replace('T', ' ')
            lead['received_date_short'] = raw_date[:10]
    else:
        lead['received_date_formatted'] = None
        lead['received_date_short'] = None

    return lead


_ROUTING_INPUT_FIELDS = {'state', 'zip', 'country'}


def _refresh_territory_assignment(current, edited):
    """Keep owner and routing explanation synchronized with form edits.

    The Territory Manager dropdown is available for exceptional manual
    assignments. A changed value is an explicit override; otherwise any
    geography change recalculates both the manager and its explanation.
    """
    old_tm = (current.get('assigned_tm') or '').strip()
    posted_tm = (edited.get('assigned_tm') or '').strip() \
        if 'assigned_tm' in edited else old_tm
    if 'assigned_tm' in edited and posted_tm != old_tm:
        edited['assigned_tm'] = posted_tm or None
        edited['assigned_tm_basis'] = 'Manual override in dashboard'
        return

    location_changed = any(
        field in edited
        and (edited.get(field) or '').strip() != (current.get(field) or '').strip()
        for field in _ROUTING_INPUT_FIELDS
    )
    if location_changed:
        candidate = dict(current)
        candidate.update(edited)
        tm, basis = territory_map.assign_tm(candidate)
        edited['assigned_tm'] = tm
        edited['assigned_tm_basis'] = basis


def _parse_warnings(lead):
    raw = lead.get('field_warnings')
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return [str(raw)]
    return parsed if isinstance(parsed, list) else [str(parsed)]


def _lead_label(lead):
    """How a lead is named in a message to the operator."""
    return (
        f"{lead.get('first_name') or ''} {lead.get('last_name') or ''}".strip()
        or lead.get('organization') or f"Lead #{lead.get('id')}"
    )


def _extract_company_candidates(text):
    """
    Extract candidate company names from warning/error text, e.g.
    "resembles 2 existing Oracle account(s) ('Acme Corp', 'Acme Corporation')."

    The names are read back out of the quotes repr() put them in, rather than
    by reading up to the next bracket and splitting on commas. Oracle account
    names routinely contain both: 'LOCKHEED MARTIN (1389119)' came back as
    'LOCKHEED MARTIN (1389119', and assigning a lead to a name no account has
    put it straight back into the same ambiguous-match block -- the loop the
    Assign button was meant to end. repr() switches to double quotes for a
    name holding an apostrophe ("O'BRIEN MEDICAL"), so both quotings count.
    """
    if not text:
        return []
    listing = re.search(r'account\(s\)\s*\(', text)
    if not listing:
        return []
    names = re.findall(r"'([^']*)'|\"([^\"]*)\"", text[listing.end():])
    return [(single or double).strip()
            for single, double in names
            if (single or double).strip() and not (single or double).startswith('+')]


app.jinja_env.filters['extract_company_candidates'] = _extract_company_candidates


def _build_alert(lead):
    """
    Turn a lead needing attention into a notification the operator can act on.

    'severity' drives the colour: 'blocked' means nothing new reached Oracle,
    'warning' means it did but something about it needs checking.
    """
    warnings = _parse_warnings(lead)
    name = (
        f"{lead.get('first_name') or ''} {lead.get('last_name') or ''}".strip()
        or lead.get('organization') or f"Lead #{lead['id']}"
    )
    status = lead.get('status')

    # Blocked locally: an equivalent lead had already gone to Oracle. Checked
    # before Oracle is contacted, because Oracle's own dedup cannot see leads
    # it has not yet assigned an owner to.
    if status == 'duplicate':
        return {
            'lead_id': lead['id'], 'name': name, 'severity': 'blocked',
            'headline': 'Not submitted — duplicate of an earlier lead',
            'detail': '; '.join(warnings) or 'An equivalent lead was already sent to Oracle.',
            'organization': lead.get('organization'),
            'can_send_anyway': True,
        }

    # Oracle's pre-submission dedup matched an existing lead, so nothing new
    # was created -- the case that used to leave no trace on the dashboard.
    existing = next((w for w in warnings if 'already exists' in w.lower()), None)
    if status == 'submitted' and existing:
        return {
            'lead_id': lead['id'], 'name': name, 'severity': 'blocked',
            'headline': 'Not submitted — already in Oracle',
            'detail': existing,
            'organization': lead.get('organization'),
            'can_send_anyway': True,
        }
    # Sent back by return_lead_for_correction(): it is waiting in Pending
    # Review, editable, with the reason on its card. Headline says so, because
    # "Submission failed" reads as something the operator has lost.
    if status == 'pending_review' and (lead.get('submit_error') or '').strip():
        err = lead.get('submit_error') or ''
        ambiguous = 'resembles' in err
        headline = 'Ambiguous company match — choose action' if ambiguous else 'Returned to Pending Review — needs a correction'
        candidates = _extract_company_candidates(err)
        return {
            'lead_id': lead['id'], 'name': name, 'severity': 'warning',
            'headline': headline,
            'detail': err or 'No error detail recorded.',
            'organization': lead.get('organization'),
            'can_send_anyway': ambiguous,
            'candidates': candidates,
        }
    if status == 'failed':
        return {
            'lead_id': lead['id'], 'name': name, 'severity': 'blocked',
            'headline': 'Submission failed',
            'detail': lead.get('submit_error') or 'No error detail recorded.',
            'organization': lead.get('organization'),
        }
    if status == 'submission_unknown':
        return {
            'lead_id': lead['id'], 'name': name, 'severity': 'blocked',
            'headline': 'Outcome unknown — check Oracle',
            'detail': lead.get('submit_error') or 'No error detail recorded.',
            'organization': lead.get('organization'),
        }
    if warnings:
        # An ambiguous company match is the one warning that asks the operator
        # to go and do something in Oracle, so name it rather than burying it
        # under the generic heading.
        ambiguous = any('resembles' in w and 'existing Oracle' in w for w in warnings)
        # A rejected website field is a configuration fault, not a fact about
        # this lead: it repeats on every lead until oracle.website_field is
        # fixed. Naming it says what to go and change, instead of leaving the
        # operator to read the same generic warning off a dozen leads.
        website_rejected = any('website field' in w.lower() for w in warnings)
        if ambiguous:
            headline = 'Possible duplicate account — check Oracle'
        elif website_rejected:
            headline = 'Website field rejected — check config.json'
        else:
            headline = 'Submitted with warnings'
        return {
            'lead_id': lead['id'], 'name': name, 'severity': 'warning',
            'headline': headline,
            'detail': '; '.join(warnings),
            'organization': lead.get('organization'),
            'can_send_anyway': ambiguous,
        }
    return None


def _csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


@app.before_request
def protect_post_requests():
    if request.method != 'POST':
        return None
    supplied = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token')
    if not supplied or not secrets.compare_digest(supplied, session.get('_csrf_token', '')):
        abort(400, 'Invalid or missing CSRF token')


def _content_security_policy(frame_ancestors="'none'"):
    """The dashboard's CSP. `frame_ancestors` is a parameter because one route
    -- reference_file() -- has to allow the page to frame its own PDFs; see the
    note there. Everything else is identical for every response, so the policy
    lives in one place rather than being restated per route and drifting."""
    return (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; "
        f"frame-ancestors {frame_ancestors}; form-action 'self'"
    )


@app.after_request
def add_security_headers(response):
    """Browser hardening for pages containing customer and employee PII.

    setdefault throughout: a route that has already set one of these has a
    reason to, and it must not be overwritten here.
    """
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    response.headers.setdefault('Content-Security-Policy', _content_security_policy())
    response.headers.setdefault('Cache-Control', 'no-store')
    return response


@app.context_processor
def inject_counts():
    token = _csrf_token()
    # Hide the delete control entirely when mailbox deletion is switched off,
    # rather than offering a button that can only ever flash an error.
    delete_enabled = outlook_ingest.deletion_enabled()
    # Same reasoning as delete_enabled: with the lookup switched off, or no AI
    # key to run it, the Find HQ button is not offered at all.
    hq_lookup_enabled = hq_lookup.is_enabled()
    try:
        from_address = outlook_ingest.forward_from_address()
    except (OSError, ValueError):
        from_address = 'the Sales mailbox'
    try:
        alerts = [a for a in (_build_alert(l) for l in db.list_submission_alerts()) if a]
        return {
            # Counted from the same lists the pages themselves render, so a
            # nav badge can never disagree with the page it points at.
            'staged_count': db.count_leads(lead_stages.STAGED),
            'ready_count': db.count_leads(lead_stages.SENDING),
            'pending_count': db.count_leads(lead_stages.PENDING),
            'alerts': alerts,
            'alert_count': len(alerts),
            'blocked_count': sum(1 for a in alerts if a['severity'] == 'blocked'),
            'csrf_token': token,
            'delete_enabled': delete_enabled,
            'forward_from_address': from_address,
            'known_addresses': db.list_known_addresses(),
            'hq_lookup_enabled': hq_lookup_enabled,
            'returnable_statuses': lead_stages.RETURNABLE,
            'recallable_statuses': lead_stages.RECALLABLE,
            'editable_statuses': lead_stages.EDITABLE,
        }
    except Exception:
        return dict(staged_count=0, ready_count=0, pending_count=0,
                    alerts=[], alert_count=0, blocked_count=0, csrf_token=token,
                    delete_enabled=delete_enabled, forward_from_address=from_address,
                    known_addresses=[], hq_lookup_enabled=hq_lookup_enabled,
                    returnable_statuses=lead_stages.RETURNABLE,
                    recallable_statuses=lead_stages.RECALLABLE,
                    editable_statuses=lead_stages.EDITABLE)


@app.route('/alerts/<int:lead_id>/dismiss', methods=['POST'])
def dismiss_alert(lead_id):
    if not db.get_lead(lead_id):
        abort(404)
    db.dismiss_lead_alert(lead_id)
    return redirect(request.referrer or url_for('index'))


@app.route('/alerts/dismiss_all', methods=['POST'])
def dismiss_all_alerts():
    db.dismiss_all_lead_alerts()
    return redirect(request.referrer or url_for('index'))


@app.route('/leads/<int:lead_id>/send-anyway', methods=['POST'])
def send_anyway(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        abort(404)
    db.mark_lead_for_force_submit(lead_id)
    _start_submit_run()
    flash(
        f'{_lead_label(lead)} will be submitted to Oracle as a new company (bypassing duplicate checks).',
        'submit_success',
    )
    return redirect(request.referrer or url_for('index'))


@app.route('/leads/<int:lead_id>/assign_company', methods=['POST'])
def assign_company(lead_id):
    """The other answer to an ambiguous company match: use the account Oracle
    already has, instead of creating a second one.

    The lead is renamed to that account's exact name, because an exact name is
    what resolve_existing_party() links on -- anything that merely resembles an
    account is what blocked the lead in the first place. So the name has to
    arrive here intact; _extract_company_candidates() truncating one at a
    bracket is what made this button loop.

    Sends straight to Oracle, like "Create as New Company" beside it: the
    operator has just answered the only question holding the lead up, and
    parking it in the Input Queue for a second confirmation only looked like
    the click had done nothing.
    """
    lead = db.get_lead(lead_id)
    if not lead:
        abort(404)
    company_name = request.form.get('company_name', '').strip()
    if not company_name:
        flash('Please select or specify a valid company name.', 'assign_error')
        return redirect(request.referrer or url_for('index'))

    status = (lead.get('status') or '').strip()
    if status not in lead_stages.EDITABLE and status not in lead_stages.RECALLABLE:
        # 'submitting' above all: a run may be mid-POST with this lead, and
        # renaming its company underneath it is the one edit that cannot be
        # undone from here.
        flash(
            f'{_lead_label(lead)} cannot be reassigned from its current state '
            f'({lead_stages.page_of(status) or status}).',
            'submit_error',
        )
        return redirect(request.referrer or url_for('index'))

    db.update_lead_fields(lead_id, {
        'organization': company_name,
        # This legacy warning action selects by name only.  Never let a party
        # ID chosen earlier for some other name ride along underneath it.
        'oracle_account_name': None,
        'oracle_account_party_id': None,
        'oracle_account_party_number': None,
        'submit_error': None,
        'force_submit': 0,
    })

    # The same gate every other route to Oracle passes through: the rename can
    # be the edit that completes a lead, and it can also be made on a lead that
    # was never sendable for an unrelated reason.
    blockers = submission_blockers(dict(lead, organization=company_name))
    if blockers:
        db.return_lead_for_correction(lead_id, 'Cannot approve: ' + ' '.join(blockers))
        flash(
            f'{_lead_label(lead)} is assigned to {company_name!r} but is back in '
            f'Pending Review for a correction: {" ".join(blockers)}',
            'submit_error',
        )
        return redirect(request.referrer or url_for('index'))

    if status in lead_stages.EDITABLE:
        db.approve_lead(
            lead_id,
            reviewed_by=request.form.get('reviewed_by') or 'dashboard',
            review_notes=f'Assigned to existing Oracle company {company_name!r}',
            status='review_approved',
        )
        db.finalize_approved_batch(reviewed_by='dashboard', lead_ids=[lead_id])
    else:
        # Already past staging ('approved' or 'failed' in the Input Queue):
        # finalize_approved_batch() would not see it, and leaving it alone left
        # a failed lead sitting with its old error.
        db.update_lead_fields(lead_id, {
            'status': 'approved',
            'submitting_started_at': None,
            'next_attempt_at': None,
        })
    db.dismiss_lead_alert(lead_id)
    review_marking.maybe_mark_email_reviewed(lead_id)

    if not _start_submit_run():
        flash(
            f'{_lead_label(lead)} is assigned to {company_name!r} and approved, but '
            'the Oracle submission could not be started. Use "Send to Oracle now".',
            'submit_error',
        )
        return redirect(request.referrer or url_for('index'))
    flash(
        f'{_lead_label(lead)} assigned to existing company {company_name!r} and sent to Oracle.',
        'submit_success',
    )
    return redirect(request.referrer or url_for('index'))


def _pending_folders():
    """Every lead awaiting review, split into Pending Review's (inbox, spam).

    lead_stages.PENDING, not 'pending_review' alone: a lead whose ingest run
    died between being written down and being screened is left in 'ingested'
    or 'extracted', and used to appear on no page at all. It needs a review
    like any other, so it belongs here -- in whichever folder it falls in.
    """
    leads = db.list_leads(status=lead_stages.PENDING)
    senders = db.message_sender_emails(l.get('message_id') for l in leads)
    inbox, spam = [], []
    for lead in leads:
        sender = senders.get(lead.get('message_id'))
        (spam if spam_folder.is_spam(lead, sender) else inbox).append(lead)
    return inbox, spam


@app.route('/')
def index():
    """
    Pending Review, ordered like the Outlook inbox: newest email at the top.

    Sorted by when the email arrived rather than by fraud risk, which is what
    this used to do -- reviewing follows the mailbox, and a lead's position
    matching the email above it is worth more than risky ones floating up.
    The risk flags are still on every card.

    ?folder=spam shows the Spam folder instead: leads from Gmail addresses,
    reviewed the same way but disposable in one go. See spam_folder.py.
    """
    folder = request.args.get('folder')
    if folder not in spam_folder.FOLDERS:
        folder = spam_folder.INBOX
    inbox, spam = _pending_folders()
    leads = spam if folder == spam_folder.SPAM else inbox
    leads = [_decorate(l) for l in leads]
    leads.sort(key=_newest_first, reverse=True)
    ingest_lease = db.get_run_lease(_INGEST_JOB_KEY) or {}
    if ingest_lease.get('status') not in ('starting', 'running'):
        ingest_lease = None
    return render_template(
        'index.html', leads=leads, ingest_lease=ingest_lease, folder=folder,
        inbox_count=len(inbox), spam_count=len(spam),
        pending_total=len(inbox) + len(spam),
    )


@app.route('/spam/disqualify_all', methods=['POST'])
def disqualify_spam():
    """Reject every lead in the Spam folder and delete their emails.

    Acts only on the leads the reviewer was looking at (the form posts their
    ids) and only on those still in Spam and still awaiting review now: a lead
    that arrived after the page loaded has not been seen, and one approved in
    another tab has been decided.

    Each email goes to the Sales mailbox's Deleted Items, the same recoverable
    delete as a card's Delete button (outlook_ingest.delete_message). One
    email can produce several leads; when any of its other leads is still
    being reviewed outside Spam, the Spam leads are rejected but the email is
    left in the inbox, so that other lead can still be checked against it.

    An Outlook failure stops the run where it is rather than trying every
    remaining email against an Outlook that is not answering. Everything
    before that point is done; everything after is still in Spam.
    """
    folder_url = url_for('index', folder=spam_folder.SPAM)
    if not outlook_ingest.deletion_enabled():
        return _action_failed(
            'Deleting email is switched off in config.json '
            '(outlook.allow_email_delete), so nothing was disqualified.',
            'spam_error', folder_url,
        )

    shown = set()
    for raw in request.form.getlist('lead_id'):
        try:
            shown.add(int(raw))
        except ValueError:
            continue
    _, spam = _pending_folders()
    chosen = [l for l in spam if l['id'] in shown]
    if not chosen:
        return _action_failed('There are no leads in Spam to disqualify.', 'spam_error', folder_url)
    chosen_ids = {l['id'] for l in chosen}

    by_message = {}
    for lead in chosen:
        by_message.setdefault(lead['message_id'], []).append(lead['id'])

    deleted_note = 'Disqualified from the Spam folder (Gmail); email deleted from the Sales inbox.'
    kept_note = ('Disqualified from the Spam folder (Gmail); email kept because '
                 'another lead from it is still being reviewed.')

    def _reject(lead_ids, note):
        # Re-read just before writing: Approve in another tab between the
        # folder being listed and this point must win.
        done = []
        for lead_id in lead_ids:
            current = db.get_lead(lead_id)
            if current and current['status'] in lead_stages.PENDING:
                db.reject_lead(lead_id, reviewed_by='dashboard', review_notes=note)
                done.append(lead_id)
        return done

    dropped, emails_deleted, emails_kept, not_in_mailbox = [], 0, 0, 0
    stopped = None
    for message_id, lead_ids in by_message.items():
        others = [
            s for s in db.list_leads_by_message(message_id)
            if s['status'] in lead_stages.EDITABLE and s['id'] not in chosen_ids
        ]
        message = db.get_message(message_id)
        if others:
            done = _reject(lead_ids, kept_note)
            dropped += done
            emails_kept += 1
            if done:
                review_marking.maybe_mark_email_reviewed(done[0])
            continue
        if not message or not message.get('source_message_id'):
            # Nothing in Outlook to delete; the leads go all the same.
            dropped += _reject(lead_ids, deleted_note)
            continue

        action_id = db.begin_outlook_action(
            lead_ids[0], 'delete', message_id=message_id,
            details={'source_message_id': message['source_message_id'],
                     'via': 'spam_disqualify_all'},
        )
        try:
            deleted = outlook_ingest.delete_message(message['source_message_id'])
        except ValueError as exc:
            # This one message is not in the Sales mailbox. Leave its leads in
            # Spam and carry on with the rest.
            db.finish_outlook_action(action_id, 'failed', str(exc))
            not_in_mailbox += len(lead_ids)
            continue
        except outlook_ingest.OutlookDeleteError as exc:
            db.finish_outlook_action(action_id, 'failed', str(exc))
            app.logger.exception('Disqualify all: could not delete message %s', message_id)
            stopped = f'Outlook failed while {exc.stage}, so it stopped there.'
            break
        except Exception:
            db.finish_outlook_action(
                action_id, 'unknown',
                'Unexpected Outlook error; verify the message before trying again.',
            )
            app.logger.exception('Disqualify all: could not delete message %s', message_id)
            stopped = ('Outlook could not delete an email, so it stopped there. Check '
                       'that Outlook is open and signed in, then try again.')
            break
        db.finish_outlook_action(action_id, 'deleted')
        dropped += _reject(lead_ids, deleted_note)
        if deleted:
            emails_deleted += 1

    if not dropped:
        return _action_failed(
            stopped or 'None of the leads in Spam could be disqualified.',
            'spam_error', folder_url,
        )

    def _n(count, word):
        return f'{count} {word}{"" if count == 1 else "s"}'

    text = (f'Disqualified {_n(len(dropped), "lead")} and deleted '
            f'{_n(emails_deleted, "email")} from the Sales inbox.')
    if emails_kept:
        text += (f' {_n(emails_kept, "email")} left in the inbox because another '
                 'lead from the same email is still being reviewed.')
    if not_in_mailbox:
        text += (f' {_n(not_in_mailbox, "lead")} left in Spam: '
                 'the email is not in the Sales mailbox.')
    if stopped:
        remaining = len(chosen_ids) - len(dropped) - not_in_mailbox
        text += f' {stopped} {_n(remaining, "lead")} still in Spam.'
    return _action_done(text, 'spam_success', folder_url, dropped=dropped)


@app.route('/ingest', methods=['POST'])
def ingest_now():
    """
    Read the Sales inbox. This is the only thing that ingests email.

    Started as a detached process and answered immediately: a run reads the
    Sales inbox and puts each email through the extractor, which takes long
    enough that holding the request open would just time out the browser. The
    new leads appear on this page as the run works through them.
    """
    lease_token = db.acquire_run_lease(_INGEST_JOB_KEY)
    if not lease_token:
        message = 'An ingest run is already going. New leads will appear automatically.'
        if _wants_json():
            return {'ok': False, 'error': message}
        flash(message, 'ingest_error')
        return redirect(url_for('index'))

    import subprocess
    try:
        child_env = os.environ.copy()
        child_env['PIPELINE_INGEST_LEASE_TOKEN'] = lease_token
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve().parent.parent / 'pipeline_ingest.py')],
            cwd=str(Path(__file__).resolve().parent.parent), env=child_env,
        )
        if isinstance(child.pid, int):
            db.update_run_lease(
                _INGEST_JOB_KEY, lease_token, 'starting', pid=child.pid,
            )
    except OSError:
        db.update_run_lease(_INGEST_JOB_KEY, lease_token, 'failed', 'Process could not start')
        app.logger.exception('Could not start a manual ingest run')
        message = 'Could not start the ingest run. Check the dashboard log.'
        if _wants_json():
            return {'ok': False, 'error': message}
        flash(message, 'ingest_error')
        return redirect(url_for('index'))

    message = 'Ingesting leads from the Sales inbox. New leads will appear automatically.'
    if _wants_json():
        return {'ok': True, 'message': message}
    flash(message, 'ingest_success')
    return redirect(url_for('index'))


@app.route('/ingest/reset', methods=['POST'])
def reset_ingest():
    """Recover an ingest claim whose worker was forcibly terminated."""
    if db.reset_orphaned_run_lease(_INGEST_JOB_KEY):
        message = 'Cleared the orphaned ingest run. You can start ingestion again.'
        category = 'ingest_success'
    elif db.run_lease_process_is_running(_INGEST_JOB_KEY):
        message = 'The ingestion worker is still running, so its lock was not reset.'
        category = 'ingest_error'
    else:
        message = 'There is no active ingest run to reset.'
        category = 'ingest_error'
    if _wants_json():
        return {'ok': category == 'ingest_success',
                'message' if category == 'ingest_success' else 'error': message}
    flash(message, category)
    return redirect(url_for('index'))


@app.route('/ingest/status')
def ingest_status():
    """Return JSON status indicating whether lead ingestion is actively running."""
    lease = db.get_run_lease(_INGEST_JOB_KEY) or {}
    running = lease.get('status') in ('starting', 'running')
    return {
        'running': running,
        'status': lease.get('status') if running else None,
        'pid': lease.get('pid') if running else None,
    }


@app.route('/leads/new')
def new_lead():
    """The form for entering a lead that never arrived as an email."""
    if not (secrets_store.get_secret('OPENAI_API_KEY') or secrets_store.get_secret('CHATGPT_API')):
        flash('No OpenAI key is configured. Extraction will use basic pattern matching.', 'manual_error')
    return render_template('manual_lead.html',
                           max_images=manual_lead_module.MAX_IMAGES)


@app.route('/leads/new', methods=['POST'])
def create_lead():
    """
    Read what was pasted or attached, and put the lead on Pending Review.

    Synchronous, unlike ingest_now(): that starts a detached run over a whole
    mailbox, this is one model call about one lead and the operator is sitting
    in front of it waiting for the answer. Landing back on Pending Review with
    the new card at the top -- rather than on a preview page of its own -- is
    the point of the feature: from there the lead is reviewed, edited and
    approved through exactly the same controls as an ingested one.
    """
    text = (request.form.get('notes') or '').strip()

    images, rejected = [], []
    for upload in request.files.getlist('screenshot'):
        if not upload or not upload.filename:
            continue
        if len(images) >= manual_lead_module.MAX_IMAGES:
            rejected.append(upload.filename)
            continue
        # The browser's declared type is not trusted to be an image on its own
        # merits -- it is checked so an unreadable upload fails here with a
        # sentence, rather than as an opaque error from the API.
        mime_type = (upload.mimetype or '').lower()
        if mime_type not in manual_lead_module.ACCEPTED_IMAGE_TYPES:
            rejected.append(upload.filename)
            continue
        raw = upload.read()
        if not raw or len(raw) > manual_lead_module.MAX_IMAGE_BYTES:
            rejected.append(upload.filename)
            continue
        images.append((mime_type, raw))

    if not text and not images:
        flash('Paste some details or attach a screenshot first.', 'manual_error')
        return redirect(url_for('new_lead'))

    try:
        lead_id, extraction = manual_lead_module.create_lead_from_notes(
            text, images=images, reviewed_by='dashboard',
        )
    except Exception:
        app.logger.exception('Could not create a lead from pasted notes')
        flash('Could not create the lead. Check the dashboard log.', 'manual_error')
        return redirect(url_for('new_lead'))

    found = len(extraction.get('fields') or {})
    message = (
        f'Lead created from {"a screenshot" if images and not text else "your notes"}'
        f' — {found} field{"" if found == 1 else "s"} read. Check it below before approving.'
        if found else
        'Lead created, but nothing could be read from it automatically. '
        'Open it and fill the fields in by hand.'
    )
    if rejected:
        message += (
            f' {len(rejected)} attachment(s) were skipped — screenshots must be '
            f'a PNG, JPEG, GIF, WebP or BMP under '
            f'{manual_lead_module.MAX_IMAGE_BYTES // (1024 * 1024)} MB, '
            f'and at most {manual_lead_module.MAX_IMAGES} at a time.'
        )
    flash(message, 'manual_success' if found else 'manual_error')
    return redirect(url_for('index', _anchor=f'lead-card-{lead_id}'))


@app.route('/leads/import')
def import_form():
    """The form for turning a spreadsheet of trade-show leads into Pending
    Review cards."""
    if not (secrets_store.get_secret('OPENAI_API_KEY') or secrets_store.get_secret('CHATGPT_API')):
        flash('No OpenAI key is configured. The sheet will be read by its column '
              'headers only, which gets unusual layouts wrong -- check every lead.',
              'import_error')
    return render_template(
        'import_leads.html',
        lead_sources=spreadsheet_import.LEAD_SOURCES,
        default_lead_source=spreadsheet_import.DEFAULT_LEAD_SOURCE,
        max_rows=spreadsheet_import.MAX_ROWS,
        max_mb=spreadsheet_import.MAX_UPLOAD_BYTES // (1024 * 1024),
    )


@app.route('/leads/import', methods=['POST'])
def import_leads():
    """
    Check an uploaded workbook and start importing it in the background.

    Not synchronous like create_lead(): the model reads the sheet ten rows at
    a time, and a few hundred rows is minutes of calls -- longer than a browser
    will hold a request open. The file is checked here, so a wrong one is
    refused at once, and the operator lands on a progress page.
    """
    upload = request.files.get('workbook')
    if not upload or not upload.filename:
        flash('Choose a spreadsheet to import.', 'import_error')
        return redirect(url_for('import_form'))

    # One byte past the limit is enough to know a file is too big. The body
    # is already parsed by now (in memory -- see _Request), bounded by
    # MAX_CONTENT_LENGTH; this only avoids copying all of it a second time.
    data = upload.read(spreadsheet_import.MAX_UPLOAD_BYTES + 1)
    try:
        job_id = spreadsheet_import.start_import(
            data, upload.filename, reviewed_by='dashboard',
            fallback_trade_show=request.form.get('trade_show'),
            lead_source=request.form.get('lead_source'),
        )
    except spreadsheet_import.ImportError_ as exc:
        flash(str(exc), 'import_error')
        return redirect(url_for('import_form'))
    except Exception:
        app.logger.exception('Could not start importing spreadsheet %r', upload.filename)
        flash('Could not import the spreadsheet. Check the dashboard log.', 'import_error')
        return redirect(url_for('import_form'))
    return redirect(url_for('import_progress', job_id=job_id))


@app.route('/leads/import/<job_id>')
def import_progress(job_id):
    """Where the operator waits while a workbook is read. Polls
    import_status() and moves on to import_finish() by itself."""
    job = spreadsheet_import.job_status(job_id)
    if job is None:
        flash('That import is no longer running. Check Pending Review for its leads.',
              'import_error')
        return redirect(url_for('import_form'))
    return render_template('import_progress.html', job=job)


@app.route('/leads/import/<job_id>/status')
def import_status(job_id):
    job = spreadsheet_import.job_status(job_id)
    if job is None:
        return {'state': 'missing'}, 404
    return {key: job[key] for key in ('state', 'stage', 'done', 'total')}


@app.route('/leads/import/<job_id>/finish')
def import_finish(job_id):
    """
    Turn a finished job into a message on the page it belongs on.

    Success goes to Pending Review, where the new leads are checked and
    approved; a failure goes back to the form with the reason.
    """
    job = spreadsheet_import.job_status(job_id)
    if job is None:
        flash('That import is no longer running. Check Pending Review for its leads.',
              'import_error')
        return redirect(url_for('import_form'))
    if job['state'] == 'running':
        return redirect(url_for('import_progress', job_id=job_id))
    if job['state'] == 'failed':
        flash(job['error'], 'import_error')
        return redirect(url_for('import_form'))

    message, created = spreadsheet_import.summarize(job['result'])
    if not created:
        flash(message, 'import_error')
        return redirect(url_for('import_form'))
    flash(message, 'import_success')
    return redirect(url_for('index', _anchor=f"lead-card-{job['result']['lead_ids'][0]}"))


@app.route('/queue')
def queue():
    # Same order as Pending Review, by the email's own date rather than when
    # the row happened to be written, so a lead keeps its place as it moves.
    staged_leads = db.list_leads(status=lead_stages.STAGED)
    staged_leads = [_decorate(l) for l in staged_leads]
    staged_leads.sort(key=_newest_first, reverse=True)

    # 'submitting' as well as 'approved': a lead a submit run has claimed is
    # on its way to Oracle, so it belongs beside the ones still waiting rather
    # than in History, which is for leads that are finished. A run that dies
    # mid-send leaves a lead claimed forever, so each one carries how long it
    # has been going and, past _STUCK_SENDING_MINUTES, a way out (check_oracle).
    ready_leads = db.list_leads(status=lead_stages.SENDING)
    ready_leads = [_decorate(l) for l in ready_leads]
    for lead in ready_leads:
        lead['sending_since'] = _sending_since(lead)
        lead['sending_is_stuck'] = _sending_is_stuck(lead)
        # Why this one will not go, if it will not. A lead that reached
        # 'approved' missing something Oracle requires reads as "waiting for
        # the submission worker" when in fact no worker will ever send it --
        # so say what is wrong here, beside the button that takes it back.
        lead['blockers'] = submission_blockers(lead)
        lead['can_recall'] = lead['status'] in lead_stages.RECALLABLE
    ready_leads.sort(key=_newest_first, reverse=True)

    return render_template('queue.html', leads=staged_leads, ready_leads=ready_leads)


def _start_submit_run():
    """
    Send approved leads to Oracle now.

    Approval is the only thing that puts a lead in front of Oracle, so it is
    also the only thing that starts a run -- there is no scheduled submission
    and nothing uploads on a timer. Every caller here is a human action:
    approving a batch, or explicitly asking for another attempt.

    Deliberately no guard against overlapping runs: pipeline_submit.py claims
    each lead atomically before sending it, so a second run cannot double-send.
    Blocking one would be worse than allowing it -- a run already in flight has
    read its list of leads, so a batch approved a moment later needs its own
    run or it would sit until somebody approved again.
    """
    import subprocess
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve().parent.parent / 'pipeline_submit.py')],
        )
        return True
    except OSError:
        app.logger.exception('Could not start an Oracle submit run')
        return False


def _hold_back_unsendable(leads):
    """Send any lead Oracle would refuse back to Pending Review, and say so.

    Returns the ids held back, keyed by id rather than by label: two inquiries
    from the same person carry the same label, and filtering on it dropped the
    innocent one.
    """
    held_back = {}
    for lead in leads:
        blockers = submission_blockers(lead)
        if blockers:
            db.return_lead_for_correction(
                lead['id'], 'Cannot approve: ' + ' '.join(blockers)
            )
            held_back[lead['id']] = _lead_label(lead)
    if held_back:
        flash(
            f"{len(held_back)} lead{'' if len(held_back) == 1 else 's'} could not be sent "
            f"and {'is' if len(held_back) == 1 else 'are'} back in Pending Review "
            f"for a correction: {', '.join(held_back.values())}.",
            'submit_error',
        )
    return held_back


@app.route('/queue/approve_all', methods=['POST'])
def queue_approve_all():
    staged_leads = db.list_leads(status=lead_stages.STAGED)

    # A lead can be edited after it was staged, so re-check here rather than
    # trusting the check approve() already did. Anything now incomplete is sent
    # back for correction instead of going out and failing at the Oracle call.
    held_back = _hold_back_unsendable(staged_leads)
    if held_back:
        staged_leads = [l for l in staged_leads if l['id'] not in held_back]
        if not staged_leads:
            return redirect(url_for('queue'))

    db.finalize_approved_batch(
        reviewed_by='dashboard', lead_ids=[l['id'] for l in staged_leads],
    )
    for lead in staged_leads:
        review_marking.maybe_mark_email_reviewed(lead['id'])
    if not _start_submit_run():
        flash(
            'The leads are approved, but the Oracle submission could not be '
            'started. Use "Send to Oracle now" to try again.',
            'submit_error',
        )
    # Stay on the Input Queue rather than jumping to History -- the approved
    # leads show up in this page's "ready" section while the submit run works
    # through them, so there's nothing to navigate away for.
    return redirect(url_for('queue'))


@app.route('/queue/submit_now', methods=['POST'])
def queue_submit_now():
    """
    Start a submit run for leads already approved.

    Needed because nothing runs on a timer any more: a lead whose first attempt
    hit a network error is put back to 'approved' with a retry time, and
    without this it would wait for the next batch approval to be picked up.
    """
    # Reset any failed queue leads back to 'approved' so they are picked up for resubmission
    failed_leads = db.list_leads(status='failed')
    for lead in failed_leads:
        db.update_lead_fields(lead['id'], {
            'status': 'approved',
            'submitting_started_at': None,
            'next_attempt_at': None,
        })

    held_back = _hold_back_unsendable(db.list_leads(status='approved'))

    waiting = db.count_leads('approved')
    if not waiting:
        if held_back:
            return redirect(url_for('queue'))
        flash('No approved leads are waiting to go to Oracle.', 'submit_error')
        return redirect(url_for('queue'))
    if not _start_submit_run():
        flash('Could not start the Oracle submission. Check the dashboard log.',
              'submit_error')
        return redirect(url_for('queue'))
    flash(
        f'Sending {waiting} approved lead{"" if waiting == 1 else "s"} to Oracle. '
        'Refresh in a moment to see the result.',
        'submit_success',
    )
    return redirect(url_for('queue'))


@app.route('/queue/<int:lead_id>/unstage', methods=['POST'])
def queue_unstage(lead_id):
    lead = db.get_lead(lead_id)
    if not lead or lead['status'] != 'review_approved':
        abort(409, 'Lead is not staged')
    db.unstage_lead(lead_id)
    return redirect(url_for('queue'))


@app.route('/leads/<int:lead_id>/recall', methods=['POST'])
def recall_lead(lead_id):
    """Take an approved lead back to Pending Review so it can be corrected.

    The way out of the dead end described in db.recall_approved_lead(): an
    approved lead Oracle would refuse had nowhere to go, because the field that
    would fix it is only editable on Pending Review and nothing offered to put
    it back there.

    Lands on the lead page rather than the queue, for the reason approve() does
    when it refuses: the operator clicked this to fix something, and that is
    the form the fix is made on.
    """
    lead = db.get_lead(lead_id)
    if not lead:
        abort(404)
    if lead['status'] not in lead_stages.RECALLABLE:
        abort(409, 'Lead cannot be taken back from its current state')
    if not db.recall_approved_lead(lead_id):
        # A submit run claimed it in the meantime; it is on its way to Oracle
        # and taking it back now would race that request.
        flash(
            f'{_lead_label(lead)} is already being sent to Oracle and cannot be '
            'taken back. Wait for the run to finish, then correct it from where '
            'it lands.',
            'submit_error',
        )
        return redirect(url_for('queue'))
    return redirect(url_for('lead_detail', lead_id=lead_id))


@app.route('/leads/<int:lead_id>')
def lead_detail(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return 'Lead not found', 404
    lead = _decorate(lead)
    duplicate_of = None
    if lead.get('duplicate_of_lead_id'):
        duplicate_of = db.get_lead(lead['duplicate_of_lead_id'])
    return render_template('lead_detail.html', lead=lead, duplicate_of=duplicate_of,
                            schema_fields=db.LEAD_SCHEMA_FIELDS,
                            field_groups=LEAD_FIELD_GROUPS,
                            territory_managers=territory_map.TERRITORY_MANAGERS,
                            lead_source_options=sorted(LEAD_SOURCE_VALUES))


@app.route('/leads/<int:lead_id>/oracle-companies')
def oracle_companies(lead_id):
    """All OSC accounts, fuzzy-ranked for the company currently being edited."""
    lead = db.get_lead(lead_id)
    if not lead:
        abort(404)
    if lead['status'] not in lead_stages.EDITABLE:
        abort(409, 'This lead is no longer editable')
    organization = (request.args.get('q') or lead.get('organization') or '').strip()
    try:
        ranked = rank_company_accounts(organization, _all_oracle_accounts())
    except (OracleApiError, OSError, ValueError, KeyError):
        app.logger.exception('Could not load OSC companies for lead %s', lead_id)
        return jsonify({
            'ok': False,
            'error': 'Could not load companies from OSC. Try again in a moment.',
        }), 503
    return jsonify({
        'ok': True,
        'query': organization,
        'accounts': [
            {
                'party_id': str(account['PartyId']),
                'party_number': str(account.get('PartyNumber') or ''),
                'name': account['OrganizationName'],
                'similarity': round(account['similarity'], 4),
            }
            for account in ranked
        ],
    })


@app.route('/leads/<int:lead_id>/save', methods=['POST'])
def save_lead(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        abort(404)
    if lead['status'] not in lead_stages.EDITABLE:
        abort(409, 'This lead is no longer editable')
    posted_tm = request.form.get('assigned_tm')
    if (posted_tm and
            posted_tm not in territory_map.TERRITORY_MANAGERS):
        abort(400, 'Invalid Territory Manager')
    posted_source = request.form.get('lead_source')
    if posted_source and posted_source not in LEAD_SOURCE_VALUES:
        abort(400, 'Invalid Lead Source')
    edited = {}
    for field in db.LEAD_SCHEMA_FIELDS:
        if field in request.form:
            edited[field] = request.form.get(field) or None

    # The select's option carries the OSC party identity as well as its name.
    # Persist all three together; changing back to free text deliberately
    # clears the old match so it cannot attach a newly typed company to a stale
    # account ID.
    selected_account_id = request.form.get('oracle_account_party_id', '').strip()
    selected_account_name = request.form.get('oracle_account_name', '').strip()
    selected_account_number = request.form.get('oracle_account_party_number', '').strip()
    if selected_account_id:
        if not selected_account_id.isdigit() or not selected_account_name:
            abort(400, 'Invalid OSC company selection')
        edited.update({
            'organization': selected_account_name,
            'oracle_account_name': selected_account_name,
            'oracle_account_party_id': selected_account_id,
            'oracle_account_party_number': selected_account_number or None,
        })
    else:
        edited.update({
            'oracle_account_name': None,
            'oracle_account_party_id': None,
            'oracle_account_party_number': None,
        })
    _refresh_territory_assignment(lead, edited)
    if 'review_notes' in request.form:
        edited['review_notes'] = request.form.get('review_notes') or None
    db.update_lead_fields(lead_id, edited)
    lead = db.get_lead(lead_id)
    if lead and lead.get('status') == 'review_approved':
        return redirect(url_for('queue'))
    return redirect(url_for('index'))


@app.route('/leads/<int:lead_id>/find-hq', methods=['POST'])
def find_hq(lead_id):
    """Research the company's head office and route the lead from it.

    The same lookup ingest runs (hq_lookup.py), offered here as a button
    because the leads that need it are exactly the ones already sitting in the
    queue marked "Unassigned" -- and because a reviewer who has just corrected
    the company or website wants to try again without re-ingesting the email.

    Synchronous: it reads several pages and calls the model, so the request
    can take the best part of a minute. The dashboard is a single-user local
    tool and the button says so.
    """
    lead = db.get_lead(lead_id)
    if not lead:
        abort(404)
    if lead['status'] not in lead_stages.EDITABLE:
        abort(409, 'This lead is no longer editable')
    detail_url = url_for('lead_detail', lead_id=lead_id)
    # Where to land afterwards -- the card list, the queue, or the lead page.
    # Only a path on this site: a posted "next" is still attacker-reachable.
    requested = request.form.get('next') or ''
    back_url = requested if re.match(r'^/[^/\\]', requested) else detail_url

    if not hq_lookup.is_enabled():
        return _action_failed(
            'Headquarters lookup is switched off in config.json, or no OpenAI '
            'key is configured.', 'hq_error', detail_url,
        )

    candidate = dict(lead)
    try:
        written = hq_lookup.apply_headquarters(candidate)
    except Exception:
        app.logger.exception('Headquarters lookup failed for lead %s', lead_id)
        written = {}
    if not written:
        return _action_failed(
            f"Could not establish where {lead.get('organization') or 'this company'} "
            'is based. Nothing on the lead was changed — enter the location by '
            'hand, or check the website on the lead is right and try again.',
            'hq_error', back_url,
        )

    tm, basis = territory_map.assign_tm(candidate)
    fields = dict(written)
    fields['assigned_tm'] = tm
    fields['assigned_tm_basis'] = f"{basis}; location from {candidate['_location_source']}"
    db.update_lead_fields(lead_id, fields)

    where = ', '.join(v for v in (written.get('city'), written.get('state'),
                                  written.get('country')) if v)
    return _action_done(
        f'Head office found: {where}. Territory: {tm}.', 'hq_success', back_url,
    )


@app.route('/leads/<int:lead_id>/approve', methods=['POST'])
def approve(lead_id):
    lead = db.get_lead(lead_id)
    if not lead or lead['status'] not in lead_stages.EDITABLE:
        abort(409, 'Lead is no longer awaiting review')
    edited = {}
    for field in db.LEAD_SCHEMA_FIELDS:
        if field in request.form:
            edited[field] = request.form.get(field) or None
    _refresh_territory_assignment(lead, edited)

    # Refuse the approval outright when Oracle would reject the lead anyway.
    # Checked here, against the lead as it would be saved, because this is the
    # moment the operator is looking at it: approving and finding out minutes
    # later through a notification meant the lead had to be hunted down and the
    # context rebuilt. The blockers are the same list the submitter enforces
    # (oracle_api.submission_blockers), so the two can never disagree.
    candidate = {**lead, **edited}
    blockers = submission_blockers(candidate)
    if blockers:
        reason = ' '.join(blockers)
        # Persisted as well as flashed: the toast is gone on the next click,
        # but the card keeps saying what to fix until it is fixed.
        db.update_lead_fields(lead_id, {'submit_error': f'Cannot approve: {reason}'})
        flash(f'{_lead_label(lead)} cannot be approved yet. {reason}', 'approve_error')
        return redirect(url_for('lead_detail', lead_id=lead_id))

    # Stages lead under 'review_approved' so user can review in /queue before final batch submission
    changed = db.approve_lead(
        lead_id,
        reviewed_by=request.form.get('reviewed_by') or 'dashboard',
        review_notes=request.form.get('review_notes') or None,
        edited_fields=edited,
        status='review_approved',
    )
    if not changed:
        abort(409, 'Lead is no longer awaiting review')
    # The reason it could not be approved before no longer applies.
    db.update_lead_fields(lead_id, {'submit_error': None})
    return redirect(_pending_folder_url(lead))


def _pending_folder_url(lead):
    """Pending Review, opened on the folder `lead` was filed in, so deciding a
    Spam lead from its own page goes back to Spam rather than to Inbox."""
    message_id = lead.get('message_id')
    sender = db.message_sender_emails([message_id]).get(message_id)
    if spam_folder.folder_of(lead, sender) == spam_folder.SPAM:
        return url_for('index', folder=spam_folder.SPAM)
    return url_for('index')


@app.route('/leads/<int:lead_id>/reject', methods=['POST'])
def reject(lead_id):
    lead = db.get_lead(lead_id)
    if not lead or lead['status'] not in lead_stages.EDITABLE:
        abort(409, 'Lead is no longer awaiting review')
    db.reject_lead(
        lead_id,
        reviewed_by=request.form.get('reviewed_by') or 'dashboard',
        review_notes=request.form.get('review_notes') or None,
    )
    review_marking.maybe_mark_email_reviewed(lead_id)
    return redirect(_pending_folder_url(lead))


@app.route('/leads/<int:lead_id>/forward', methods=['POST'])
def forward_lead_email(lead_id):
    """Forward the original Outlook email and dispose of the non-lead."""
    lead = db.get_lead(lead_id)
    if not lead or lead['status'] not in lead_stages.EDITABLE:
        abort(409, 'Lead is no longer awaiting review')

    detail_url = url_for('lead_detail', lead_id=lead_id)

    def _failed(text):
        return _action_failed(text, 'forward_error', detail_url)

    # Addressed and copied, same shape as a reply: handing an inquiry to whoever
    # should own it routinely means keeping someone else visibly in the loop.
    # There is no tick-list of the original thread here, though -- forwarding is
    # for people who are NOT already on the email.
    seen = set()
    try:
        recipients = _clean_addresses(
            _split_addresses(request.form.get('recipient')), seen
        )
        cc_addresses = _clean_addresses(
            _split_addresses(request.form.get('cc_recipients')), seen
        )
    except ValueError as bad:
        return _failed(f'{bad} is not a valid email address.')

    if not recipients and cc_addresses:
        return _failed('Choose someone to forward to, not only a copy.')
    if not recipients:
        return _failed('Enter one valid email address.')
    if len(recipients) + len(cc_addresses) > 20:
        return _failed('That is more than 20 recipients — trim the list.')

    forward_note = (request.form.get('forward_note') or '').strip()
    if len(forward_note) > 5000:
        return _failed('The forwarding message must be 5,000 characters or fewer.')

    message = db.get_message(lead.get('message_id')) if lead.get('message_id') else None
    if not message or not message.get('source_message_id'):
        return _failed('The original Outlook email is not available to forward.')

    action_id = db.begin_outlook_action(
        lead_id, 'forward', message_id=lead.get('message_id'),
        details={'to': recipients, 'cc': cc_addresses, 'body': forward_note},
    )
    try:
        outlook_ingest.forward_message(
            message['source_message_id'], recipients, note=forward_note or None,
            cc_addresses=cc_addresses,
            fallback_message_path=message.get('message_file_path'),
        )
    except ValueError as exc:
        db.finish_outlook_action(action_id, 'failed', str(exc))
        return _failed(str(exc))
    except outlook_ingest.OutlookForwardError as exc:
        db.finish_outlook_action(action_id, 'failed', str(exc))
        app.logger.exception('Could not forward source email for lead %s', lead_id)
        return _failed(
            f'Outlook failed while {exc.stage}. The email was not sent and the '
            'lead is still pending.'
        )
    except Exception:
        db.finish_outlook_action(
            action_id, 'unknown',
            'Unexpected Outlook error; check Sent Items before trying again.',
        )
        app.logger.exception('Could not forward source email for lead %s', lead_id)
        return _failed(
            'Outlook could not send the email. Check that Outlook is open and '
            'signed in, then try again.'
        )

    db.finish_outlook_action(action_id, 'sent')
    sent_to = _sent_to_label(recipients, cc_addresses)
    # Recorded like a reply, so the addresses a lead actually gets handed to
    # are available to autocomplete next time. The review note stays too --
    # it is what a person reads on the lead itself.
    db.record_lead_reply(
        lead_id, forward_note or '(forwarded without a message)',
        recipients=recipients, cc_recipients=cc_addresses,
        message_id=lead.get('message_id'), kind='forward',
        sent_by=request.form.get('reviewed_by') or 'dashboard',
    )
    db.reject_lead(
        lead_id,
        reviewed_by='dashboard',
        review_notes=f'Original email forwarded to {sent_to}; not processed as a lead.',
    )
    review_marking.maybe_mark_email_reviewed(lead_id)
    # Only this lead leaves the queue: unlike deleting the email, forwarding
    # says nothing about the other leads that came out of the same message.
    return _action_done(
        f'Email forwarded to {sent_to}.', 'forward_success',
        url_for('lead_detail', lead_id=lead_id, forwarded=1), dropped=[lead_id],
    )


@app.route('/leads/<int:lead_id>/recipients', methods=['POST'])
def lead_recipients(lead_id):
    """
    Who else is on this lead's email, as JSON for the reply dialog.

    Answers from the stored value when there is one. When there isn't -- every
    message ingested before To/CC was captured, which is most of what is in the
    queue right now -- it reads Outlook once and records the answer, so the
    next open is a database read.

    POST rather than GET because it can write: the app's CSRF check covers
    POSTs, and a GET that quietly updates a row is the kind of thing that
    surprises whoever reads the route list later.
    """
    lead = db.get_lead(lead_id)
    if not lead:
        abort(404)

    message = db.get_message(lead.get('message_id')) if lead.get('message_id') else None
    if not message or not message.get('source_message_id'):
        return {'known': False, 'label': '', 'count': 0,
                'error': 'The original email is not available.'}

    recipients = db.parse_message_recipients(message.get('recipients'))
    if recipients is None:
        try:
            recipients = outlook_ingest.message_recipients(
                message['source_message_id'],
                fallback_message_path=message.get('message_file_path'),
            )
        except Exception:
            app.logger.exception('Could not read recipients for lead %s', lead_id)
            return {'known': False, 'label': '', 'count': 0,
                    'error': 'Outlook could not be reached to check who else is on this email.'}
        db.set_message_recipients(message['id'], recipients)

    others = _other_recipients(recipients)
    return {'known': True, 'label': _recipients_label(others),
            'count': len(others), 'error': None}


@app.route('/leads/<int:lead_id>/reply', methods=['POST'])
def reply_lead_email(lead_id):
    """Reply to the original Outlook email and keep the lead where it is.

    Unlike forwarding and deleting -- which both dispose of a message that
    turned out not to be a lead -- a reply is part of working the lead: you
    answer a question or ask for the detail the inquiry left out, and it stays
    in the review queue waiting for that answer. The sent reply is recorded so
    whoever opens the lead next can see the customer has already been written
    to, since outbound mail never comes back through ingestion.
    """
    lead = db.get_lead(lead_id)
    if not lead or lead['status'] not in lead_stages.EDITABLE:
        abort(409, 'Lead is no longer awaiting review')

    detail_url = url_for('lead_detail', lead_id=lead_id)

    def _failed(text):
        return _action_failed(text, 'reply_error', detail_url)

    body = (request.form.get('reply_body') or '').strip()
    if not body:
        return _failed('Write a message before sending the reply.')
    if len(body) > 10000:
        return _failed('The reply must be 10,000 characters or fewer.')

    # Ticked candidates plus anyone typed in, de-duplicated case-insensitively
    # while keeping what the operator actually typed. Addressed and copied
    # recipients are collected separately; the To line wins if the same person
    # somehow ends up in both.
    seen = set()

    def _collect(ticked_field, typed_field):
        return _clean_addresses(
            request.form.getlist(ticked_field)
            + _split_addresses(request.form.get(typed_field)),
            seen,
        )

    try:
        to_addresses = _collect('recipients', 'extra_recipients')
        cc_addresses = _collect('cc_recipients', 'extra_cc_recipients')
    except ValueError as bad:
        return _failed(f'{bad} is not a valid email address.')

    if not to_addresses and not cc_addresses:
        return _failed('Choose at least one person to reply to.')
    if not to_addresses:
        return _failed('Choose someone to address the reply to, not only a copy.')
    if len(to_addresses) + len(cc_addresses) > 20:
        return _failed('That is more than 20 recipients — trim the list.')

    # Which message of the thread is being answered. Identified by timestamp
    # and sender because a conversation message's Outlook id does not resolve
    # later -- see outlook_ingest._find_thread_item().
    thread_target = None
    if request.form.get('thread_received_at'):
        thread_target = {
            'received_at': request.form.get('thread_received_at'),
            'sender_email': request.form.get('thread_sender') or None,
        }

    message = db.get_message(lead.get('message_id')) if lead.get('message_id') else None
    if not message or not message.get('source_message_id'):
        return _failed('The original Outlook email is not available to reply to.')

    action_id = db.begin_outlook_action(
        lead_id, 'reply', message_id=lead.get('message_id'),
        details={'to': to_addresses, 'cc': cc_addresses, 'body': body},
    )
    try:
        recipients = outlook_ingest.reply_message(
            message['source_message_id'], body, to_addresses=to_addresses,
            cc_addresses=cc_addresses, thread_target=thread_target,
            fallback_message_path=message.get('message_file_path'),
        )
    except ValueError as exc:
        db.finish_outlook_action(action_id, 'failed', str(exc))
        return _failed(str(exc))
    except outlook_ingest.OutlookReplyError as exc:
        db.finish_outlook_action(action_id, 'failed', str(exc))
        app.logger.exception('Could not reply to source email for lead %s', lead_id)
        return _failed(f'Outlook failed while {exc.stage}. The reply was not sent.')
    except Exception:
        db.finish_outlook_action(
            action_id, 'unknown',
            'Unexpected Outlook error; check Sent Items before trying again.',
        )
        app.logger.exception('Could not reply to source email for lead %s', lead_id)
        return _failed(
            'Outlook could not send the reply. Check that Outlook is open and '
            'signed in, then try again.'
        )

    db.finish_outlook_action(action_id, 'sent')
    db.record_lead_reply(
        lead_id, body, recipients=to_addresses, cc_recipients=cc_addresses,
        message_id=lead.get('message_id'),
        # Kept for the record: whether this went to more than the one person
        # who wrote the message being answered.
        reply_all=len(to_addresses) + len(cc_addresses) > 1,
        sent_by=request.form.get('reviewed_by') or 'dashboard',
    )
    # No `dropped`: a reply is part of working the lead, so its card stays in
    # the queue waiting for the answer.
    return _action_done(
        f'Reply sent to {_sent_to_label(to_addresses, cc_addresses)}.', 'reply_success',
        url_for('lead_detail', lead_id=lead_id, replied=1),
    )


def _drop_leads_from_deleted_email(lead_id, message_id, note):
    """
    Reject every still-undecided lead pulled from an email that has just been
    deleted, and return their ids.

    One email can produce several leads (an Excel attachment routinely does),
    and none of them can be reviewed any more once their source email has left
    the inbox -- leaving the siblings sitting in the queue would strand leads
    whose original is no longer there to check them against.
    """
    siblings = db.list_leads_by_message(message_id) if message_id else []
    lead_ids = [
        s['id'] for s in siblings
        if s['status'] in lead_stages.EDITABLE
    ]
    if lead_id not in lead_ids:
        lead_ids.append(lead_id)
    for dropped_id in lead_ids:
        db.reject_lead(dropped_id, reviewed_by='dashboard', review_notes=note)
    return lead_ids


@app.route('/leads/<int:lead_id>/delete-email', methods=['POST'])
def delete_lead_email(lead_id):
    """Delete the original Outlook email from the Sales inbox and drop the lead.

    The counterpart to forwarding: forwarding hands a non-lead to whoever
    should have received it, this simply removes junk. The email goes to the
    Sales mailbox's Deleted Items (recoverable -- see
    outlook_ingest.delete_message), and the leads it produced are rejected.
    """
    lead = db.get_lead(lead_id)
    if not lead or lead['status'] not in lead_stages.EDITABLE:
        abort(409, 'Lead is no longer awaiting review')

    detail_url = url_for('lead_detail', lead_id=lead_id)

    def _failed(text):
        return _action_failed(text, 'delete_error', detail_url)

    message = db.get_message(lead.get('message_id')) if lead.get('message_id') else None
    if not message or not message.get('source_message_id'):
        return _failed('The original Outlook email is not available to delete.')

    action_id = db.begin_outlook_action(
        lead_id, 'delete', message_id=lead.get('message_id'),
        details={'source_message_id': message['source_message_id']},
    )
    try:
        deleted = outlook_ingest.delete_message(message['source_message_id'])
    except ValueError as exc:
        db.finish_outlook_action(action_id, 'failed', str(exc))
        # Deletion switched off in config, or the message turned out not to be
        # in the Sales mailbox. Both are worth saying verbatim.
        return _failed(str(exc))
    except outlook_ingest.OutlookDeleteError as exc:
        db.finish_outlook_action(action_id, 'failed', str(exc))
        app.logger.exception('Could not delete source email for lead %s', lead_id)
        return _failed(
            f'Outlook failed while {exc.stage}. The email was not deleted and the '
            'lead is still pending.'
        )
    except Exception:
        db.finish_outlook_action(
            action_id, 'unknown',
            'Unexpected Outlook error; verify the message before trying again.',
        )
        app.logger.exception('Could not delete source email for lead %s', lead_id)
        return _failed(
            'Outlook could not delete the email. Check that Outlook is open and '
            'signed in, then try again.'
        )

    db.finish_outlook_action(action_id, 'deleted')
    dropped = _drop_leads_from_deleted_email(
        lead_id, lead.get('message_id'),
        'Original email deleted from the Sales inbox; not processed as a lead.',
    )
    # Deliberately no review_marking.maybe_mark_email_reviewed() here: the green
    # "already entered" tag is written onto the live inbox item, which no longer
    # exists. Its other job -- keeping the email from being re-ingested -- is
    # already covered, since a deleted message is not returned by a later fetch.
    # Named by subject: the operator is looking at a queue of cards and needs to
    # know which email just went, and the subject line is what they were reading
    # when they decided it was junk.
    subject = (message.get('translated_subject') or message.get('subject') or '').strip()
    if not deleted:
        message_text = 'That email was no longer in the Sales inbox.'
    elif subject:
        message_text = f'Deleted email from "{subject}".'
    else:
        message_text = 'Deleted email with no subject.'
    others = len(dropped) - 1
    if others == 1:
        message_text += ' 1 other lead from the same email was dropped too.'
    elif others > 1:
        message_text += f' {others} other leads from the same email were dropped too.'

    return _action_done(
        message_text, 'delete_success',
        url_for('lead_detail', lead_id=lead_id, deleted=1), dropped=dropped,
    )


@app.route('/leads/<int:lead_id>/restore', methods=['POST'])
def restore(lead_id):
    """Send a finished lead back to Pending Review.

    'duplicate' is in lead_stages.RETURNABLE alongside rejected and failed: a
    lead the dedup guard matched to an already-submitted one had no way back
    at all, so a wrong match -- two genuine inquiries from the same person,
    months apart -- was a lead nobody could ever act on again.
    """
    lead = db.get_lead(lead_id)
    if not lead or lead['status'] not in lead_stages.RETURNABLE:
        abort(409, 'Lead cannot be restored from its current state')
    db.restore_lead_to_review(lead_id)
    return redirect(url_for('history'))


@app.route('/leads/<int:lead_id>/check-oracle', methods=['POST'])
def check_oracle(lead_id):
    """Release a lead a dead submit run left claimed.

    A run claims each lead as 'submitting' before it calls Oracle, so a run
    that is killed -- machine restarted, process closed -- leaves its lead
    claimed with nothing left to finish it. db.release_stale_submission_claims()
    sweeps those up, but only when some later run starts, and nothing here runs
    on a timer.

    It moves to 'submission_unknown' rather than straight back to 'approved'
    for the reason that status exists: the dead run may have reached Oracle
    before it died. History then offers the two honest answers -- confirmed in
    Oracle, or checked and safe to retry.
    """
    lead = db.get_lead(lead_id)
    if not lead:
        abort(404)
    if lead['status'] != 'submitting':
        abort(409, 'This lead is not mid-submission')
    db.mark_submission_unknown(
        lead_id,
        'The submit run holding this lead stopped without finishing. '
        'Check Oracle for a record before retrying.',
    )
    flash(
        f'{_lead_label(lead)} was released from a stopped submit run. '
        'It is in History — check Oracle, then confirm or retry it there.',
        'submit_error',
    )
    return redirect(url_for('queue'))


@app.route('/leads/<int:lead_id>/unknown-confirmed', methods=['POST'])
def unknown_confirmed(lead_id):
    lead = db.get_lead(lead_id)
    if not lead or lead['status'] != 'submission_unknown':
        abort(409, 'Lead does not have an uncertain submission outcome')
    db.mark_lead_submitted(
        lead_id, lead.get('oracle_project_name') or 'Manually confirmed in Oracle'
    )
    return redirect(url_for('history'))


@app.route('/leads/<int:lead_id>/unknown-retry', methods=['POST'])
def unknown_retry(lead_id):
    lead = db.get_lead(lead_id)
    if not lead or lead['status'] != 'submission_unknown':
        abort(409, 'Lead does not have an uncertain submission outcome')
    db.schedule_lead_retry(
        lead_id, 'Operator checked Oracle and explicitly authorized a retry.',
        delay_minutes=0,
    )
    # Start the run here: with no scheduled submission, authorizing a retry
    # would otherwise only set a flag and wait for the next batch approval.
    _start_submit_run()
    return redirect(url_for('history'))


@app.route('/history')
def history():
    # Finished leads only. 'approved' and 'submitting' used to be listed here
    # as well as in the Input Queue, so a lead on its way to Oracle appeared
    # twice and "where is it?" had two answers. They now belong to the queue
    # until Oracle has actually taken them -- see lead_stages.py.
    statuses = list(lead_stages.HISTORY)
    query = request.args.get('q', '').strip()
    per_page = 25
    page = max(1, request.args.get('page', 1, type=int))
    total = db.count_leads(status=statuses, query=query if query else None)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    # order='finished', not the inbox order the other two pages use: these
    # leads are done, and the newest thing here is the one that just went to
    # Oracle. Ordering History by email date meant a batch submitted this
    # morning scattered itself through months of older leads, so the page read
    # as unordered. See db.LEAD_ORDERS.
    leads = db.list_leads(
        status=statuses, limit=per_page, offset=(page - 1) * per_page,
        order='finished', query=query if query else None,
    )
    leads = [_decorate(l) for l in leads]
    for lead in leads:
        # Rendered the way the Reviewed column it replaces was -- a trimmed
        # ISO stamp -- so the fixed-width column still fits it.
        lead['finished_at'] = (_finished_at(lead) or '')[:16].replace('T', ' ')
    return render_template(
        'history.html', leads=leads, total=total, page=page, pages=pages, query=query,
    )


def _reference_kind(suffix):
    if suffix in REFERENCE_IMAGE_TYPES:
        return 'image'
    if suffix in REFERENCE_PDF_TYPES:
        return 'pdf'
    if suffix in REFERENCE_TEXT_TYPES:
        return 'text'
    return 'file'


def _human_size(num_bytes):
    for unit in ('bytes', 'KB', 'MB'):
        if num_bytes < 1024 or unit == 'MB':
            precision = 0 if unit == 'bytes' else 1
            return f'{num_bytes:.{precision}f} {unit}'
        num_bytes /= 1024


def _reference_documents():
    """
    Every file under reference/, described for display, newest first within
    each folder.

    Subfolders become sections, so documents can be grouped ("Territories",
    "Price lists") simply by making a folder. Hidden/dot files are skipped so
    editor and OS leftovers (.DS_Store, Thumbs.db) never show up as documents.
    """
    if not REFERENCE_DIR.is_dir():
        return []

    documents = []
    for path in sorted(REFERENCE_DIR.rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(REFERENCE_DIR)
        if any(part.startswith('.') for part in relative.parts):
            continue
        # The folder's own README explains how to use the folder. Rendering it
        # as a document made the tab open on a wall of monospaced instructions
        # -- it reads as code, and it is the one thing here nobody came to look
        # at.
        if path.stem.lower() == 'readme':
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        suffix = path.suffix.lower()
        kind = _reference_kind(suffix)
        document = {
            'name': path.stem.replace('_', ' ').replace('-', ' '),
            'filename': path.name,
            # Always forward slashes: this is a URL path, not a Windows path.
            'relpath': relative.as_posix(),
            'folder': relative.parent.as_posix() if relative.parent != Path('.') else '',
            'kind': kind,
            'size': _human_size(stat.st_size),
            'updated': _format_timestamp(
                datetime.fromtimestamp(stat.st_mtime).isoformat()
            ),
            'text': None,
            'truncated': False,
        }
        if kind == 'text':
            try:
                content = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                content = None
            if content is not None:
                document['truncated'] = len(content) > REFERENCE_TEXT_PREVIEW_LIMIT
                document['text'] = content[:REFERENCE_TEXT_PREVIEW_LIMIT]
        documents.append(document)
    return documents


@app.route('/reference')
def reference():
    """Reference material for whoever is reviewing leads -- territory maps and
    the like -- kept as ordinary files so they can be swapped out without a
    code change. Read-only: nothing here is uploaded through the dashboard."""
    documents = _reference_documents()
    sections = {}
    for document in documents:
        sections.setdefault(document['folder'], []).append(document)
    return render_template(
        'reference.html',
        sections=sorted(sections.items()),
        document_count=len(documents),
        reference_dir=str(REFERENCE_DIR),
    )


def _unique_reference_path(filename):
    """A path under reference/ that does not overwrite an existing document.
    Adding a second copy of a map should never silently replace the first."""
    target = REFERENCE_DIR / filename
    if not target.exists():
        return target
    stem, suffix = Path(filename).stem, Path(filename).suffix
    for n in range(2, 1000):
        candidate = REFERENCE_DIR / f'{stem} ({n}){suffix}'
        if not candidate.exists():
            return candidate
    raise FileExistsError(filename)


@app.route('/reference/upload', methods=['POST'])
def reference_upload():
    """
    Add documents to reference/ from the browser.

    Here because the alternative is asking someone to find a folder on disk
    every time they want a territory map on the page -- a screenshot is taken
    and pasted, not filed. Deliberately narrow: it writes only into
    reference/, only file types the tab can display, and never overwrites.
    """
    uploads = [f for f in request.files.getlist('document') if f and f.filename]
    if not uploads:
        flash('Choose a file to add.', 'reference_error')
        return redirect(url_for('reference'))

    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    saved, rejected = [], []
    for upload in uploads:
        # secure_filename strips any directory part, so "../../.env" arrives
        # as "env" -- an upload cannot choose where it lands, only its name.
        filename = secure_filename(upload.filename)
        if not filename or Path(filename).suffix.lower() not in REFERENCE_UPLOAD_TYPES:
            rejected.append(upload.filename)
            continue
        try:
            target = _unique_reference_path(filename)
            upload.save(target)
        except (OSError, FileExistsError):
            app.logger.exception('Could not save reference document %s', filename)
            rejected.append(upload.filename)
            continue
        saved.append(target.name)

    if saved:
        flash(f'Added {", ".join(saved)}.', 'reference_success')
    if rejected:
        flash(
            f'Could not add {", ".join(rejected)}. Reference documents must be '
            'an image, a PDF, or a .txt/.md/.csv file.',
            'reference_error',
        )
    return redirect(url_for('reference'))


def _reference_target(relpath):
    """
    Resolve a document path from a request, or None if it is not a document
    this tab is allowed to touch.

    Same containment check as reference_file(), for the same reason: this is a
    URL turning into a filesystem path, and here it turns into a deletion. The
    README and dot-file exclusions match _reference_documents() so the route
    can only reach something the page actually lists.
    """
    if not relpath:
        return None
    try:
        target = (REFERENCE_DIR / relpath).resolve()
        relative = target.relative_to(REFERENCE_DIR.resolve())
    except (ValueError, OSError):
        return None
    if not target.is_file():
        return None
    if any(part.startswith('.') for part in relative.parts):
        return None
    if target.stem.lower() == 'readme':
        return None
    return target


@app.route('/reference/delete', methods=['POST'])
def reference_delete():
    """Remove one reference document. The button sits inside the opened
    preview, so a document cannot be deleted without having been looked at."""
    target = _reference_target(request.form.get('relpath'))
    if target is None:
        flash('That document could not be found.', 'reference_error')
        return redirect(url_for('reference'))

    try:
        target.unlink()
    except OSError:
        app.logger.exception('Could not delete reference document %s', target)
        flash(f'Could not delete {target.name}.', 'reference_error')
        return redirect(url_for('reference'))

    flash(f'Deleted {target.name}.', 'reference_success')
    return redirect(url_for('reference'))


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(error):
    """A too-large upload should say so on the page it came from, rather than
    dropping the operator onto a bare 413.

    MAX_CONTENT_LENGTH caps every request body, so three pages can raise
    this: the Reference tab, the screenshot box on Create Lead, and Import
    spreadsheet. Answering both
    from the reference page sent an operator who had just attached a large
    screenshot to a tab about territory maps, carrying a flash category that
    page does not render -- so the error vanished and the lead simply never
    appeared."""
    limit_mb = MAX_REFERENCE_UPLOAD_BYTES // (1024 * 1024)
    message = f'That file is too large — the limit is {limit_mb} MB.'
    if (request.endpoint or '') == 'create_lead':
        flash(message, 'manual_error')
        return redirect(url_for('new_lead'))
    if (request.endpoint or '') == 'import_leads':
        flash(message, 'import_error')
        return redirect(url_for('import_form'))
    flash(message, 'reference_error')
    return redirect(url_for('reference'))


@app.route('/reference/file/<path:relpath>')
def reference_file(relpath):
    """
    Serve one file out of reference/, and nothing outside it.

    send_from_directory already refuses to escape the directory, but the
    resolved-path check is kept as the explicit guarantee: this is the only
    route in the dashboard that turns part of a URL into a filesystem path,
    and it should be obvious from reading it that "../.." cannot reach the
    .env file or pipeline.db sitting one level up.
    """
    try:
        target = (REFERENCE_DIR / relpath).resolve()
        target.relative_to(REFERENCE_DIR.resolve())
    except (ValueError, OSError):
        abort(404)
    if not target.is_file():
        abort(404)

    kind = _reference_kind(target.suffix.lower())
    # Images and PDFs are meant to be looked at, so they render in the page.
    # Anything else downloads instead of opening: these files come from a
    # shared drive rather than this codebase, and an .html or .htm dropped in
    # the folder would otherwise run as a page in the dashboard's own origin.
    as_attachment = kind not in ('image', 'pdf')
    response = send_from_directory(REFERENCE_DIR, relpath, as_attachment=as_attachment)
    response.headers['X-Content-Type-Options'] = 'nosniff'

    if not as_attachment:
        # The Reference tab previews a PDF in an <iframe> pointing back here.
        # The app-wide "X-Frame-Options: DENY" and "frame-ancestors 'none'"
        # (add_security_headers) refuse ALL framing -- including the dashboard
        # framing its own file -- so the browser blocks the load and draws
        # "refused to connect" where the preview should be. Narrowed to
        # same-origin for served files only: every HTML page keeps DENY, which
        # is where the clickjacking risk actually is (the approve/reject and
        # delete buttons). A cross-origin page still cannot frame this.
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Content-Security-Policy'] = _content_security_policy("'self'")

    return response


def _calculate_days_to_qualify(created_at, qualified_at):
    if not qualified_at or not created_at:
        return None
    try:
        def parse_ts(ts):
            ts = str(ts).strip().replace('Z', '+00:00')
            if 'T' in ts:
                return datetime.fromisoformat(ts)
            elif ' ' in ts:
                return datetime.fromisoformat(ts.replace(' ', 'T'))
            else:
                return datetime.fromisoformat(ts + 'T00:00:00')

        c_dt = parse_ts(created_at)
        q_dt = parse_ts(qualified_at)
        days = (q_dt.date() - c_dt.date()).days
        return max(0, days)
    except Exception:
        return None


@app.route('/status')
def status():
    keys = [
        'graph_delta_link', 'outlook_last_checked_at',
        'last_ingest_run_at', 'last_submit_run_at', 'oracle_session_status',
        'oracle_sync_status', 'oracle_sync_started_at',
        'oracle_sync_finished_at', 'oracle_sync_result',
        'oracle_order_lines_status',
    ]
    state = {k: db.get_run_state(k) for k in keys}
    # Lifetime figures, not live row counts: retention.py deletes finished
    # leads once they are old enough, and every count on this page would step
    # down each time it ran. A conversion rate over a denominator that shrinks
    # as history is purged is worse than no rate -- it still looks like a real
    # number. db.lifetime_* adds what was deleted back in from
    # purged_lead_stats, which holds counts and money only, never names.
    lifetime_counts = db.lifetime_lead_counts()
    counts = {s: lifetime_counts.get(s, 0) for s in db.LEAD_STATUSES}
    query = request.args.get('q', '').strip()
    outcome = request.args.get('outcome', 'all').strip().lower()
    if outcome not in ('all', 'qualified', 'converted'):
        outcome = 'all'
    oracle_summary = db.lifetime_oracle_summary()
    local_summary = {
        'total': sum(lifetime_counts.values()),
        'approved': sum(lifetime_counts.get(s, 0)
                        for s in ('review_approved', 'approved', 'submitted')),
        'rejected': lifetime_counts.get('rejected', 0),
        'submitted': lifetime_counts.get('submitted', 0),
    }
    # How many of those the database no longer holds. The per-lead table below
    # can only list leads that still exist, so the page says so rather than
    # letting the difference between the funnel and the list read as a bug.
    purged_total = sum(
        bucket['count'] for bucket
        in db.purged_stats().get(db.PURGED_LEAD_STATUS, {}).values()
    )
    submitted = local_summary.get('submitted', 0)
    total = local_summary.get('total', 0)
    synced = oracle_summary.get('synced', 0)
    qualified = oracle_summary.get('qualified', 0)
    converted = oracle_summary.get('converted', 0)

    conversion_rates = {
        'qualification_rate': round((qualified / submitted * 100), 1) if submitted > 0 else 0.0,
        'conversion_rate': round((converted / submitted * 100), 1) if submitted > 0 else 0.0,
        'end_to_end_rate': round((converted / total * 100), 1) if total > 0 else 0.0,
        'synced_rate': round((synced / submitted * 100), 1) if submitted > 0 else 0.0,
    }
    outcomes = db.search_lead_outcomes(query=query, outcome=outcome)
    for item in outcomes:
        item['days_to_qualify'] = _calculate_days_to_qualify(item.get('created_at'), item.get('qualified_at'))

    return render_template(
        'status.html', state=state, counts=counts,
        message_counts=db.lifetime_message_counts(),
        local_summary=local_summary, oracle_summary=oracle_summary,
        conversion_rates=conversion_rates,
        outcomes=outcomes, purged_total=purged_total,
        retention_rejected_days=retention.REJECTED_RETENTION_DAYS,
        retention_finished_days=retention.FINISHED_RETENTION_DAYS,
        query=query, outcome=outcome,
    )


def _start_oracle_sync_run(lease_token=None):
    """Start an Oracle outcome sync run in the background."""
    import subprocess
    try:
        child_env = os.environ.copy()
        if lease_token:
            child_env['PIPELINE_ORACLE_SYNC_LEASE_TOKEN'] = lease_token
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve().parent.parent / 'oracle_sync.py')],
            cwd=str(Path(__file__).resolve().parent.parent), env=child_env,
        )
        return True
    except OSError:
        if lease_token:
            db.update_run_lease(_ORACLE_SYNC_JOB_KEY, lease_token, 'failed', 'Process could not start')
        app.logger.exception('Could not start a manual Oracle outcome sync run')
        return False


@app.route('/status/sync', methods=['POST'])
def trigger_oracle_sync():
    """Trigger an on-demand background read-only sync of lead outcomes from Oracle."""
    lease_token = db.acquire_run_lease(_ORACLE_SYNC_JOB_KEY, stale_minutes=30)
    if not lease_token:
        message = 'An Oracle sync is already in progress. New outcomes will appear automatically.'
        if _wants_json():
            return {'ok': False, 'error': message}
        flash(message, 'oracle_sync_notice')
        return redirect(url_for('status'))

    if not _start_oracle_sync_run(lease_token):
        message = 'Could not start Oracle outcome sync. Check the dashboard log.'
        if _wants_json():
            return {'ok': False, 'error': message}
        flash(message, 'oracle_sync_error')
        return redirect(url_for('status'))

    message = 'Started Oracle outcome sync in the background. Outcomes will refresh automatically.'
    if _wants_json():
        return {'ok': True, 'message': message}
    flash(message, 'oracle_sync_success')
    return redirect(url_for('status'))


@app.route('/status/leads/<int:lead_id>')
def lead_lifecycle(lead_id):
    lifecycle = db.get_lead_lifecycle(lead_id)
    if not lifecycle:
        abort(404)
    payload = lifecycle['lead'].get('oracle_payload')
    try:
        lifecycle['submitted_payload'] = json.loads(payload) if payload else None
    except (TypeError, ValueError):
        lifecycle['submitted_payload'] = None
    return render_template('lead_lifecycle.html', **lifecycle)


if __name__ == '__main__':
    db.init_db()
    with open(Path(__file__).resolve().parent.parent / 'config.json') as f:
        dash_config = json.load(f).get('dashboard', {})
    app.run(
        host=dash_config.get('host', '127.0.0.1'),
        port=dash_config.get('port', 5151),
        debug=bool(dash_config.get('debug', False)),
    )
