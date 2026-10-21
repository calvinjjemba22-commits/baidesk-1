"""Backups: one zip with a consistent copy of the database plus the uploaded files."""
import os
import sqlite3
import tempfile
import zipfile
from sqlalchemy.engine import make_url


def sqlite_path(app):
    url = make_url(app.config['SQLALCHEMY_DATABASE_URI'])
    return url.database if url.get_backend_name() == 'sqlite' and url.database else None


def write_backup(app, fileobj):
    """Write the backup zip to `fileobj` (a path or a binary file). Safe while the app is running."""
    path = sqlite_path(app)
    if not path:
        raise RuntimeError("Automatic backup supports the built-in SQLite database. Use your database's own backup tool.")
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, 'baidesk.db')
        src, dst = sqlite3.connect(path), sqlite3.connect(copy)
        try:
            src.backup(dst)            # a consistent snapshot even while people are working
        finally:
            dst.close()
            src.close()
        with zipfile.ZipFile(fileobj, 'w', zipfile.ZIP_DEFLATED) as z:
            z.write(copy, 'baidesk.db')
            up = app.config['UPLOAD_FOLDER']
            if os.path.isdir(up):
                for name in sorted(os.listdir(up)):
                    full = os.path.join(up, name)
                    if os.path.isfile(full):
                        z.write(full, f'uploads/{name}')
            z.writestr('README.txt', 'BaiDesk backup. To restore: stop BaiDesk, replace instance/baidesk.db with '
                       'baidesk.db and copy the uploads/ folder back, then start BaiDesk.\n')


def prune(folder, keep):
    """Keep only the newest `keep` backups in the folder; returns how many were deleted."""
    files = sorted((f for f in os.listdir(folder) if f.startswith('baidesk-backup-') and f.endswith('.zip')), reverse=True)
    for old in files[keep:]:
        os.remove(os.path.join(folder, old))
    return max(0, len(files) - keep)
