"""Run the local review dashboard with Waitress instead of Flask's dev server.

Started at logon by the LeadPipeline_Dashboard scheduled task (see
setup_scheduler.bat), which runs it under pythonw.exe so there is no console
window sitting in the taskbar all day.

That is also why logging goes to a file here: under pythonw there is no
console, sys.stdout and sys.stderr are None, and anything written to them is
lost -- or raises. A crash at 3am has to leave a trace somewhere.
"""

import json
import ipaddress
import logging
import socket
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Put the project root on sys.path before importing anything from it, so this
# runs the same whether started as `py -m dashboard.serve` from the project
# folder or by absolute path from a scheduled task -- schtasks cannot set a
# working directory, and `-m` needs one.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from waitress import serve  # noqa: E402

from dashboard.app import app, db  # noqa: E402

LOG_DIR = ROOT / 'logs'


def setup_logging():
    """Log to logs/dashboard.log, rotating so an always-on server cannot fill
    the disk. A console handler is added only when there is a console to write
    to -- under pythonw.exe sys.stdout is None and a StreamHandler would blow
    up on the first log line."""
    LOG_DIR.mkdir(exist_ok=True)
    handlers = [
        RotatingFileHandler(
            LOG_DIR / 'dashboard.log', maxBytes=2_000_000, backupCount=5,
            encoding='utf-8',
        )
    ]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-7s  %(name)s  %(message)s',
        handlers=handlers,
        force=True,
    )
    return LOG_DIR / 'dashboard.log'


def already_serving(host, port):
    """
    Whether a dashboard is already answering on this address.

    Checked before binding because Windows will happily let a second server
    bind a port that is already in use (SO_REUSEADDR does not mean here what
    it means on Linux). Both processes then appear healthy while requests go
    to whichever bound last -- which is how three dashboards ended up running
    at once, serving a mix of old and new code, and why a "restart" appeared
    to change nothing.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def is_loopback_host(host):
    """The dashboard has no login and must never bind to a network interface."""
    if (host or '').strip().lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main():
    log_file = setup_logging()
    logger = logging.getLogger(__name__)

    db.init_db()
    with open(ROOT / 'config.json', encoding='utf-8') as config_file:
        config = json.load(config_file).get('dashboard', {})

    host = config.get('host', '127.0.0.1')
    port = int(config.get('port', 5151))

    if not is_loopback_host(host):
        logger.critical(
            'Refusing to expose the unauthenticated dashboard on %s. Set '
            'dashboard.host to 127.0.0.1 or ::1.', host,
        )
        return 2

    if already_serving(host, port):
        logger.error(
            f'A dashboard is already answering on http://{host}:{port}; not '
            'starting a second one. Stop the running dashboard first if you '
            'meant to replace it.'
        )
        return 1

    logger.info(f'Dashboard starting on http://{host}:{port}  (log: {log_file})')
    try:
        serve(app, host=host, port=port, threads=int(config.get('threads', 4)))
    except OSError as e:
        logger.error(f'Could not bind http://{host}:{port}: {e}')
        raise
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
