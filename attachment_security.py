"""Best-effort Microsoft Defender scanning for preserved inbound email files."""

import os
import subprocess
from pathlib import Path


_DEFENDER_PLATFORM = Path(os.environ.get('PROGRAMDATA', r'C:\ProgramData')) / (
    'Microsoft/Windows Defender/Platform'
)


def _defender_command():
    """Return the newest installed MpCmdRun.exe, or None off Windows/Defender."""
    if os.name != 'nt' or not _DEFENDER_PLATFORM.is_dir():
        return None
    commands = sorted(
        _DEFENDER_PLATFORM.glob('*/MpCmdRun.exe'),
        key=lambda path: path.parent.name,
        reverse=True,
    )
    return commands[0] if commands else None


def scan_file(path, timeout=180):
    """Scan one file with Defender without opening or executing it.

    Returns a small status dictionary. A scanner outage is deliberately not
    reported as clean: callers show it as a caution flag to the reviewer.
    """
    candidate = Path(path) if path else None
    if not candidate or not candidate.is_file():
        return {'status': 'unavailable', 'detail': 'Preserved email file is unavailable for scanning.'}
    command = _defender_command()
    if not command:
        return {'status': 'unavailable', 'detail': 'Microsoft Defender command-line scanner is unavailable.'}

    try:
        completed = subprocess.run(
            [str(command), '-Scan', '-ScanType', '3', '-File', str(candidate),
             '-DisableRemediation'],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'status': 'error', 'detail': f'Microsoft Defender scan could not finish: {exc}'}

    output = '\n'.join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    detail = output[-600:] if output else f'Microsoft Defender returned exit code {completed.returncode}.'
    if completed.returncode == 0:
        return {'status': 'clean', 'detail': 'Microsoft Defender found no malware.'}
    if completed.returncode == 2:
        return {'status': 'malicious', 'detail': detail}
    return {'status': 'error', 'detail': detail}
