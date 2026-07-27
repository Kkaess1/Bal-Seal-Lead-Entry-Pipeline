"""
Outlook desktop (COM) email ingestion.

Uses Windows COM automation (pywin32) to read the "Sales" shared mailbox
directly through the already-signed-in Outlook desktop application -- no
Microsoft Graph API, no Azure AD app registration, no admin/IT action needed
at all.

Deliberately scoped to ONLY the shared mailbox named in config.json's
outlook.mailbox_name (default "Sales") -- confirmed via a live folder-list
that this Outlook profile has two separate top-level mailboxes, "Sales" and
Keaton's personal "kkaess@balseal.com". This module must never fall back to
GetDefaultFolder() (which returns the *personal* account's inbox) -- that
would read personal/internal correspondence the pipeline has no business
touching, not just sales leads.

Requires: Outlook desktop installed, signed in, with the Sales mailbox added
to the profile, and an interactive Windows session at run time -- COM
automation needs Outlook to actually be reachable, which is why the
scheduled task should run "only when logged on," not fully headless
overnight. See project notes for the exact Task Scheduler setup.

Exposes the same interface as email_ingest.py's Graph-based path
(fetch_new_messages(), download_excel_attachments()) so pipeline_ingest.py
doesn't care which backend is active. email_ingest.py/graph_auth.py are kept
around, dormant, in case Graph API access ever becomes available later --
switching back is a one-line import change in pipeline_ingest.py.
"""

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pywintypes
import win32com.client

import db

ATTACHMENT_DIR = Path(__file__).parent / 'email_attachments'
_LAST_CHECKED_KEY = 'outlook_last_checked_at'
_PENDING_CHECKED_KEY = 'outlook_pending_checked_at'
_OUTLOOK_DATE_FMT = '%m/%d/%Y %I:%M %p'   # format Outlook's Restrict() filter expects
_OL_MAIL_ITEM_CLASS = 43                  # olMail -- skip meeting requests, receipts, etc.


class OutlookUnavailableError(RuntimeError):
    """Raised when Outlook (or the specific Sales mailbox) can't be reached
    at all -- not installed, not signed in, mailbox not found, or COM
    automation blocked by local policy. Distinct from a per-message
    processing error so callers can tell "the whole backend is down, stop
    and flag it" apart from "one message failed, log and move on"."""


def _mailbox_name():
    with open(Path(__file__).parent / 'config.json') as f:
        config = json.load(f)
    return config.get('outlook', {}).get('mailbox_name', 'Sales')


def _connect():
    try:
        outlook = win32com.client.Dispatch('Outlook.Application')
        namespace = outlook.GetNamespace('MAPI')
        return namespace
    except (pywintypes.com_error, AttributeError) as e:
        # AttributeError shows up here too, not just com_error -- confirmed
        # live when Outlook was momentarily busy right after a prior Save()
        # call; a bare retry succeeded immediately. Normalizing both into the
        # same OutlookUnavailableError means every call site (main()'s
        # `except RuntimeError`, pipeline_ingest.py's per-message handling)
        # gets consistent, clean error handling instead of a raw traceback
        # on a transient hiccup.
        raise OutlookUnavailableError(
            f'Could not connect to Outlook via COM: {e}. Is Outlook desktop installed '
            'and signed in? Is this running in an interactive logged-on session? '
            '(If Outlook was just busy, this may just need a retry.)'
        ) from e


def _get_sales_inbox(namespace):
    """
    Returns the Inbox folder of the configured Sales mailbox specifically --
    never the signed-in user's personal default inbox. Raises
    OutlookUnavailableError (listing what *is* available) if the named
    mailbox isn't found, rather than silently falling back to anything else.
    """
    mailbox_name = _mailbox_name()
    for folder in namespace.Folders:
        if folder.Name == mailbox_name:
            return folder.Folders.Item('Inbox')
    available = [f.Name for f in namespace.Folders]
    raise OutlookUnavailableError(
        f'Could not find a mailbox named "{mailbox_name}" in this Outlook profile. '
        f'Available top-level mailboxes: {available}. Check config.json\'s '
        'outlook.mailbox_name, and that the Sales mailbox is added to this Outlook profile.'
    )


def _sender_smtp_address(item):
    """
    Outlook COM returns an X.500 legacy Exchange DN for SenderEmailAddress on
    internal/Exchange mail (SenderEmailType == 'EX'), not a usable SMTP
    address -- confirmed against this mailbox's real CurrentUser.Address.
    Resolve through the Exchange user object when that's the case; external
    senders (SenderEmailType == 'SMTP') already come through fine as-is.
    """
    try:
        if item.SenderEmailType == 'EX':
            exch_user = item.Sender.GetExchangeUser()
            if exch_user and exch_user.PrimarySmtpAddress:
                return exch_user.PrimarySmtpAddress
        return item.SenderEmailAddress
    except Exception:
        return getattr(item, 'SenderEmailAddress', None)


def _item_to_message_dict(item):
    received_at = item.ReceivedTime
    received_at_str = received_at.isoformat() if hasattr(received_at, 'isoformat') else str(received_at)
    return {
        'id': item.EntryID,
        'received_at': received_at_str,
        'sender_email': _sender_smtp_address(item),
        'sender_name': getattr(item, 'SenderName', None),
        'subject': item.Subject,
        'body_text': item.Body or '',
        'has_attachments': item.Attachments.Count > 0,
    }


def fetch_new_messages():
    """
    Returns a list of dicts: {id, received_at, sender_email, sender_name,
    subject, body_text, has_attachments}.

    Watermark is a plain timestamp (unlike Graph's delta link) stored in
    db.run_state, captured at the START of this run -- not from the newest
    message seen -- so a message that arrives mid-run is never skipped, only
    picked up again next time.
    """
    run_started_at = datetime.now()
    last_checked_str = db.get_run_state(_LAST_CHECKED_KEY)
    if last_checked_str:
        last_checked = datetime.fromisoformat(last_checked_str)
    else:
        last_checked = run_started_at - timedelta(days=1)  # first run: baseline to last 24h

    namespace = _connect()
    inbox = _get_sales_inbox(namespace)

    filter_str = f"[ReceivedTime] > '{last_checked.strftime(_OUTLOOK_DATE_FMT)}'"
    restricted = inbox.Items.Restrict(filter_str)

    messages = []
    for item in restricted:
        if getattr(item, 'Class', None) != _OL_MAIL_ITEM_CLASS:
            continue
        try:
            messages.append(_item_to_message_dict(item))
        except pywintypes.com_error:
            continue  # a single unreadable item shouldn't fail the whole run

    # This is deliberately only a pending watermark. The orchestrator commits
    # it after every fetched message has been durably processed. If the process
    # crashes, the prior watermark remains in place and the messages are seen
    # again; message/lead idempotency makes that safe.
    db.set_run_state(_PENDING_CHECKED_KEY, run_started_at.isoformat())
    return messages


def commit_fetch_watermark():
    pending = db.get_run_state(_PENDING_CHECKED_KEY)
    if pending:
        db.set_run_state(_LAST_CHECKED_KEY, pending)
        db.set_run_state(_PENDING_CHECKED_KEY, '')


def fetch_recent_messages(count):
    """
    Returns the `count` most recent mail items in the Sales inbox, regardless
    of the last-checked watermark -- for one-off bounded test/backfill runs
    (e.g. "process just the last 10 emails"). Deliberately does NOT read or
    update the run_state watermark, so it never affects what
    fetch_new_messages() picks up on the next real scheduled run.
    """
    namespace = _connect()
    inbox = _get_sales_inbox(namespace)
    items = inbox.Items
    items.Sort('[ReceivedTime]', True)  # descending -- newest first

    messages = []
    for item in items:
        if len(messages) >= count:
            break
        if getattr(item, 'Class', None) != _OL_MAIL_ITEM_CLASS:
            continue
        try:
            messages.append(_item_to_message_dict(item))
        except pywintypes.com_error:
            continue

    return messages


def download_excel_attachments(message_id):
    """
    Returns local file paths for any .xlsx/.xls attachments on the given
    message (message_id is the Outlook EntryID from fetch_new_messages).

    Outlook's EntryID is ~140 characters -- using it verbatim in a filename
    pushed the full path past Windows' 260-character MAX_PATH limit on a real
    test message (SaveAsFile failed with a misleading "path does not exist"
    error). A short hash of the EntryID keeps paths well under the limit
    while staying unique per message.

    Passes the Sales mailbox's StoreID explicitly to GetItemFromID -- with
    two mailboxes in this profile, an EntryID should be resolved against the
    right store rather than relying on Outlook to guess.
    """
    namespace = _connect()
    sales_inbox = _get_sales_inbox(namespace)
    item = namespace.GetItemFromID(message_id, sales_inbox.StoreID)

    ATTACHMENT_DIR.mkdir(exist_ok=True)
    short_id = hashlib.sha256(message_id.encode()).hexdigest()[:16]
    paths = []
    for i in range(1, item.Attachments.Count + 1):
        att = item.Attachments.Item(i)
        name = att.FileName or ''
        if not name.lower().endswith('.xlsx'):
            continue
        safe_name = re.sub(r'[^\w.\-]', '_', name)
        path = ATTACHMENT_DIR / f'{short_id}_{safe_name}'
        att.SaveAsFile(str(path))
        paths.append(str(path))
    return paths


def _reviewed_category_name(namespace):
    """
    Find this profile's green category by color rather than a hardcoded
    name string -- confirmed live against this mailbox that it's already
    named "Green category" (color value 5), but matching on color is more
    robust against naming/locale differences. Creates it (color 5) only if
    genuinely missing.
    """
    categories = namespace.Categories
    for i in range(1, categories.Count + 1):
        cat = categories.Item(i)
        if cat.Color == 5 or 'green' in cat.Name.lower():
            return cat.Name
    return categories.Add('Green category', 5).Name


def mark_reviewed(message_id):
    """
    Tag a message with Outlook's green category so it's visually obvious in
    Outlook itself which emails have been fully reviewed (every lead pulled
    from it approved or rejected via the dashboard) -- NOT just ingested.
    Called from dashboard/app.py's approve/reject routes, once all leads tied
    to a message reach a decided state (see _maybe_mark_email_reviewed there),
    not at ingestion time. Appends to any existing categories on the message
    rather than overwriting them. Safe to call more than once (no-op if
    already tagged).
    """
    namespace = _connect()
    sales_inbox = _get_sales_inbox(namespace)
    item = namespace.GetItemFromID(message_id, sales_inbox.StoreID)

    green = _reviewed_category_name(namespace)
    existing = [c.strip() for c in (item.Categories or '').split(',') if c.strip()]
    if green not in existing:
        existing.append(green)
        item.Categories = ', '.join(existing)
        item.Save()


if __name__ == '__main__':
    try:
        msgs = fetch_new_messages()
        print(f'{len(msgs)} new message(s).')
        for m in msgs[:10]:
            print(f"  {m['received_at']}  {m['sender_email']}  {m['subject']!r}"
                  f"  attachments={m['has_attachments']}")
    except OutlookUnavailableError as e:
        print(f'ERROR: {e}')
