"""Which leads awaiting review are filed under Pending Review's Spam folder.

Pending Review has two folders, Inbox and Spam. A lead that came in by email
from a Gmail address -- either the address the email was sent from, or the
contact address pulled out of it -- is filed under Spam, where it can still be
reviewed and approved like any other, or thrown out together with the rest of
the folder by Disqualify all (which also deletes the emails from the Sales
inbox).

The folder is a view, not a status. Every lead in it is still
'pending_review' (or one of the other lead_stages.PENDING statuses), so the
lead_stages invariant -- every status on exactly one page, every lead with a
way onward and a way back -- holds without change, and Approve, Reject, Edit,
Reply, Forward and Delete all work on a Spam lead exactly as they do in Inbox.
It also means the folder follows the lead's current details: correcting a
contact address on the review form moves the lead to whichever folder the new
address belongs in, unless the email itself was sent from Gmail.

Only email-sourced leads are filed here. A lead entered by hand or imported
from a spreadsheet was put in front of the reviewer deliberately, and has no
email for Disqualify all to delete.
"""

SPAM_DOMAINS = ('gmail.com', 'googlemail.com')

INBOX = 'inbox'
SPAM = 'spam'
FOLDERS = (INBOX, SPAM)


def is_spam_address(address):
    """Whether an email address is at one of SPAM_DOMAINS."""
    address = (address or '').strip().lower().rstrip('>')
    if '@' not in address:
        return False
    return address.rsplit('@', 1)[1].strip() in SPAM_DOMAINS


def is_spam(lead, sender_email=None):
    """Whether `lead` belongs in the Spam folder.

    `sender_email` is the From address of the lead's source email, which the
    lead row itself does not carry.
    """
    if not lead.get('message_id'):
        return False
    return is_spam_address(lead.get('email')) or is_spam_address(sender_email)


def folder_of(lead, sender_email=None):
    return SPAM if is_spam(lead, sender_email) else INBOX
