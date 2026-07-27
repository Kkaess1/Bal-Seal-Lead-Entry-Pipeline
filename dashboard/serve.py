"""Run the local review dashboard with Waitress instead of Flask's dev server."""

import json
from pathlib import Path

from waitress import serve

from dashboard.app import app, db


def main():
    root = Path(__file__).resolve().parent.parent
    db.init_db()
    with open(root / 'config.json', encoding='utf-8') as config_file:
        config = json.load(config_file).get('dashboard', {})
    serve(
        app,
        host=config.get('host', '127.0.0.1'),
        port=int(config.get('port', 5151)),
        threads=int(config.get('threads', 4)),
    )


if __name__ == '__main__':
    main()
