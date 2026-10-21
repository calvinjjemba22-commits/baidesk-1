"""SQLite settings for a shared, always-on server.

* WAL journal: readers no longer block the writer (and vice versa) — the main fix for "database is locked".
* busy_timeout: a writer waits (up to 15 s) for the lock instead of failing immediately.
* synchronous=NORMAL: safe with WAL (a power cut can lose the last moments, never corrupt the file) and much faster.
Set SQLITE_WAL=0 to switch the WAL part off (used only to compare in load tests).
"""
import os
from sqlalchemy import event


def tune_sqlite(engine):
    if engine.url.get_backend_name() != 'sqlite':
        return

    @event.listens_for(engine, 'connect')
    def _on_connect(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        if os.getenv('SQLITE_WAL', '1') == '1':
            cur.execute('PRAGMA journal_mode=WAL')
        cur.execute('PRAGMA synchronous=NORMAL')
        cur.execute('PRAGMA busy_timeout=15000')
        cur.close()
