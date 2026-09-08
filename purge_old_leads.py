"""
Entry point: delete finished leads that are older than the retention policy.

    py purge_old_leads.py              # dry run -- says what would go, deletes nothing
    py purge_old_leads.py --apply      # actually delete
    py purge_old_leads.py --apply --rejected-days 60 --finished-days 730

`retention.py` documents the policy and why it only ever touches finished
leads. In short: a rejected lead goes 30 days after it was rejected, anything
else that has finished goes a year after it finished, and a lead still in
Pending Review or the Input Queue is never purged however old it is.

Deletion is permanent and writes no backup file. What survives is the counts:
every purged lead's contribution to the status page is added to
`purged_lead_stats` in the same transaction that deletes it, so the totals,
rates and funnel there keep counting leads that no longer exist.

Run daily by the LeadPipeline_Retention scheduled task (setup_retention_task.ps1),
which passes --apply. Like the other entry points it logs to logs/, because an
unattended run has no console anyone is watching and this is the only record of
what it deleted.
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import db
import retention

logger = logging.getLogger(__name__)
LOG_DIR = Path(__file__).parent / 'logs'


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = LOG_DIR / f'purge_{stamp}.log'
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-7s  %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[1])
    parser.add_argument(
        '--apply', action='store_true',
        help='Actually delete. Without it this is a dry run and nothing changes.')
    parser.add_argument(
        '--rejected-days', type=int, default=retention.REJECTED_RETENTION_DAYS,
        help=f'Keep rejected leads this many days (default {retention.REJECTED_RETENTION_DAYS}).')
    parser.add_argument(
        '--finished-days', type=int, default=retention.FINISHED_RETENTION_DAYS,
        help=f'Keep every other finished lead this many days '
             f'(default {retention.FINISHED_RETENTION_DAYS}).')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    log_file = setup_logging()
    logger.info(f'Log file: {log_file}')
    db.init_db()

    logger.info(
        f'Retention policy: rejected leads kept {args.rejected_days} days, '
        f'other finished leads kept {args.finished_days} days. '
        f"Leads awaiting review are never purged."
    )
    result = retention.purge(
        rejected_days=args.rejected_days,
        finished_days=args.finished_days,
        apply=args.apply,
    )
    # Which rule caught them, not just how many: "365 rejected" reads very
    # differently from "365 aged out", and the log is the only place anyone
    # will ever see it.
    for rule, count in sorted(result['by_rule'].items()):
        label = ('past the rejected-lead window' if rule == 'rejected'
                 else 'past the one-year window')
        logger.info(f'{count} lead(s) {label}.')
    logger.info(retention.describe(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
