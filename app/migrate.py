"""Tiny schema upgrader.

`db.create_all()` creates missing *tables* but never adds columns to tables that already exist, so a database
made by an earlier BaiDesk would break the moment a model gained a column. This adds the missing columns
(additive and idempotent — it never drops or rewrites anything) and back-fills sensible values.
"""
from sqlalchemy import inspect, text
from .extensions import db

# (table, column, SQL type + default) — keep in step with models.py
ADDITIONS = [
    ('tickets', 'first_response_due_at', 'DATETIME'),
    ('tickets', 'sla_paused_at', 'DATETIME'),
    ('tickets', 'sla_paused_minutes', 'INTEGER NOT NULL DEFAULT 0'),
    ('users', 'capacity', 'INTEGER NOT NULL DEFAULT 15'),
    ('users', 'is_available', 'BOOLEAN NOT NULL DEFAULT 1'),
    ('departments', 'capacity', 'INTEGER'),
    ('users', 'must_change_password', 'BOOLEAN NOT NULL DEFAULT 0'),
]


def upgrade_schema():
    insp = inspect(db.engine)
    existing = {t: {c['name'] for c in insp.get_columns(t)} for t in insp.get_table_names()}
    added = []
    with db.engine.begin() as conn:
        for table, column, ddl in ADDITIONS:
            if table in existing and column not in existing[table]:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
                added.append((table, column))
        if ('tickets', 'sla_paused_at') in added:
            # Tickets already waiting on the customer start with their clock paused.
            conn.execute(text(
                "UPDATE tickets SET sla_paused_at = updated_at WHERE status_id IN "
                "(SELECT id FROM ticket_statuses WHERE slug = 'pending')"))
    return added
