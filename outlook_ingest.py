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

This is the pipeline's only source of leads. There is deliberately no second
ingestion path: a Microsoft Graph backend used to sit alongside this one and
was removed on 2026-07-31, so that everything entering the system comes from
the Sales inbox and nowhere else.
"""

import hashlib
import html
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pywintypes
import pythoncom
import win32com.client

import db

ATTACHMENT_DIR = Path(__file__).parent / 'email_attachments'
MESSAGE_FILE_DIR = Path(__file__).parent / 'email_messages'
_LAST_CHECKED_KEY = 'outlook_last_checked_at'
_PENDING_CHECKED_KEY = 'outlook_pending_checked_at'
_OUTLOOK_DATE_FMT = '%m/%d/%Y %I:%M %p'   # format Outlook's Restrict() filter expects
_OL_MAIL_ITEM_CLASS = 43                  # olMail -- skip meeting requests, receipts, etc.
_OL_SAVE_AS_MSG = 3                       # olMSG -- full-fidelity Outlook message file
_OL_CATEGORY_GREEN = 5                    # olCategoryColorGreen


class OutlookUnavailableError(RuntimeError):
    """Raised when Outlook (or the specific Sales mailbox) can't be reached
    at all -- not installed, not signed in, mailbox not found, or COM
    automation blocked by local policy. Distinct from a per-message
    processing error so callers can tell "the whole backend is down, stop
    and flag it" apart from "one message failed, log and move on"."""


class OutlookForwardError(RuntimeError):
    """A forwarding failure annotated with the Outlook operation that failed."""

    def __init__(self, stage, original_error):
        self.stage = stage
        self.original_error = original_error
        super().__init__(f'Outlook failed while {stage}: {original_error}')


def _mailbox_name():
    with open(Path(__file__).parent / 'config.json') as f:
        config = json.load(f)
    return config.get('outlook', {}).get('mailbox_name', 'Sales')


def _forward_from_address():
    """SMTP identity used for dashboard forwards from the shared mailbox."""
    with open(Path(__file__).parent / 'config.json') as f:
        config = json.load(f)
    return config.get('outlook', {}).get('forward_from_address', 'sales@balseal.com')


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


def _conversation_id(item):
    """Outlook's stable thread identity, when the store exposes one."""
    try:
        return getattr(item, 'ConversationID', None) or None
    except (pywintypes.com_error, AttributeError):
        return None


def green_category_names(namespace=None, store=None):
    """
    Names of every green category defined on the Sales mailbox, lowercased.

    Matched by COLOUR, not by name. Categories live per-store, and the Sales
    mailbox's own list is what its messages are tagged from -- confirmed live
    on 2026-07-31, where the tag actually in use is named "Completed"
    (colour 5 = green) and "Green category" sits unused alongside it. A
    name-based check for the word "green" therefore matched nothing at all,
    which is why already-entered emails kept being re-ingested.

    Names containing "green" are included too, so a green category that only
    exists on a message (not in the store's list) is still recognised.
    """
    if store is None:
        namespace = namespace or _connect()
        store = _get_sales_inbox(namespace).Store
    names = set()
    try:
        categories = store.Categories
        for i in range(1, categories.Count + 1):
            category = categories.Item(i)
            if category.Color == _OL_CATEGORY_GREEN or 'green' in category.Name.lower():
                names.add(category.Name.strip().lower())
    except (pywintypes.com_error, AttributeError):
        # Older Outlook builds don't expose Store.Categories. Fall back to the
        # name-based check alone rather than failing the run.
        pass
    names.add('green category')
    return names


def _has_green_category(item, green_names):
    """Whether this mail item carries one of the mailbox's green categories --
    the operator's "already entered" marker."""
    categories_str = getattr(item, 'Categories', None) or ''
    for cat in categories_str.split(','):
        cat = cat.strip().lower()
        if cat and (cat in green_names or 'green' in cat):
            return True
    return False


def is_reviewed(message_id):
    """
    Whether the message behind this EntryID is tagged green in Outlook right
    now. Used for messages reconstructed from the database (the retry path),
    which carry no category information of their own.
    """
    try:
        namespace = _connect()
        sales_inbox = _get_sales_inbox(namespace)
        item = namespace.GetItemFromID(message_id, sales_inbox.StoreID)
        return _has_green_category(item, green_category_names(store=sales_inbox.Store))
    except (pywintypes.com_error, OutlookUnavailableError, AttributeError):
        # Can't tell -- let the caller ingest it rather than silently dropping
        # a lead because Outlook was momentarily unreachable.
        return False


def _item_to_message_dict(item, green_names):
    received_at = item.ReceivedTime
    received_at_str = received_at.isoformat() if hasattr(received_at, 'isoformat') else str(received_at)

    # Category first, and bail out before touching anything expensive. Reading
    # .Body, resolving an Exchange sender through GetExchangeUser() and
    # counting .Attachments are each a separate COM round-trip, and a
    # green-tagged message is discarded by pipeline_ingest's gate without any
    # of them ever being read. Only the subject is kept, for the skip log.
    if _has_green_category(item, green_names):
        return {
            'id': item.EntryID,
            'conversation_id': _conversation_id(item),
            'received_at': received_at_str,
            'sender_email': None,
            'sender_name': None,
            'subject': item.Subject,
            'body_text': '',
            'has_attachments': False,
            'attachments': [],
            'reviewed_in_outlook': True,
        }

    return {
        'id': item.EntryID,
        'conversation_id': _conversation_id(item),
        'received_at': received_at_str,
        'sender_email': _sender_smtp_address(item),
        'sender_name': getattr(item, 'SenderName', None),
        'subject': item.Subject,
        'body_text': item.Body or '',
        'has_attachments': item.Attachments.Count > 0,
        'attachments': list_attachments(item),
        'reviewed_in_outlook': False,
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
    green_names = green_category_names(store=inbox.Store)

    filter_str = f"[ReceivedTime] > '{last_checked.strftime(_OUTLOOK_DATE_FMT)}'"
    restricted = inbox.Items.Restrict(filter_str)

    messages = []
    for item in restricted:
        if getattr(item, 'Class', None) != _OL_MAIL_ITEM_CLASS:
            continue
        try:
            messages.append(_item_to_message_dict(item, green_names))
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
    green_names = green_category_names(store=inbox.Store)
    items = inbox.Items
    items.Sort('[ReceivedTime]', True)  # descending -- newest first

    messages = []
    for item in items:
        if len(messages) >= count:
            break
        if getattr(item, 'Class', None) != _OL_MAIL_ITEM_CLASS:
            continue
        try:
            messages.append(_item_to_message_dict(item, green_names))
        except pywintypes.com_error:
            continue

    return messages


# Outlook exposes a signature logo or any other embedded image as an ordinary
# attachment. These names are what Outlook and most mail clients auto-assign
# to them, and a lead's attachment list is useless if every message shows
# three copies of the sender's letterhead.
_EMBEDDED_IMAGE_RE = re.compile(r'^(?:image|oledata|logo)\d*\.(?:png|jpe?g|gif|bmp|emf|wmf)$', re.IGNORECASE)

_ATTACHMENT_KINDS = {
    'image': ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tif', '.tiff', '.heic', '.webp'),
    'drawing/CAD': ('.dwg', '.dxf', '.step', '.stp', '.iges', '.igs', '.sldprt', '.sldasm', '.ipt', '.catpart'),
    'document': ('.pdf', '.doc', '.docx', '.rtf', '.txt', '.odt'),
    'spreadsheet': ('.xls', '.xlsx', '.xlsm', '.csv', '.ods'),
    'presentation': ('.ppt', '.pptx'),
    'archive': ('.zip', '.7z', '.rar', '.tar', '.gz'),
    'email': ('.msg', '.eml'),
}


def _attachment_kind(name):
    suffix = Path(name).suffix.lower()
    for kind, suffixes in _ATTACHMENT_KINDS.items():
        if suffix in suffixes:
            return kind
    return suffix.lstrip('.') or 'file'


def list_attachments(item):
    """
    Describe what a message carries, WITHOUT saving anything.

    Returns [{name, size, kind}] -- enough for a reviewer to see that a
    customer sent a photo of a worn seal or a drawing, and to go find it in
    Outlook. Nothing is written to disk on purpose: see db.record_attachments.

    Embedded signature images are filtered out; they are attachments as far as
    Outlook is concerned but never something a rep needs to know about.
    """
    attachments = []
    try:
        count = item.Attachments.Count
    except (pywintypes.com_error, AttributeError):
        return attachments

    for i in range(1, count + 1):
        try:
            att = item.Attachments.Item(i)
            name = (att.FileName or '').strip()
            if not name or _EMBEDDED_IMAGE_RE.match(name):
                continue
            attachments.append({
                'name': name[:200],
                'size': int(getattr(att, 'Size', 0) or 0),
                'kind': _attachment_kind(name),
            })
        except (pywintypes.com_error, AttributeError, ValueError):
            # One unreadable attachment shouldn't cost us the rest of the list.
            continue
    return attachments


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


def save_message_file(message_id, subject=None):
    """
    Save the whole original email to disk as a .msg file and return its path
    (None if it couldn't be saved).

    This is what eventually lands in the Oracle lead's Attachments section, so
    it deliberately saves the *real* message rather than the cleaned body text
    the extractor works from: a .msg keeps the formatting, the full header
    block, the thread history and any files the sender attached, and opens
    natively in Outlook for whoever picks the lead up in Oracle.

    Called at ingest time, not at submission time -- the message is guaranteed
    to still be in the Sales inbox while we're processing it, whereas a submit
    run happens later and may not even have Outlook reachable.

    Same short-hash filename scheme as download_excel_attachments(), and for
    the same reason: an Outlook EntryID is ~140 characters and blows past
    Windows' MAX_PATH when used verbatim in a filename.
    """
    MESSAGE_FILE_DIR.mkdir(exist_ok=True)
    short_id = hashlib.sha256(message_id.encode()).hexdigest()[:16]
    safe_subject = re.sub(r'[^\w.\-]', '_', (subject or 'email').strip())[:60].strip('_')
    path = MESSAGE_FILE_DIR / f'{short_id}_{safe_subject or "email"}.msg'
    if path.exists():
        return str(path)

    try:
        namespace = _connect()
        sales_inbox = _get_sales_inbox(namespace)
        item = namespace.GetItemFromID(message_id, sales_inbox.StoreID)
        item.SaveAs(str(path), _OL_SAVE_AS_MSG)
    except (pywintypes.com_error, OutlookUnavailableError, AttributeError, OSError):
        # Best-effort: the pipeline still has the cached body text to fall back
        # on, so a message that can't be exported is not worth failing an
        # ingest run over. Caller decides whether to log.
        return None
    return str(path) if path.exists() else None


def _prepend_forward_note(forwarded, note):
    """Add an operator's plain-text note without flattening an HTML email."""
    note = (note or '').strip()
    if not note:
        return
    # olFormatPlain = 1. Assigning Body on an HTML message converts it to plain
    # text and destroys the source formatting, so use the matching body field.
    if getattr(forwarded, 'BodyFormat', None) == 1:
        forwarded.Body = f'{note}\r\n\r\n{forwarded.Body or ""}'
        return
    safe_note = html.escape(note).replace('\r\n', '\n').replace('\r', '\n')
    safe_note = safe_note.replace('\n', '<br>')
    forwarded.HTMLBody = f'<div>{safe_note}</div><br>{forwarded.HTMLBody or ""}'


def forward_message(message_id, recipient, note=None, fallback_message_path=None,
                    forward_from_address=None):
    """Forward a Sales-inbox message to one SMTP recipient.

    Forwarding the Outlook item (rather than composing a new message from the
    cached body) preserves the original formatting and attachments.  Resolve
    the recipient before sending so an unresolvable address cannot silently
    become a draft addressed to the wrong Outlook contact.
    """
    # Flask serves dashboard requests on worker threads.  COM initialization
    # is per-thread on Windows; Outlook may be open and reachable from the main
    # process while Dispatch() on a Flask thread still fails with
    # CO_E_NOTINITIALIZED ("CoInitialize has not been called").
    pythoncom.CoInitialize()
    namespace = None
    sales_inbox = None
    item = None
    item_opened_from_file = False
    forwarded = None
    sent = False
    stage = 'connecting to Outlook'
    try:
        namespace = _connect()
        stage = 'opening the source email'
        sales_inbox = _get_sales_inbox(namespace)
        try:
            item = namespace.GetItemFromID(message_id, sales_inbox.StoreID)
        except Exception:
            # EntryIDs stop resolving when a message is moved, removed, or
            # quarantined after ingestion. Ingestion also exports the original
            # .msg specifically so later workflows retain a full-fidelity copy.
            fallback = Path(fallback_message_path) if fallback_message_path else None
            if not fallback or not fallback.is_file():
                raise
            stage = 'opening the preserved source email'
            item = namespace.OpenSharedItem(str(fallback))
            item_opened_from_file = True
        stage = 'creating the forward'
        forwarded = item.Forward()

        # A shared mailbox is mounted as a Store, not listed in Accounts, so it
        # cannot be assigned through SendUsingAccount. SentOnBehalfOfName is
        # Outlook's supported route for Send As / Send on Behalf. Use the full
        # SMTP address rather than the ambiguous display name "Sales".
        stage = 'selecting the sending account'
        forwarded.SentOnBehalfOfName = (
            forward_from_address or _forward_from_address()
        )

        stage = 'adding the forward message'
        _prepend_forward_note(forwarded, note)

        stage = 'resolving the recipient'
        forwarded.Recipients.Add(recipient)
        if not forwarded.Recipients.ResolveAll():
            raise ValueError(f'Outlook could not resolve the recipient {recipient}.')

        stage = 'sending the forwarded email'
        forwarded.Send()
        sent = True
    except (OutlookUnavailableError, ValueError):
        raise
    except Exception as exc:
        raise OutlookForwardError(stage, exc) from exc
    finally:
        if forwarded is not None and not sent:
            try:
                forwarded.Close(1)  # olDiscard -- do not leave a failed draft behind
            except Exception:
                pass
        if item_opened_from_file and item is not None:
            try:
                item.Close(1)  # olDiscard -- the preserved .msg is read-only input
            except Exception:
                pass
        # Release the Outlook proxies before uninitializing this thread's COM
        # apartment.  The Flask worker can then be safely reused.
        forwarded = None
        item = None
        sales_inbox = None
        namespace = None
        pythoncom.CoUninitialize()


def _reviewed_category_name(store):
    """
    The green category to stamp on a fully-reviewed email, taken from the
    SALES mailbox's category list -- not namespace.Categories, which is the
    signed-in user's personal store and a different list entirely. Reading the
    wrong one meant the pipeline tagged with "Green category" while the
    operators tag with "Completed"; both are green, but they were never the
    same marker.

    Picks the first green category the mailbox already defines (currently
    "Completed") so pipeline-marked and hand-marked mail look identical.
    Creates one only if the mailbox genuinely has none.
    """
    categories = store.Categories
    for i in range(1, categories.Count + 1):
        cat = categories.Item(i)
        if cat.Color == _OL_CATEGORY_GREEN:
            return cat.Name
    for i in range(1, categories.Count + 1):
        cat = categories.Item(i)
        if 'green' in cat.Name.lower():
            return cat.Name
    return categories.Add('Green category', _OL_CATEGORY_GREEN).Name


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

    green = _reviewed_category_name(sales_inbox.Store)
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
