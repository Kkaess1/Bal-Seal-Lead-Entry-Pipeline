"""
Interactive session-capture tool for Oracle Sales Cloud.

Run this whenever pipeline_submit.py reports the Oracle session has expired
(or before the very first unattended run). Opens a real browser, you log in
and navigate to the Leads list yourself -- same as main.py's manual flow --
and this saves the resulting cookies/localStorage to config.json's
oracle.storage_state_path, so pipeline_submit.py can restore that session
later without an interactive step.

How long a captured session survives replay is genuinely unknown until it's
been tried against the real tenant -- see the plan notes on this. Re-run this
script any time pipeline_submit.py's dashboard status page flags the session
as expired.

Usage:  py oracle_login_capture.py
"""

import json
from pathlib import Path

from oracle_automation import OracleLeadAutomator


def main():
    config_path = Path(__file__).parent / 'config.json'
    with open(config_path) as f:
        config = json.load(f)
    oracle_config = dict(config['oracle'])
    # This script's entire purpose is a human logging in interactively --
    # don't let a future config change toward auto-login silently break it.
    oracle_config['manual_login'] = True

    if not oracle_config.get('storage_state_path'):
        print('ERROR: config.json\'s oracle.storage_state_path is not set.')
        return

    print()
    print('=' * 60)
    print('  Oracle Sales Cloud -- Session Capture')
    print('=' * 60)
    print()
    print('  A browser window will open. Log in and navigate to the Leads')
    print("  list exactly as you would for a normal manual import.")
    print()
    print('  Once captured, pipeline_submit.py can reuse this session')
    print("  without asking you to log in again -- until it expires, at")
    print('  which point you run this script again.')
    print()

    with OracleLeadAutomator(oracle_config, username='', password='') as bot:
        bot.login()  # manual flow: human logs in, script waits for Enter, then
                      # verifies the "Create Lead" button is visible
        bot.save_session()

    print()
    print(f'Session saved to {oracle_config["storage_state_path"]}.')
    print('You can close the browser window now.')


if __name__ == '__main__':
    main()
