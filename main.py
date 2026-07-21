"""
Oracle Sales Cloud — Automated Lead Input
Usage:  py main.py
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from excel_parser import parse_excel
from oracle_automation import OracleLeadAutomator


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(log_dir='logs'):
    Path(log_dir).mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = Path(log_dir) / f'run_{stamp}.log'

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-7s  %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def prompt(message, default=None):
    if default:
        response = input(f'{message} [{default}]: ').strip()
        return response if response else default
    else:
        while True:
            response = input(f'{message}: ').strip()
            if response:
                return response
            print('  (required — please enter a value)')


def confirm(message):
    return input(f'{message} [y/n]: ').strip().lower().startswith('y')


def print_header():
    print()
    print('=' * 60)
    print('  Oracle Sales Cloud — Automated Lead Input')
    print('=' * 60)
    print()


def preview_leads(leads, trade_show_name, count=5):
    print(f'\nPreview — first {min(count, len(leads))} of {len(leads)} leads:')
    for lead in leads[:count]:
        org   = (lead.get('organization') or '').upper()
        first = lead.get('first_name') or ''
        last  = lead.get('last_name')  or ''
        rank  = lead.get('rank') or '?'
        print(f'  [{rank:4s}]  {org} - {first} {last} - {trade_show_name}')
    print()


# ---------------------------------------------------------------------------
# Excel file picker
# ---------------------------------------------------------------------------

def pick_excel_file():
    """
    Look for .xlsx files in the 'input' folder next to this script.
    - Zero files  → ask the user to paste a full path
    - One file    → use it automatically
    - Many files  → show a numbered menu to pick from
    """
    input_dir = Path(__file__).parent / 'input'
    files = sorted(input_dir.glob('*.xlsx')) + sorted(input_dir.glob('*.xls'))

    if not files:
        print(f'No Excel files found in the input\\ folder.')
        path = prompt('Paste the full path to your Excel file')
        p = Path(path.strip('"'))
        if not p.exists():
            print(f'ERROR: File not found: {p}')
            sys.exit(1)
        return str(p)

    if len(files) == 1:
        print(f'Excel file: {files[0].name}')
        return str(files[0])

    print('Multiple Excel files found in input\\:')
    for i, f in enumerate(files, 1):
        print(f'  {i}. {f.name}')
    while True:
        choice = input('Select a file number: ').strip()
        if choice.isdigit() and 1 <= int(choice) <= len(files):
            return str(files[int(choice) - 1])
        print('  Please enter a valid number.')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print_header()
    log_file = setup_logging()
    logger   = logging.getLogger(__name__)
    logger.info('Session started.')

    # --- Load config ---
    config_path = Path(__file__).parent / 'config.json'
    if not config_path.exists():
        print('ERROR: config.json not found. Please restore it.')
        sys.exit(1)
    with open(config_path) as f:
        config = json.load(f)
    oracle_config = config['oracle']

    # --- Credentials (only needed if manual_login is False) ---
    load_dotenv(Path(__file__).parent / '.env', override=False)
    if oracle_config.get('manual_login', True):
        username = password = ''
    else:
        username = os.getenv('ORACLE_USERNAME') or prompt('Oracle username')
        password = os.getenv('ORACLE_PASSWORD') or prompt('Oracle password')

    # --- Excel file ---
    excel_path = pick_excel_file()

    # --- Trade show name ---
    trade_show_name = prompt('Trade show name (used in Project Name)', default='Trade Show')

    # --- Row range (optional) ---
    print()
    print('Row range (press Enter to import all):')
    start_row = input('  Start row (1 = first lead): ').strip()
    end_row   = input('  End row   (Enter = last):   ').strip()
    start_idx = max(0, int(start_row) - 1) if start_row.isdigit() else 0
    end_idx   = int(end_row) if end_row.isdigit() else None

    # --- Parse Excel ---
    print('\nReading Excel…')
    try:
        all_leads = parse_excel(excel_path)
    except Exception as e:
        print(f'ERROR reading Excel: {e}')
        sys.exit(1)

    leads = all_leads[start_idx:end_idx]
    if not leads:
        print('No leads found in the selected range.')
        sys.exit(0)

    print(f'\nFound {len(leads)} lead(s) to import.')

    # --- Preview ---
    preview_leads(leads, trade_show_name)

    if not confirm(f'Proceed with importing {len(leads)} lead(s) into Oracle?'):
        print('Aborted.')
        sys.exit(0)

    # --- Run automation ---
    results = {'ok': 0, 'fail': 0, 'errors': []}

    with OracleLeadAutomator(oracle_config, username, password) as bot:
        try:
            bot.login()
            bot.navigate_to_leads()
        except Exception as e:
            logger.error(f'Failed during login/navigation: {e}')
            bot.screenshot('login_failure.png')
            print('\nERROR: Could not log in or navigate to Leads. See logs for details.')
            sys.exit(1)

        for i, lead in enumerate(leads, 1):
            row = lead.get('row_number', '?')
            org   = (lead.get('organization') or '?').upper()
            first = lead.get('first_name') or ''
            last  = lead.get('last_name')  or ''
            label = f'{org} - {first} {last}'

            print(f'[{i:>3}/{len(leads)}]  {label}', end='  ', flush=True)

            try:
                project_name = bot.create_lead(lead, trade_show_name)
                print('filled')
                logger.info(f'  FILLED row {row}: {project_name}')
                results['ok'] += 1
                print()
                print('  Form is ready. Review the fields in the browser,')
                print('  select Market, then click Save and Close.')
                print()
                input('  Once you are back on the Leads list, press Enter to continue...')
                print()
            except Exception as e:
                print('FAILED (form could not be opened/filled)')
                logger.error(f'  FAILED row {row} ({label}): {e}')
                bot.screenshot(f'fail_row_{row}.png')
                results['fail'] += 1
                results['errors'].append({'row': row, 'lead': label, 'error': str(e)})
                input('  Press Enter to try the next lead...')
                print()

    # --- Summary ---
    print()
    print('=' * 60)
    print(f'  Done.  {results["ok"]} saved   {results["fail"]} failed')
    print(f'  Log: {log_file}')
    print('=' * 60)

    if results['errors']:
        print('\nFailed leads:')
        for err in results['errors']:
            print(f'  Row {err["row"]}  {err["lead"]}')
            print(f'    → {err["error"]}')

    logger.info(
        f'Session complete. Saved: {results["ok"]}  Failed: {results["fail"]}'
    )


if __name__ == '__main__':
    main()
