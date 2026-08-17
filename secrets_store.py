"""
Thin secret-retrieval wrapper: tries the OS credential store (via `keyring`,
Windows Credential Manager on this machine) first, falls back to environment
variables (.env) for local dev convenience or if keyring is unavailable.

Only meant for actual short secrets (API keys, the Oracle username/password
used for REST basic auth).

Usage to store a secret:  py secrets_store.py OPENAI_API_KEY sk-...
"""

import os

try:
    import keyring
except ImportError:
    keyring = None

SERVICE_NAME = 'LeadPipeline'


def get_secret(name):
    """Try the OS credential store first, then fall back to os.environ."""
    if keyring is not None:
        try:
            value = keyring.get_password(SERVICE_NAME, name)
            if value:
                return value
        except Exception:
            pass  # backend unavailable -- fall through to .env
    return os.getenv(name, '')


def set_secret(name, value):
    if keyring is None:
        raise RuntimeError('keyring package not installed -- py -m pip install keyring')
    keyring.set_password(SERVICE_NAME, name, value)


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 3:
        set_secret(sys.argv[1], sys.argv[2])
        print(f'Stored secret "{sys.argv[1]}" in the OS credential store.')
    else:
        print('Usage: py secrets_store.py <NAME> <value>')
