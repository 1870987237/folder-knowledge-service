"""Consistent SQLite backup, including committed WAL data."""
import argparse
import os
from pathlib import Path
import sqlite3


def backup(state_dir: str | Path, output: str | Path):
    source = Path(state_dir).resolve() / 'management.sqlite3'
    target = Path(output).resolve()
    if not source.is_file():
        raise ValueError('Management database does not exist')
    # Exclusive creation also rejects overwriting the source or an existing backup.
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    reader = writer = None
    try:
        reader = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)
        writer = sqlite3.connect(target)
        reader.backup(writer, pages=256)
        if writer.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Backup integrity check failed')
    except BaseException:
        if writer:
            writer.close(); writer = None
        target.unlink(missing_ok=True)
        raise
    finally:
        if writer:
            writer.close()
        if reader:
            reader.close()
    return target


def main():
    parser = argparse.ArgumentParser(description='Back up management SQLite while the service is running')
    parser.add_argument('--state-dir', default=os.getenv('KB_STATE_DIR'), required=not os.getenv('KB_STATE_DIR'))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(backup(args.state_dir, args.output))


if __name__ == '__main__':
    main()
