"""
Scheduled entry point: submit approved leads to Oracle Sales Cloud.

Two modes, controlled by config.json's oracle.auto_submit:
  - false -- "fill but don't submit" validation mode. Fills each approved
    lead's form via create_lead() and asks a human at the terminal to review
    it in the browser, click Save and Close themselves, and confirm
    success/failure back here. THIS MODE MUST BE RUN BY HAND, WATCHED -- do
    not put it on Task Scheduler, it will hang waiting on input() with no one
    there to answer.
  - true (current setting, as of 2026-07-21) -- full unattended submission via
    submit_lead() (fill + click Save and Close + verify), no human
    interaction. This is what's now on Task Scheduler (see the "Lead Pipeline
    - Submit" task). Enabled after fill-only mode was exercised, Oracle session
    persistence was confirmed working against the real tenant, and one real
    lead (Interlune) went through this exact path successfully -- see the plan
    notes for what was found and fixed along the way (an Owner-field bug) and
    what's still being actively monitored (Oracle session lifetime under
    normal use, real risk of a field silently not populating correctly even
    though the lead still saves and reports success).

Either way: only ever acts on leads with status='approved' (set via the
dashboard's Approve button).

Login (as of 2026-07-20): tries the restored storage_state session first
(fast); if that's actually expired, falls back to a real username/password
auto-login via oracle_automation.OracleLeadAutomator.ensure_logged_in() --
this tenant has no MFA (confirmed by hand previously), and Keaton explicitly
asked for this so the whole pipeline doesn't stall waiting on a manual
oracle_login_capture.py recapture every time the ~30-60 min session expires
between the twice-daily runs. Credentials come from secrets_store.py
(ORACLE_USERNAME/ORACLE_PASSWORD in the OS credential store), never
hardcoded. Exactly one login attempt per run -- if that fails (e.g. bad/
rotated credentials), the run aborts the same as before; approved leads are
left untouched and retried next run.

Usage:  py pipeline_submit.py
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import db
import secrets_store
from oracle_automation import OracleLeadAutomator

logger = logging.getLogger(__name__)
LOG_DIR = Path(__file__).parent / 'logs'


def setup_logging():
    """Same pattern as pipeline_ingest.py/main.py -- a scheduled Task Scheduler
    run has no console anyone is watching, so this is the only record of what
    an unattended submit run actually did (which leads, any failures)."""
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = LOG_DIR / f'submit_{stamp}.log'
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-7s  %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file

# Leads from a real trade-show batch (an Excel attachment on an email) get the
# same "Tradeshow" labeling main.py already uses; leads extracted from a direct
# email inquiry are not trade-show contacts and get labeled accordingly instead.
_SOURCE_LABELS = {
    'email_excel_attachment': ('Tradeshow', 'Email Import'),
    'manual_excel': ('Tradeshow', 'Email Import'),
    'email_ai_extract': ('Inquiry', 'Website/Email'),
}
_DEFAULT_SOURCE_LABEL = ('Inquiry', 'Website/Email')


def _project_label(lead):
    return _SOURCE_LABELS.get(lead.get('source'), _DEFAULT_SOURCE_LABEL)


def _load_oracle_config():
    with open(Path(__file__).parent / 'config.json') as f:
        config = json.load(f)
    oracle_config = dict(config['oracle'])
    # Every lead this pipeline submits comes from the Sales inbox (website
    # inquiries/form submissions), never a physical trade show -- forced here
    # rather than in config.json itself, since main.py's separate manual
    # Excel-import flow still legitimately uses config.json's lead_source
    # value for real trade-show batches.
    oracle_config['lead_source'] = 'Website'
    # Respect config.json's headless setting (defaults to False if not set)
    oracle_config['headless'] = config.get('oracle', {}).get('headless', False)
    # This run never waits on a human -- ensure_logged_in() drives
    # _auto_login() directly when needed, bypassing the manual_login branch
    # in login()/navigate_to_leads() entirely. Set for clarity/consistency
    # even though ensure_logged_in() doesn't itself branch on this flag.
    oracle_config['manual_login'] = False
    return oracle_config


def _run_fill_only(bot, leads):
    """Phase-3-style validation mode: fill each lead, a human reviews and
    clicks Save and Close themselves, then confirms the result here so the
    dashboard's history stays accurate even though the pipeline didn't click
    the final button itself."""
    print()
    print('=' * 60)
    print('  FILL-ONLY MODE (oracle.auto_submit = false in config.json)')
    print('  Each lead will be filled in. Review it, click Save and Close')
    print('  yourself, then confirm the result here.')
    print('=' * 60)

    results = {'ok': 0, 'fail': 0}
    for lead in leads:
        source_label, trade_show_name = _project_label(lead)
        label = f"{lead.get('organization') or '?'} - {lead.get('first_name') or ''} {lead.get('last_name') or ''}"
        print(f'\n[{label}]')
        try:
            project_name = bot.create_lead(lead, trade_show_name, source_label=source_label)
            print(f'  Filled: {project_name}')
        except Exception as e:
            logger.error(f'  Failed to fill lead {lead["id"]}: {e}')
            bot.screenshot(f'submit_fill_fail_{lead["id"]}.png')
            db.mark_lead_failed(lead['id'], f'Fill failed: {e}')
            results['fail'] += 1
            continue

        answer = input('  Saved successfully in the browser? [y/n]: ').strip().lower()
        if answer.startswith('y'):
            db.mark_lead_submitted(lead['id'], project_name)
            results['ok'] += 1
        else:
            reason = input('  What went wrong? (optional): ').strip()
            db.mark_lead_failed(lead['id'], reason or 'Human reported failure in fill-only mode')
            results['fail'] += 1
    return results


def _run_auto_submit(bot, leads):
    """Full unattended submission -- no human interaction. Only reached when
    config.oracle.auto_submit is true."""
    results = {'ok': 0, 'fail': 0}
    for lead in leads:
        source_label, trade_show_name = _project_label(lead)
        label = f"{lead.get('organization') or '?'} - {lead.get('first_name') or ''} {lead.get('last_name') or ''}"
        try:
            success, project_name, error_detail = bot.submit_lead(
                lead, trade_show_name, source_label=source_label
            )
            if success:
                db.mark_lead_submitted(lead['id'], project_name)
                logger.info(f'  Submitted: {label} ({project_name})')
                results['ok'] += 1
            else:
                screenshot_name = f'submit_fail_{lead["id"]}.png'
                db.mark_lead_failed(lead['id'], error_detail, submit_screenshot_path=f'logs/{screenshot_name}')
                logger.error(f'  Failed: {label} -- {error_detail}')
                results['fail'] += 1
        except Exception as e:
            db.mark_lead_failed(lead['id'], str(e))
            logger.error(f'  Exception submitting {label}: {e}')
            results['fail'] += 1
    return results


def main():
    log_file = setup_logging()
    logger.info(f'Log file: {log_file}')
    load_dotenv(Path(__file__).parent / '.env', override=False)
    db.init_db()

    oracle_config = _load_oracle_config()
    auto_submit = oracle_config.get('auto_submit', False)

    leads = db.list_leads(status='approved')
    if not leads:
        logger.info('No approved leads waiting for submission.')
        return

    logger.info(f'{len(leads)} approved lead(s) to submit. auto_submit={auto_submit}')

    username = secrets_store.get_secret('ORACLE_USERNAME')
    password = secrets_store.get_secret('ORACLE_PASSWORD')

    with OracleLeadAutomator(oracle_config, username=username, password=password) as bot:
        if not bot.ensure_logged_in():
            logger.error(
                'Could not establish an Oracle session -- restored session was invalid '
                'and auto-login failed (check ORACLE_USERNAME/ORACLE_PASSWORD in the OS '
                'credential store, or run py oracle_login_capture.py manually).'
            )
            db.set_run_state('oracle_session_status', 'expired')
            sys.exit(1)
        db.set_run_state('oracle_session_status', 'valid')

        if auto_submit:
            results = _run_auto_submit(bot, leads)
        else:
            results = _run_fill_only(bot, leads)

    db.set_run_state('last_submit_run_at', datetime.now(timezone.utc).isoformat())
    logger.info(f"Submit run complete. {results['ok']} ok, {results['fail']} failed.")


if __name__ == '__main__':
    main()
