"""
Entry point: move the pipeline's data -- every lead, its history and the
cached emails -- from one computer to another.

    py transfer_data.py export                        # to a zip on your Desktop
    py transfer_data.py export "D:\\lead-data.zip"
    py transfer_data.py import "D:\\lead-data.zip"     # on the other computer
    py transfer_data.py import "D:\\lead-data.zip" --replace

Why this exists rather than "copy the folder". GitHub carries the code only:
pipeline.db and the email folders hold customers' names, addresses and whole
emails, and .gitignore keeps them out on purpose. And a plain copy does not
work anyway, for two reasons:

  * The database records every cached email by its full path --
    C:\\Users\\<you>\\...\\email_cache\\... -- so on a computer with another
    username, every email body, thread and Oracle attachment points at a
    folder that does not exist. import rewrites those paths to where the
    project lives on the new computer.

  * pipeline.db runs in WAL mode while the dashboard is up, and copying the
    file then can capture half a transaction. export takes its snapshot with
    SQLite's own backup API, which is consistent even with the dashboard
    running.

The zip is the whole customer dataset. Move it by company OneDrive/SharePoint
or a USB drive -- never email or GitHub -- and delete it once imported. It is
refused inside the project folder so a `git add -A` cannot publish it.

Two computers should not both run the pipeline afterwards: each has its own
database, and an email one ingests is tagged green so the other skips it --
the leads end up split between two dashboards. Export from one, import on the
other, and stop using the first.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import db

ROOT = Path(__file__).resolve().parent
DB_NAME = 'pipeline.db'
MANIFEST = 'manifest.json'
FORMAT_VERSION = 1

# Everything outside the database that a lead refers to or the dashboard
# shows. reference/README.md is tracked in git, so a fresh clone already has it.
DATA_DIRS = ('email_cache', 'email_messages', 'email_attachments', 'reference')
_SKIP = {'reference/README.md'}

# Folders whose files the app only ever finds through a path stored in the
# database, so a file there can be renamed as long as that path is updated.
# Not reference/: those names are what the Reference tab shows.
_RENAMEABLE_DIRS = ('email_cache', 'email_messages', 'email_attachments')

# Emails cached before 2026-08 were named after Outlook's ~150-character entry
# ID. Under a project folder a little deeper than the original that passes
# Windows' 260-character path limit, and the file cannot even be written.
# Anything this long is given the short hashed name ingest uses today.
_MAX_NAME = 64

# Past this, a plain Windows path cannot be opened unless long paths are
# switched on for the machine -- which neither this tool nor the app assumes.
_MAX_PATH = 250

# run_state entries describing a process on the *old* computer. A lease left
# "running" there would, on the new one, look like an ingest already underway
# -- and its pid may even belong to some unrelated process.
_LIVE_LEASE_STATUSES = {'starting', 'running'}


class TransferError(Exception):
    """A problem the operator can fix; the message is shown as-is."""


def _default_export_path():
    stamp = datetime.now().strftime('%Y%m%d-%H%M')
    desktop = Path.home() / 'Desktop'
    return (desktop if desktop.is_dir() else Path.home()) / f'lead-pipeline-data-{stamp}.zip'


def _is_inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _snapshot(db_path, target):
    """A consistent copy of a live WAL database, via SQLite's backup API.
    Opened normally, not read-only: SQLite can refuse a read-only open of a
    WAL database whose -shm file is missing, and the backup only reads."""
    source = sqlite3.connect(db_path)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()


def _counts(db_path):
    conn = sqlite3.connect(f'file:{Path(db_path).as_posix()}?mode=ro', uri=True)
    try:
        return {table: conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                for table in ('leads', 'messages')}
    finally:
        conn.close()


def export_data(out_path=None, root=ROOT, log=print):
    """
    Write the database snapshot and the data folders to one zip.

    Returns the manifest written into it. The zip is assembled under a
    temporary name and renamed at the end, so an export that fails partway
    never leaves something that looks complete.
    """
    root = Path(root)
    out_path = Path(out_path) if out_path else _default_export_path()
    if out_path.suffix.lower() != '.zip':
        out_path = out_path.with_suffix('.zip')
    if _is_inside(out_path, root):
        raise TransferError(
            'Save the export outside the project folder (e.g. your Desktop): it '
            'holds every customer record, and inside the repo one "git add -A" '
            'would publish it.'
        )
    db_path = root / DB_NAME
    if not db_path.exists():
        raise TransferError(f'There is no {DB_NAME} in {root} to export.')
    out_path.parent.mkdir(parents=True, exist_ok=True)

    partial = out_path.with_name(out_path.name + '.part')
    files = {}
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / DB_NAME
        log('Taking a consistent snapshot of the database...')
        _snapshot(db_path, snapshot)
        manifest = {
            'format': FORMAT_VERSION,
            'source_root': str(root),
            'exported_at': datetime.now(timezone.utc).isoformat(),
            'counts': _counts(snapshot),
        }
        try:
            with zipfile.ZipFile(partial, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                archive.write(snapshot, DB_NAME)
                for folder in DATA_DIRS:
                    base = root / folder
                    count = 0
                    if base.is_dir():
                        log(f'Adding {folder}\\ ...')
                        for path in sorted(base.rglob('*')):
                            name = path.relative_to(root).as_posix()
                            if path.is_file() and name not in _SKIP:
                                archive.write(path, name)
                                count += 1
                    files[folder] = count
                manifest['files'] = files
                archive.writestr(MANIFEST, json.dumps(manifest, indent=2))
            os.replace(partial, out_path)
        finally:
            if partial.exists():
                partial.unlink()
    manifest['path'] = str(out_path)
    return manifest


def _safe_member(name):
    """
    Whether a zip entry may be extracted: the database, the manifest, or a
    file inside one of the data folders -- never an absolute path or one
    climbing out with "..". A tampered zip must not be able to overwrite the
    code it is being imported beside.
    """
    if name in (DB_NAME, MANIFEST):
        return True
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or ':' in name or '\\' in name:
        return False
    return len(path.parts) >= 2 and path.parts[0] in DATA_DIRS


def _has_leads(db_path):
    if not Path(db_path).exists():
        return False
    try:
        return any(_counts(db_path).values())
    except sqlite3.Error:
        return True     # unreadable: treat as something worth not overwriting


def _remove_database(db_path):
    for suffix in ('', '-wal', '-shm'):
        path = Path(f'{db_path}{suffix}')
        if path.exists():
            try:
                path.unlink()
            except PermissionError as exc:
                raise TransferError(
                    'pipeline.db is in use. Stop the dashboard first:\n'
                    '  Stop-ScheduledTask -TaskName LeadPipeline_Dashboard'
                ) from exc


def _short_name(name):
    """Where a file lands on import: its own name, or -- for an over-long one
    in a rename-able folder -- a hash of it, as pipeline_ingest names caches."""
    path = PurePosixPath(name)
    if path.parts[0] not in _RENAMEABLE_DIRS or len(path.name) <= _MAX_NAME:
        return name
    short = hashlib.sha256(path.name.encode('utf-8')).hexdigest()[:24] + path.suffix
    return str(path.with_name(short))


def _rewrite_paths(conn, old_root, new_root, renames=None):
    """
    Point every stored file path under the old project folder at the new one.

    Every column named *_path, in every table, so a path column added later
    is covered without touching this. Compared case-insensitively and with
    either slash, as Windows does. `renames` maps a file's old relative path
    (posix, casefolded) to the one it was written under. Returns
    (rewritten, left_alone).
    """
    renames = renames or {}
    old = old_root.rstrip('\\/')
    old_key = old.replace('/', '\\').casefold()
    new = str(new_root).rstrip('\\/')
    rewritten = left = 0
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")]
    for table in tables:
        columns = [r[1] for r in conn.execute(f'PRAGMA table_info({table})')
                   if r[1].endswith('_path')]
        for column in columns:
            rows = conn.execute(
                f"SELECT rowid, {column} FROM {table} "
                f"WHERE {column} IS NOT NULL AND {column} != ''").fetchall()
            for rowid, value in rows:
                key = value.replace('/', '\\').casefold()
                if key.startswith(old_key + '\\'):
                    rest = value[len(old) + 1:].replace('\\', '/')
                    rest = renames.get(rest.casefold(), rest).replace('/', '\\')
                    conn.execute(f'UPDATE {table} SET {column} = ? WHERE rowid = ?',
                                 (f'{new}\\{rest}', rowid))
                    rewritten += 1
                else:
                    left += 1
    return rewritten, left


def _clear_live_leases(conn):
    cleared = 0
    for key, value in conn.execute(
            "SELECT key, value FROM run_state WHERE key LIKE 'job:%'").fetchall():
        try:
            status = (json.loads(value or '{}') or {}).get('status')
        except ValueError:
            status = None
        if status in _LIVE_LEASE_STATUSES:
            conn.execute('DELETE FROM run_state WHERE key = ?', (key,))
            cleared += 1
    return cleared


def _missing_files(conn):
    """How many stored paths do not exist on this computer after import."""
    missing = total = 0
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")]
    for table in tables:
        for column in [r[1] for r in conn.execute(f'PRAGMA table_info({table})')
                       if r[1].endswith('_path')]:
            for (value,) in conn.execute(
                    f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != ''"):
                total += 1
                if not Path(value).exists():
                    missing += 1
    return missing, total


def import_data(zip_path, root=ROOT, replace=False, log=print):
    """
    Unpack an export into this project folder and point it at its new home.

    Refuses to overwrite a database that already has leads unless `replace`;
    even then the old one is kept as pipeline.before-import-<time>.db
    (gitignored, like every *.db). Returns a summary dict.
    """
    root = Path(root)
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise TransferError(f'{zip_path} does not exist.')
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise TransferError(f'{zip_path.name} is not a pipeline export (not a zip).') from exc

    with archive:
        names = archive.namelist()
        if MANIFEST not in names or DB_NAME not in names:
            raise TransferError(f'{zip_path.name} is not a pipeline export.')
        manifest = json.loads(archive.read(MANIFEST))
        if manifest.get('format') != FORMAT_VERSION:
            raise TransferError(
                f'{zip_path.name} was written by a different version of this tool '
                f'(format {manifest.get("format")}); pull the latest code on both computers.'
            )
        unsafe = [n for n in names if not n.endswith('/') and not _safe_member(n)]
        if unsafe:
            raise TransferError(
                f'{zip_path.name} contains files outside the data folders '
                f'({unsafe[0]!r}); refusing to import it.'
            )

        renames, plan = {}, []
        for name in names:
            if name.endswith('/') or name == MANIFEST:
                continue
            landed = _short_name(name) if name != DB_NAME else name
            if landed != name:
                renames[name.casefold()] = landed
            target = root / PurePosixPath(landed)
            if not _is_inside(target, root):
                raise TransferError(f'Refusing to write outside the project folder: {name!r}')
            plan.append((name, target))
        longest = max((len(str(target)) for _, target in plan), default=0)
        if longest > _MAX_PATH:
            raise TransferError(
                f'The project folder is too deep for Windows: some files would have '
                f'{longest}-character paths (the limit is about 260). Move the project '
                f'to a shorter folder, such as C:\\LeadPipeline, and import again.'
            )

        db_path = root / DB_NAME
        backup = None
        if _has_leads(db_path):
            if not replace:
                raise TransferError(
                    f'This computer already has leads in {DB_NAME}. Importing would '
                    'replace them. Run again with --replace to do that (the current '
                    'database is kept as a backup file).'
                )
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            backup = root / f'pipeline.before-import-{stamp}.db'
            log(f'Keeping the current database as {backup.name}')
            _snapshot(db_path, backup)
        _remove_database(db_path)

        log('Unpacking...')
        for name, target in plan:
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, open(target, 'wb') as sink:
                shutil.copyfileobj(source, sink)

    conn = sqlite3.connect(db_path)
    try:
        rewritten, left = _rewrite_paths(conn, manifest['source_root'], root, renames)
        cleared = _clear_live_leases(conn)
        conn.commit()
    finally:
        conn.close()

    # Brings an export from an older checkout up to this code's schema.
    db.init_db(db_path)

    conn = sqlite3.connect(db_path)
    try:
        missing, total = _missing_files(conn)
    finally:
        conn.close()

    return {
        'counts': _counts(db_path),
        'exported_at': manifest.get('exported_at'),
        'source_root': manifest.get('source_root'),
        'paths_rewritten': rewritten,
        'files_renamed': len(renames),
        'paths_left_alone': left,
        'files_missing': missing,
        'paths_total': total,
        'leases_cleared': cleared,
        'backup': str(backup) if backup else None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Move the lead pipeline data between computers.')
    sub = parser.add_subparsers(dest='command', required=True)
    exp = sub.add_parser('export', help='write a zip of the data (default: your Desktop)')
    exp.add_argument('zip', nargs='?', help='where to write it')
    imp = sub.add_parser('import', help='load a zip written by export')
    imp.add_argument('zip', help='the export to load')
    imp.add_argument('--replace', action='store_true',
                     help='replace a database that already has leads (a backup is kept)')
    args = parser.parse_args(argv)

    try:
        if args.command == 'export':
            manifest = export_data(args.zip)
            counts = manifest['counts']
            print(f'\nExported {counts["leads"]} leads and {counts["messages"]} emails to:\n'
                  f'  {manifest["path"]}\n'
                  'This file holds every customer record. Move it by company OneDrive '
                  'or USB drive, never email or GitHub, and delete it once imported.')
        else:
            summary = import_data(args.zip, replace=args.replace)
            counts = summary['counts']
            print(f'\nImported {counts["leads"]} leads and {counts["messages"]} emails '
                  f'(exported {summary["exported_at"]}).')
            print(f'File paths moved to this folder: {summary["paths_rewritten"]}.')
            if summary['files_missing']:
                print(f'WARNING: {summary["files_missing"]} of {summary["paths_total"]} '
                      'referenced files are not on this computer.')
            if summary['backup']:
                print(f'Previous database kept as {summary["backup"]}.')
            print('Start the dashboard, and stop using the pipeline on the old computer.')
    except TransferError as exc:
        print(f'\n{exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
