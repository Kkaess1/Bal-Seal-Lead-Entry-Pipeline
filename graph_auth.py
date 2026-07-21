"""
Microsoft Graph authentication via MSAL device-code flow.

One-time interactive setup: run `py graph_auth.py --login` once. It opens a
device-code prompt (visit a URL, enter a short code, sign in as yourself).
After that, every call to get_access_token() silently refreshes from the
cached token -- no interactive step needed for the twice-daily scheduled runs.

Needs config.json's "graph" block filled in (client_id, tenant_id) -- these
come from whichever Entra ID admin creates the Azure AD app registration (see
project notes for the exact request to send IT). This is a *public client*
app -- no client secret is stored or needed, since it authenticates as
Keaton via delegated permissions, not as an app with its own identity.
"""

import json
import sys
from pathlib import Path

import msal

AUTH_DIR = Path(__file__).parent / 'auth'
TOKEN_CACHE_PATH = AUTH_DIR / 'msal_token_cache.bin'
SCOPES = ['Mail.Read']


def _load_graph_config():
    config_path = Path(__file__).parent / 'config.json'
    with open(config_path) as f:
        config = json.load(f)
    graph_config = config.get('graph') or {}
    client_id = graph_config.get('client_id')
    tenant_id = graph_config.get('tenant_id')
    if not client_id or not tenant_id:
        raise RuntimeError(
            "config.json's graph.client_id / graph.tenant_id are blank -- fill these "
            "in with the values from the Azure AD app registration before running "
            "graph_auth.py. See project notes for what to ask IT for."
        )
    return client_id, tenant_id


def _build_app():
    client_id, tenant_id = _load_graph_config()
    cache = msal.SerializableTokenCache()
    AUTH_DIR.mkdir(exist_ok=True)
    if TOKEN_CACHE_PATH.exists():
        cache.deserialize(TOKEN_CACHE_PATH.read_text(encoding='utf-8'))

    app = msal.PublicClientApplication(
        client_id,
        authority=f'https://login.microsoftonline.com/{tenant_id}',
        token_cache=cache,
    )
    return app, cache


def _save_cache(cache):
    if cache.has_state_changed:
        AUTH_DIR.mkdir(exist_ok=True)
        TOKEN_CACHE_PATH.write_text(cache.serialize(), encoding='utf-8')


def interactive_login():
    """Run once, interactively, to populate the token cache via device code flow."""
    app, cache = _build_app()
    flow = app.initiate_device_flow(scopes=SCOPES)
    if 'user_code' not in flow:
        raise RuntimeError(f'Failed to start device flow: {flow}')

    print(flow['message'])  # "To sign in, open https://microsoft.com/devicelogin and enter code XXXXX"
    result = app.acquire_token_by_device_flow(flow)  # blocks until the user completes sign-in
    _save_cache(cache)

    if 'access_token' not in result:
        raise RuntimeError(f'Login failed: {result.get("error_description", result)}')
    print('Login successful -- token cached for future runs.')
    return result['access_token']


def get_access_token():
    """
    Silently acquire a token from the cache, refreshing if needed.

    Returns None if there's no cached account to refresh from. Callers should
    treat that as "run `py graph_auth.py --login` first", not retry in a loop
    -- an unattended pipeline run with no valid token should abort and flag
    it, the same way pipeline_submit.py treats an expired Oracle session.
    """
    app, cache = _build_app()
    accounts = app.get_accounts()
    if not accounts:
        return None
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    _save_cache(cache)
    if not result or 'access_token' not in result:
        return None
    return result['access_token']


if __name__ == '__main__':
    try:
        if '--login' in sys.argv:
            interactive_login()
        else:
            token = get_access_token()
            if token:
                print('Cached token is valid -- access token acquired (not printed).')
            else:
                print('No valid cached token. Run: py graph_auth.py --login')
    except RuntimeError as e:
        print(f'ERROR: {e}')
        sys.exit(1)
