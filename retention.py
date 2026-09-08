"""
How long a finished lead is kept, and what deleting one actually removes.

The database held every lead the pipeline had ever seen. That is fine for a
year and a problem after several: the rows themselves are small, but each one
anchors a cached email body, a saved .msg file and any attachments the customer
sent, and all of it is real personal data sitting on one Windows machine. There
is no reason to keep a lead that was rejected as junk in 2026 for the rest of
the pipeline's life.

Two rules:

    rejected                    deleted 30 days after it was rejected
    any other finished lead     deleted 365 days after it finished

**Only finished leads are ever purged** -- the lead_stages.HISTORY statuses.
A lead still in Pending Review or the Input Queue is work someone has not done
yet, however old it is, and lead_stages.py exists to guarantee such a lead
always has a way onward and a way back. Deleting it would be the most complete
dead end of all. An ancient lead sitting in the queue is a prompt to deal with
it, not something to quietly bin.

Age is measured from when the lead *finished*, not from when its email arrived
-- see db.FINISHED_AT_SQL. A lead reviewed last week is a week old even if it
came from a year-old email, and purging on email date would delete leads the
operator had only just dealt with.

**Nothing personal survives a purge, and the numbers do.** Before any row is
deleted, its contribution to every count on the status page is added to
`purged_lead_stats` -- lead counts by status, the Oracle funnel, opportunity
and order money by currency. That table holds counts and money only, no names.
The status page then reads live + purged, so "All leads", the conversion rates
and the funnel do not drop the morning this first runs. What cannot survive is
the per-lead "Find a submitted lead" table: you cannot search a lead that has
been deleted, and pretending otherwise would mean not deleting it.

Purging is not reversible and writes no backup. That is the point of it.

Run by hand with `py purge_old_leads.py` (dry run) / `--apply`, and daily by
the LeadPipeline_Retention scheduled task -- see DEPLOYMENT.md.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent

# A rejected lead is junk, a scam, or an inquiry that was never ours. A month
# is long enough to notice a rejection was wrong and use "Return to Review".
REJECTED_RETENTION_DAYS = 30

# Everything else that has finished -- submitted, duplicate, submission_unknown.
# A submitted lead's real record lives in Oracle and is not affected by this;
# what goes is our local copy of it.
FINISHED_RETENTION_DAYS = 365

# Directories a purged lead's files may live in. A path outside all of them is
# left alone: this deletes files, and it should only ever be able to delete the
# pipeline's own cached copies of customer email.
DELETABLE_DIRS = ('email_cache', 'email_attachments', 'email_messages')


def cutoffs(now=None, rejected_days=None, finished_days=None):
    """The two 'older than this' timestamps, as ISO strings to compare against
    stored ones. Everything in the database is UTC ISO-8601, so string
    comparison is chronological -- the same reasoning db.list_leads() uses."""
    now = now or datetime.now(timezone.utc)
    rejected_days = REJECTED_RETENTION_DAYS if rejected_days is None else rejected_days
    finished_days = FINISHED_RETENTION_DAYS if finished_days is None else finished_days
    return (
        (now - timedelta(days=rejected_days)).isoformat(),
        (now - timedelta(days=finished_days)).isoformat(),
    )


def due_for_purge(now=None, rejected_days=None, finished_days=None, conn=None):
    """Every lead the policy says is deletable right now, oldest first."""
    rejected_before, finished_before = cutoffs(now, rejected_days, finished_days)
    return db.leads_due_for_purge(rejected_before, finished_before, conn=conn)


def _is_deletable(path):
    """Whether a stored path is one of ours to delete.

    Paths come out of the database and some are relative, some absolute, some
    were written by an older version of the pipeline. Resolving each one and
    requiring it to sit under a known cache directory means a bad or hand-edited
    value cannot turn the retention job into a delete-anything tool.
    """
    try:
        resolved = Path(path).expanduser()
        resolved = (resolved if resolved.is_absolute() else ROOT / resolved).resolve()
    except (OSError, ValueError):
        return False
    for name in DELETABLE_DIRS:
        directory = (ROOT / name).resolve()
        if resolved == directory or directory in resolved.parents:
            return True
    return False


def delete_files(paths):
    """Remove cached email files belonging to purged leads. Returns how many
    were deleted; a file already gone is not an error, and one that will not
    delete (open in another program, say) is logged and skipped rather than
    failing the run -- the database rows are the part that must be consistent."""
    deleted = 0
    for path in paths:
        if not _is_deletable(path):
            logger.debug(f'Not deleting {path}: outside the cache directories.')
            continue
        try:
            file_path = Path(path)
            if not file_path.is_absolute():
                file_path = ROOT / file_path
            file_path.unlink()
            deleted += 1
        except FileNotFoundError:
            pass
        except OSError as error:
            logger.warning(f'Could not delete {path}: {error}')
    return deleted


def purge(now=None, rejected_days=None, finished_days=None, apply=False, conn=None):
    """
    Apply the retention policy.

    With apply=False (the default) nothing is touched and the return value says
    what a real run would delete. The dry run is the default everywhere,
    including in the scheduled task's own logs, for the same reason
    scratch_retire_leads.py works that way: a delete you cannot undo should be
    something you asked for twice.

    Files are deleted after the transaction commits, never before. If it is
    going to go wrong, the harmless order is rows-gone-files-left (a stray file
    in email_cache/ that nothing references) rather than rows-left-files-gone
    (a lead on the dashboard whose email body will not open).
    """
    leads = due_for_purge(now, rejected_days, finished_days, conn=conn)
    lead_ids = [lead['id'] for lead in leads]
    result = {
        'due': len(lead_ids),
        'applied': bool(apply),
        'by_status': {},
        'by_rule': {},
        'leads': 0,
        'messages': 0,
        'files': 0,
    }
    for lead in leads:
        result['by_status'][lead['status']] = result['by_status'].get(lead['status'], 0) + 1
        result['by_rule'][lead['rule']] = result['by_rule'].get(lead['rule'], 0) + 1
    if not lead_ids or not apply:
        return result

    paths = db.purge_lead_file_paths(lead_ids, conn=conn)
    removed = db.purge_leads(lead_ids, conn=conn)
    result.update(leads=removed['leads'], messages=removed['messages'])
    result['files'] = delete_files(paths)
    return result


def describe(result):
    """One line for the log and the console, whichever way this was run."""
    if not result['due']:
        return 'Nothing is old enough to purge.'
    statuses = ', '.join(
        f'{count} {status}' for status, count in sorted(result['by_status'].items()))
    if not result['applied']:
        return (f"{result['due']} lead(s) are due for deletion ({statuses}). "
                'Nothing was deleted -- re-run with --apply.')
    return (f"Deleted {result['leads']} lead(s) ({statuses}), "
            f"{result['messages']} orphaned message(s) and {result['files']} cached file(s).")
