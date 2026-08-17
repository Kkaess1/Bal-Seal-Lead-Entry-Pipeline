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
import os
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
_OL_RECIPIENT_TO = 1                      # olTo
_OL_RECIPIENT_CC = 2                      # olCC (3 is olBCC -- never collected)
_MAX_PATH = 240                           # under Windows' 260, leaving headroom


class OutlookUnavailableError(RuntimeError):
    """Raised when Outlook (or the specific Sales mailbox) can't be reached
    at all -- not installed, not signed in, mailbox not found, or COM
    automation blocked by local policy. Distinct from a per-message
    processing error so callers can tell "the whole backend is down, stop
    and flag it" apart from "one message failed, log and move on"."""


class OutlookOperationError(RuntimeError):
    """An Outlook failure annotated with the operation that was in progress,
    so a caller can tell the operator *which* step failed rather than showing
    a raw COM error."""

    def __init__(self, stage, original_error):
        self.stage = stage
        self.original_error = original_error
        super().__init__(f'Outlook failed while {stage}: {original_error}')


class OutlookForwardError(OutlookOperationError):
    """A forwarding failure annotated with the Outlook operation that failed."""


class OutlookReplyError(OutlookOperationError):
    """A reply failure annotated with the Outlook operation that failed."""


class OutlookDeleteError(OutlookOperationError):
    """A deletion failure annotated with the Outlook operation that failed."""


def _mailbox_name():
    with open(Path(__file__).parent / 'config.json') as f:
        config = json.load(f)
    return config.get('outlook', {}).get('mailbox_name', 'Sales')


def forward_from_address():
    """SMTP identity dashboard forwards and replies are sent from. Public
    because the dashboard shows the operator which address a reply will appear
    to come from before they send it."""
    with open(Path(__file__).parent / 'config.json') as f:
        config = json.load(f)
    return config.get('outlook', {}).get('forward_from_address', 'sales@balseal.com')


def deletion_enabled():
    """
    Whether the dashboard may delete mail out of the Sales inbox at all
    (config.json -> outlook.allow_email_delete, default on).

    Same purpose as review_marking.marking_enabled(): a switch that keeps a
    test or evaluation run from altering the real shared mailbox. Unlike that
    one, an unreadable/invalid config fails CLOSED -- refusing to delete when
    we can't read our own settings costs an operator one manual click, while
    guessing "allowed" would remove somebody's mail on a bad config.
    """
    try:
        with open(Path(__file__).parent / 'config.json', encoding='utf-8') as f:
            config = json.load(f)
    except (OSError, ValueError):
        return False
    return bool(config.get('outlook', {}).get('allow_email_delete', True))


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


def _recipient_entry(recipient):
    """
    One Outlook recipient as {'name', 'address'}.

    Exchange recipients carry an X.500 legacy DN in .Address rather than a
    usable address -- the same wrinkle _sender_smtp_address() handles for the
    sender -- so resolve through the Exchange user where the store allows it.
    """
    address = None
    try:
        entry = recipient.AddressEntry
        exch_user = entry.GetExchangeUser() if entry else None
        if exch_user and exch_user.PrimarySmtpAddress:
            address = exch_user.PrimarySmtpAddress
    except (pywintypes.com_error, AttributeError):
        address = None
    if not address:
        address = getattr(recipient, 'Address', None)
    return {
        'name': str(getattr(recipient, 'Name', None) or '').strip(),
        'address': str(address or '').strip(),
    }


def message_recipient_list(item):
    """
    Who an email was addressed to: {'to': [...], 'cc': [...]}, each entry a
    {'name', 'address'} dict.

    This is what makes the dashboard's reply-all choice an informed one --
    without it the operator cannot see that answering will also reach the
    customer's colleague or their distributor.

    BCC is deliberately not collected. A received message should not expose
    one, and a blind copy is not something to surface as "who else is here".

    Never raises: a message whose recipients cannot be read is worth ingesting
    anyway, and the reply dialog says so rather than claiming nobody is there.
    """
    recipients = {'to': [], 'cc': []}
    try:
        items = item.Recipients
        count = items.Count
    except (pywintypes.com_error, AttributeError):
        return recipients

    for i in range(1, count + 1):
        try:
            recipient = items.Item(i)
            entry = _recipient_entry(recipient)
            if not entry['address'] and not entry['name']:
                continue
            kind = getattr(recipient, 'Type', _OL_RECIPIENT_TO)
            if kind == _OL_RECIPIENT_CC:
                recipients['cc'].append(entry)
            elif kind == _OL_RECIPIENT_TO:
                recipients['to'].append(entry)
        except (pywintypes.com_error, AttributeError):
            continue
    return recipients


def message_recipients(message_id, fallback_message_path=None):
    """
    Read To/CC off a Sales-inbox message on demand.

    Ingestion records this now, but every message ingested before it did has
    nothing stored, and those are exactly the leads sitting in the queue
    today. The dashboard calls this when the reply dialog is opened -- the one
    moment the answer is actually wanted -- rather than paying a COM round-trip
    on every page render.
    """
    pythoncom.CoInitialize()
    item = None
    item_opened_from_file = False
    try:
        namespace = _connect()
        sales_inbox = _get_sales_inbox(namespace)
        try:
            item = namespace.GetItemFromID(message_id, sales_inbox.StoreID)
        except Exception:
            fallback = Path(fallback_message_path) if fallback_message_path else None
            if not fallback or not fallback.is_file():
                raise
            item = namespace.OpenSharedItem(str(fallback))
            item_opened_from_file = True
        return message_recipient_list(item)
    finally:
        if item_opened_from_file and item is not None:
            try:
                item.Close(1)  # olDiscard -- the preserved .msg is read-only input
            except Exception:
                pass
        item = None
        pythoncom.CoUninitialize()


def conversation_messages(message_id, limit=25):
    """
    The other messages in this email's Outlook conversation, oldest first, as
    {id, received_at, sender_email, sender_name, subject, body_text} dicts.

    Why this exists: a reply is ingested on its own, but the messages it is
    replying to usually are not -- they were tagged green when they were first
    handled, so the pipeline skips them and never stores them. The reviewer
    then sees a bare "RE: ..." with no idea what it answers, which is exactly
    the context needed to judge whether to forward it.

    Reads across the Sales mailbox's folders, so a reply the team already sent
    from Sent Items shows up too -- that is part of "the whole string". Items
    that do not resolve inside the Sales store are skipped, keeping the
    module's rule that it never reads the personal mailbox.

    Never raises: thread context is a nicety, and failing to build it must not
    cost an ingest run the lead itself.

    Walks the conversation with GetRootItems/GetChildren, which hand back real
    Outlook items. The Table API looks tidier but returns a 48-character
    EntryID that GetItemFromID cannot resolve ("The operation failed"), so
    every message came back unreadable -- confirmed live on 2026-08-07.

    Returns every message in the thread including, usually, the one asked
    about: an item reached through the conversation reports a different
    EntryID than the one it was fetched with, so "is this the same message"
    cannot be answered here. Callers de-duplicate on sender and timestamp.
    """
    messages = []
    try:
        pythoncom.CoInitialize()
        namespace = _connect()
        sales_inbox = _get_sales_inbox(namespace)
        item = namespace.GetItemFromID(message_id, sales_inbox.StoreID)
        for node in _walk_conversation(item, limit=limit):
            try:
                received_at = getattr(node, 'ReceivedTime', None) or getattr(
                    node, 'SentOn', None
                )
                messages.append({
                    'id': node.EntryID,
                    'conversation_id': _conversation_id(node),
                    'received_at': (
                        received_at.isoformat() if hasattr(received_at, 'isoformat')
                        else str(received_at or '')
                    ),
                    'sender_email': _sender_smtp_address(node),
                    'sender_name': getattr(node, 'SenderName', None),
                    'subject': node.Subject,
                    'body_text': node.Body or '',
                })
            except (pywintypes.com_error, AttributeError):
                # One unreadable message in a thread should not cost the rest.
                continue
    except Exception:
        return messages

    messages.sort(key=lambda m: m['received_at'] or '')
    return messages


def _walk_conversation(item, limit=25):
    """
    Yield the live Outlook items in this item's conversation.

    Item objects, not ids: an item reached this way reports an EntryID that
    GetItemFromID cannot resolve afterwards (confirmed against the Sales
    mailbox), so anything that needs to act on a thread message -- replying to
    one, in particular -- has to find it here rather than store its id and
    come back for it later.
    """
    conversation = item.GetConversation()
    if conversation is None:
        return

    seen = set()
    pending = []
    roots = conversation.GetRootItems()
    for i in range(1, roots.Count + 1):
        pending.append(roots.Item(i))

    yielded = 0
    while pending and yielded < limit:
        node = pending.pop(0)
        try:
            entry_id = node.EntryID
            if entry_id in seen:
                continue
            seen.add(entry_id)

            children = conversation.GetChildren(node)
            for i in range(1, children.Count + 1):
                pending.append(children.Item(i))

            if getattr(node, 'Class', None) != _OL_MAIL_ITEM_CLASS:
                continue
        except (pywintypes.com_error, AttributeError):
            continue
        yielded += 1
        yield node


def _find_thread_item(anchor_item, received_at, sender_email):
    """
    The live Outlook item for one message of a thread, matched on timestamp
    and sender -- the pair the dashboard holds and the only identity that
    survives, since conversation EntryIDs do not resolve later.

    Returns None when it cannot be found, so callers fall back to the message
    they do have rather than sending something to the wrong person.
    """
    wanted_sender = (sender_email or '').strip().lower()
    wanted_time = (received_at or '')[:19]
    if not wanted_time:
        return None
    for node in _walk_conversation(anchor_item):
        try:
            node_time = getattr(node, 'ReceivedTime', None) or getattr(node, 'SentOn', None)
            node_time = node_time.isoformat()[:19] if hasattr(node_time, 'isoformat') else ''
            node_sender = (_sender_smtp_address(node) or '').strip().lower()
        except (pywintypes.com_error, AttributeError):
            continue
        if node_time == wanted_time and (not wanted_sender or node_sender == wanted_sender):
            return node
    return None


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
            'recipients': {'to': [], 'cc': []},
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
        'recipients': message_recipient_list(item),
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


def _visible_attachments(item):
    """
    The attachments worth showing a person, as (outlook_index, name, size).

    Shared by list_attachments() and save_attachment() so the position of an
    attachment in the stored list always means the same file. Outlook's own
    index cannot be stored instead: embedded signature images are skipped
    here, so the two numberings differ.
    """
    found = []
    try:
        count = item.Attachments.Count
    except (pywintypes.com_error, AttributeError):
        return found

    for i in range(1, count + 1):
        try:
            att = item.Attachments.Item(i)
            name = (att.FileName or '').strip()
            if not name or _EMBEDDED_IMAGE_RE.match(name):
                continue
            found.append((i, name[:200], int(getattr(att, 'Size', 0) or 0)))
        except (pywintypes.com_error, AttributeError, ValueError):
            # One unreadable attachment shouldn't cost us the rest of the list.
            continue
    return found


def list_attachments(item):
    """
    Describe what a message carries, WITHOUT saving anything.

    Returns [{name, size, kind}] -- enough for a reviewer to see that a
    customer sent a photo of a worn seal or a drawing. The files themselves
    are still not written to disk at ingest: they are fetched one at a time,
    on demand, only when someone asks to open one (see save_attachment).

    Embedded signature images are filtered out; they are attachments as far as
    Outlook is concerned but never something a rep needs to know about.
    """
    return [
        {'name': name, 'size': size, 'kind': _attachment_kind(name)}
        for _, name, size in _visible_attachments(item)
    ]


def save_attachment(message_id, index, dest_dir, fallback_message_path=None):
    """
    Write one attachment to `dest_dir` and return (path, filename).

    `index` is the position in the list stored at ingest (see
    _visible_attachments), not Outlook's own attachment index.

    Fetched from Outlook on demand rather than saved during ingestion: the
    mailbox stays the only copy until somebody actually wants the file, and a
    lead nobody opens never puts a stranger's attachment on this disk at all.

    The saved name is Outlook's, sanitised and prefixed with a hash of the
    message id -- an EntryID is ~140 characters and using it verbatim pushes
    the path past Windows' MAX_PATH (see download_excel_attachments).
    """
    pythoncom.CoInitialize()
    item = None
    item_opened_from_file = False
    try:
        namespace = _connect()
        sales_inbox = _get_sales_inbox(namespace)
        try:
            item = namespace.GetItemFromID(message_id, sales_inbox.StoreID)
        except Exception:
            # Same stale-EntryID fallback as forwarding: the .msg exported at
            # ingest still carries the attachments.
            fallback = Path(fallback_message_path) if fallback_message_path else None
            if not fallback or not fallback.is_file():
                raise
            item = namespace.OpenSharedItem(str(fallback))
            item_opened_from_file = True

        visible = _visible_attachments(item)
        if not 0 <= index < len(visible):
            raise IndexError(f'This email has no attachment {index}.')
        outlook_index, name, _ = visible[index]

        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        short_id = hashlib.sha256(f'{message_id}:{index}'.encode()).hexdigest()[:16]
        # Separators become underscores, so nothing can name a directory, and
        # runs of dots collapse -- "..\..\.env" saves as "env", not
        # ".._.._.env". The hash prefix keeps two attachments distinct even
        # when sanitising leaves them with the same name.
        safe_name = re.sub(r'[^\w.\-]', '_', name)
        safe_name = re.sub(r'\.{2,}', '.', safe_name).strip('._-') or 'attachment'

        # Budget the whole path against Windows' 260-character MAX_PATH, not
        # just the filename: attachment names are sender-controlled and the
        # cache folder may sit deep. SaveAsFile reports this as "Path does not
        # exist", which sends you looking in entirely the wrong place --
        # confirmed live on a 63-character attachment name.
        stem, suffix = os.path.splitext(safe_name)
        room = _MAX_PATH - len(str(dest_dir)) - len(short_id) - len(suffix) - 2
        if room < 1:
            raise OSError(
                f'The path for this attachment would exceed {_MAX_PATH} characters. '
                f'Move the cache folder somewhere shallower.'
            )
        path = dest_dir / f'{short_id}_{stem[:room]}{suffix}'
        if not path.exists():
            item.Attachments.Item(outlook_index).SaveAsFile(str(path))
        return str(path), name
    finally:
        if item_opened_from_file and item is not None:
            try:
                item.Close(1)  # olDiscard -- the preserved .msg is read-only input
            except Exception:
                pass
        item = None
        pythoncom.CoUninitialize()


def open_in_outlook(message_id):
    """
    Open the source email in Outlook itself.

    The safest way to deal with a stranger's attachment: Outlook opens it
    under the protections the mail gateway and Office already apply to it
    (Protected View in particular), which a file downloaded through the
    dashboard does not get.
    """
    pythoncom.CoInitialize()
    try:
        namespace = _connect()
        sales_inbox = _get_sales_inbox(namespace)
        item = namespace.GetItemFromID(message_id, sales_inbox.StoreID)
        item.Display()
        return True
    finally:
        pythoncom.CoUninitialize()


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


def _prepend_operator_note(forwarded, note):
    """Add an operator's plain-text note above an existing message body,
    without flattening an HTML email. Used for both the note on a forward and
    the body of a reply -- in each case Outlook has already built the message
    with the original quoted underneath, and this puts the typed text on top."""
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


def forward_message(message_id, recipients, note=None, cc_addresses=None,
                    fallback_message_path=None, from_address=None):
    """Forward a Sales-inbox message to one or more SMTP recipients.

    `recipients` are addressed and `cc_addresses` are copied -- handing an
    inquiry to the person who should own it while keeping their manager or the
    territory's rep visibly in the loop, which is the ordinary way this gets
    passed on. A single address may be passed as a plain string.

    Forwarding the Outlook item (rather than composing a new message from the
    cached body) preserves the original formatting and attachments.  Resolve
    the recipients before sending so an unresolvable address cannot silently
    become a draft addressed to the wrong Outlook contact.
    """
    if isinstance(recipients, str):
        recipients = [recipients]
    recipients = [a for a in (recipients or []) if a]
    cc_addresses = [a for a in (cc_addresses or []) if a]
    if not recipients:
        raise ValueError('Enter at least one address to forward to.')
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
        forwarded.SentOnBehalfOfName = from_address or forward_from_address()

        stage = 'adding the forward message'
        _prepend_operator_note(forwarded, note)

        stage = 'resolving the recipients'
        for address in recipients:
            forwarded.Recipients.Add(address)
        for address in cc_addresses:
            # Add() defaults to the To line; the copy only becomes a copy once
            # its Type says so -- the same wrinkle reply_message() handles.
            forwarded.Recipients.Add(address).Type = _OL_RECIPIENT_CC
        if not forwarded.Recipients.ResolveAll():
            raise ValueError(
                'Outlook could not resolve '
                + ', '.join(recipients + cc_addresses) + '.'
            )

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


def _recipient_smtp_addresses(mail_item):
    """
    Best-effort list of the SMTP addresses a composed message is addressed to.

    Recorded alongside the sent reply so the dashboard can show exactly who was
    written to -- which is the whole point of the reply-all option. Exchange
    recipients carry an X.500 legacy DN in .Address rather than a usable
    address, the same wrinkle _sender_smtp_address() handles for inbound mail,
    so resolve through the Exchange user where possible and fall back to the
    display name. Purely informational: never raises, because failing to build
    a label must not stop (or undo) a message that Outlook is about to send.
    """
    addresses = []
    try:
        recipients = mail_item.Recipients
        for i in range(1, recipients.Count + 1):
            entry = _recipient_entry(recipients.Item(i))
            address = entry['address'] or entry['name']
            if address:
                addresses.append(address)
    except (pywintypes.com_error, AttributeError):
        pass
    return addresses


def reply_message(message_id, body, reply_all=False, fallback_message_path=None,
                  from_address=None, to_addresses=None, cc_addresses=None,
                  thread_target=None):
    """Reply to a Sales-inbox message, and return who Outlook sent it to.

    `to_addresses` replaces the recipients Outlook chose with an explicit list,
    and `cc_addresses` are copied rather than addressed. Answering a thread is
    rarely "the sender" or "everyone": a colleague's follow-up should usually
    go back to them alone, with the customer copied in only if that is
    deliberate. When either is given, the reply-all flag is ignored -- the
    caller has already decided.

    `thread_target` is {'received_at', 'sender_email'} naming one message of
    the conversation to reply *to*, so the quoted text and threading match
    that message rather than whichever one the lead was ingested from. Falls
    back to the ingested message if it cannot be found.

    Replying to the Outlook item rather than composing a new message keeps the
    conversation intact: Outlook fills in the recipients, the Re: subject and
    the quoted original, and the customer's mail client threads the answer
    under their own inquiry. The operator's text goes above the quote.

    `reply_all` includes everyone the original was addressed to. It is off by
    default because a lead inquiry routinely carries CCs -- a distributor, a
    colleague, a web-form robot -- and answering all of them should be a
    deliberate choice, not what happens when nobody thought about it.

    Sends from the shared mailbox identity, not the signed-in operator, for the
    same Send As / Send on Behalf reasons documented in forward_message().
    """
    body = (body or '').strip()
    if not body:
        raise ValueError('Write a message before sending the reply.')

    # Per-thread COM initialization for Flask workers -- see forward_message().
    pythoncom.CoInitialize()
    namespace = None
    sales_inbox = None
    item = None
    item_opened_from_file = False
    reply = None
    sent = False
    recipients = []
    stage = 'connecting to Outlook'
    try:
        namespace = _connect()
        stage = 'opening the source email'
        sales_inbox = _get_sales_inbox(namespace)
        try:
            item = namespace.GetItemFromID(message_id, sales_inbox.StoreID)
        except Exception:
            # Same stale-EntryID fallback as forwarding: replying to the
            # preserved .msg still reaches the right person with the right
            # subject, even once the live item has been moved or removed.
            fallback = Path(fallback_message_path) if fallback_message_path else None
            if not fallback or not fallback.is_file():
                raise
            stage = 'opening the preserved source email'
            item = namespace.OpenSharedItem(str(fallback))
            item_opened_from_file = True

        if thread_target:
            stage = 'finding that message in the thread'
            target = _find_thread_item(
                item, thread_target.get('received_at'), thread_target.get('sender_email'),
            )
            if target is not None:
                item = target

        stage = 'creating the reply'
        chose_recipients = bool(to_addresses or cc_addresses)
        reply = item.ReplyAll() if (reply_all and not chose_recipients) else item.Reply()

        stage = 'selecting the sending account'
        reply.SentOnBehalfOfName = from_address or forward_from_address()

        stage = 'adding the reply message'
        _prepend_operator_note(reply, body)

        if chose_recipients:
            # Clear what Outlook chose before adding the operator's list --
            # otherwise the sender is addressed twice and anyone they removed
            # is still on it. Removing shifts the collection, so always take
            # index 1.
            stage = 'setting the recipients'
            while reply.Recipients.Count > 0:
                reply.Recipients.Remove(1)
            for address in to_addresses or []:
                reply.Recipients.Add(address)
            for address in cc_addresses or []:
                # Add() defaults to the To line; the copy only becomes a copy
                # once its Type says so.
                reply.Recipients.Add(address).Type = _OL_RECIPIENT_CC

        stage = 'resolving the recipients'
        if not reply.Recipients.ResolveAll():
            raise ValueError(
                'Outlook could not resolve who this email should be replied to.'
            )
        if reply.Recipients.Count < 1:
            # A no-reply/robot sender can leave Outlook with nothing to reply
            # to. Better to say so than to hand Outlook an empty To: line.
            raise ValueError('The original email has no address to reply to.')
        recipients = _recipient_smtp_addresses(reply)

        stage = 'sending the reply'
        reply.Send()
        sent = True
        return recipients
    except (OutlookUnavailableError, ValueError):
        raise
    except Exception as exc:
        raise OutlookReplyError(stage, exc) from exc
    finally:
        if reply is not None and not sent:
            try:
                reply.Close(1)  # olDiscard -- do not leave a failed draft behind
            except Exception:
                pass
        if item_opened_from_file and item is not None:
            try:
                item.Close(1)  # olDiscard -- the preserved .msg is read-only input
            except Exception:
                pass
        reply = None
        item = None
        sales_inbox = None
        namespace = None
        pythoncom.CoUninitialize()


def delete_message(message_id):
    """Delete one Sales-inbox message, and return whether anything was deleted.

    Recoverable on purpose: MailItem.Delete() does exactly what the Delete key
    does -- the message lands in the Sales mailbox's own Deleted Items folder
    and can be dragged back -- rather than purging it. Nothing in this pipeline
    should be able to destroy a customer's email outright.

    Returns False (not an error) when the EntryID no longer resolves: the
    message has already been deleted or moved out of the mailbox by hand, so
    the caller's actual goal -- that email not being in the inbox -- already
    holds. True means this call deleted it.

    Note that deleting moves the item between folders, which changes its
    EntryID, so the stored source_message_id goes stale afterwards. That only
    costs the ability to re-open the live item; ingestion already exported the
    full-fidelity .msg (see save_message_file), which is what later workflows
    fall back to.

    Refuses to touch anything outside the Sales mailbox, and refuses outright
    when outlook.allow_email_delete is off -- see deletion_enabled().
    """
    if not deletion_enabled():
        raise ValueError(
            'Deleting email from the Sales inbox is turned off in config.json '
            '(outlook.allow_email_delete).'
        )

    # Same per-thread COM initialization the dashboard's forward path needs --
    # see forward_message() for why Flask worker threads require this.
    pythoncom.CoInitialize()
    namespace = None
    sales_inbox = None
    item = None
    stage = 'connecting to Outlook'
    try:
        namespace = _connect()
        stage = 'opening the Sales inbox'
        sales_inbox = _get_sales_inbox(namespace)
        try:
            item = namespace.GetItemFromID(message_id, sales_inbox.StoreID)
        except Exception:
            return False  # already gone -- nothing left to delete

        # GetItemFromID is scoped to the Sales store, but confirm it against the
        # item we actually got before deleting anything. This module's whole
        # premise is that it never touches the personal mailbox sitting beside
        # the Sales one in this Outlook profile, and deletion is the one
        # operation here that can't be undone by re-running the pipeline.
        # A store that doesn't expose Parent.StoreID isn't treated as a
        # mismatch -- only a positively wrong store blocks the delete.
        stage = 'checking the message is in the Sales mailbox'
        try:
            parent_store_id = item.Parent.StoreID
        except (pywintypes.com_error, AttributeError):
            parent_store_id = None
        if parent_store_id and parent_store_id != sales_inbox.StoreID:
            raise ValueError(
                'That message is not in the Sales mailbox, so it was not deleted.'
            )

        stage = 'deleting the email'
        item.Delete()
        return True
    except (OutlookUnavailableError, ValueError):
        raise
    except Exception as exc:
        raise OutlookDeleteError(stage, exc) from exc
    finally:
        # Release the Outlook proxies before uninitializing this thread's COM
        # apartment, so the Flask worker can be safely reused.
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


def apply_green_category(message_id):
    """
    Tag a message with the Sales mailbox's green category.

    Appends to whatever categories the message already carries rather than
    overwriting them, and is a no-op when the tag is already there, so it is
    safe to call more than once on the same message.

    CoInitialize because this is called from the dashboard's Flask worker
    threads as well as from the ingest script's main thread; COM
    initialization is per-thread on Windows.

    It is deliberately NOT paired with CoUninitialize, unlike forward/reply/
    delete. Those run once per request on a worker thread; this runs after
    every ingested message, in the middle of a run that is already using COM.
    Uninitializing there tore down the whole thread's apartment -- pywin32's
    own implicit initialization included -- and every later Outlook call in
    that run died with "CoInitialize has not been called". That is not
    theoretical: it cost a real ingest run on 2026-08-07, which tagged one
    email green and then failed on the next message.

    Leaving the apartment up costs nothing. The ingest script exits at the end
    of its run, and a Flask worker wants COM initialized anyway.
    """
    pythoncom.CoInitialize()
    namespace = _connect()
    sales_inbox = _get_sales_inbox(namespace)
    item = namespace.GetItemFromID(message_id, sales_inbox.StoreID)

    green = _reviewed_category_name(sales_inbox.Store)
    existing = [c.strip() for c in (item.Categories or '').split(',') if c.strip()]
    if green not in existing:
        existing.append(green)
        item.Categories = ', '.join(existing)
        item.Save()


def mark_ingested(message_id):
    """
    Green-tag an email the pipeline has just pulled leads out of.

    Called from pipeline_ingest.py once a message has been processed
    successfully -- never before, so a message that failed part-way through is
    left untagged and can be picked up again.

    Note that green is also the marker this module treats as "already handled,
    skip it" (see _has_green_category), which is what stops an ingested email
    being read a second time.
    """
    apply_green_category(message_id)


def mark_reviewed(message_id):
    """
    Green-tag an email every lead of which has now been approved or rejected.
    Called from dashboard/app.py's approve/reject routes via
    review_marking.maybe_mark_email_reviewed().

    Since ingestion now applies the same green tag, this is usually a no-op by
    the time it runs -- kept because it still closes out any message that was
    ingested while ingest-marking was switched off.
    """
    apply_green_category(message_id)


if __name__ == '__main__':
    try:
        msgs = fetch_new_messages()
        print(f'{len(msgs)} new message(s).')
        for m in msgs[:10]:
            print(f"  {m['received_at']}  {m['sender_email']}  {m['subject']!r}"
                  f"  attachments={m['has_attachments']}")
    except OutlookUnavailableError as e:
        print(f'ERROR: {e}')
