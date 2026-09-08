"""Where a lead of any given status is shown, and how it may move.

A lead lives in exactly one of three places:

    Pending Review  ->  Input Queue  ->  History
         ^                    |               |
         +--------------------+---------------+
              "Return to Pending Review"

Every status below belongs to exactly one of those pages. That is the whole
point of this module: before it existed each page carried its own hand-written
list of statuses, and two statuses -- 'ingested' and 'extracted', which a lead
holds for the few seconds between being written down and being screened --
appeared on no list at all. A lead whose ingest run died in that window still
existed in the database, still had its email, and was visible nowhere on the
dashboard, with nothing to bring it back.

tests/test_lead_stages.py reads the status literals back out of db.py and
fails if one of them is missing from here, so a new status cannot be added
without deciding which page it appears on.
"""

# --- Pending Review -------------------------------------------------------
# 'ingested' is insert_lead()'s default and 'extracted' is what
# set_extraction_result() writes; a lead passes through both during ingest and
# is left in one of them if the run dies before fraud screening. They belong
# with Pending Review because that is exactly what such a lead still needs.
INGESTING = ('ingested', 'extracted')
PENDING = INGESTING + ('pending_review',)

# --- Input Queue ----------------------------------------------------------
# Staged by a reviewer, waiting for the batch to be confirmed.
STAGED = ('review_approved',)
# Confirmed and on their way to Oracle. 'submitting' is held only while a
# submit run has the lead claimed; 'failed' keeps failed attempts in the
# Input Queue with error signage so the user can fix/retry without going to History.
SENDING = ('approved', 'submitting', 'failed')
QUEUE = STAGED + SENDING

# --- History --------------------------------------------------------------
# Finished, one way or another. Everything here is over unless a human sends
# it back.
HISTORY = ('submitted', 'rejected', 'duplicate', 'submission_unknown')

ALL = PENDING + QUEUE + HISTORY

PAGE_OF = dict(
    [(status, 'Pending Review') for status in PENDING]
    + [(status, 'Input Queue') for status in QUEUE]
    + [(status, 'History') for status in HISTORY]
)

# Statuses a reviewer can send back to Pending Review from History. 'submitted'
# is absent on purpose -- the lead is in Oracle, and returning it here would
# invite a second copy. 'submission_unknown' has its own two answers (confirm
# or retry) because Oracle may or may not hold a record for it.
RETURNABLE = ('rejected', 'duplicate')

# Statuses a reviewer can pull back out of the Input Queue to Pending Review.
# 'approved' and 'failed' allow pulling a lead back to Pending Review for edits.
RECALLABLE = ('approved', 'failed')

# Statuses whose fields a reviewer may still edit.
EDITABLE = PENDING + STAGED


def page_of(status):
    """The page a lead with this status appears on, or None if it has none."""
    return PAGE_OF.get((status or '').strip())


def is_listed(status):
    """Whether a lead with this status is visible anywhere on the dashboard."""
    return page_of(status) is not None
