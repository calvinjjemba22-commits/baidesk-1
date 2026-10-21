"""CSV export helpers (Excel-friendly: UTF-8 BOM, formula-injection safe)."""
import csv, io
from flask import Response
from sqlalchemy.orm import joinedload
from .models import Ticket
from .services import ACTIVE_SLUGS
from .timeutil import localdt, utcnow

TICKET_LOAD = [joinedload(Ticket.customer), joinedload(Ticket.category), joinedload(Ticket.priority),
               joinedload(Ticket.status), joinedload(Ticket.department),
               joinedload(Ticket.assigned_user), joinedload(Ticket.creator)]

TICKET_HEADERS = ['Ticket', 'Created', 'Customer', 'Customer no.', 'Subject', 'Category', 'Priority',
                  'Status', 'Department', 'Assignee', 'Created by', 'Due', 'First response',
                  'Resolved', 'Closed', 'Resolution (hours)', 'Resolution SLA']
CSV_FMT = '%Y-%m-%d %H:%M'


def _ld(dt):
    return localdt(dt, CSV_FMT) if dt else ''


def hours_between(a, b):
    return round((b - a).total_seconds() / 3600, 2) if a and b else ''


def sla_result(t):
    if t.due_at and t.resolved_at:
        return 'Met' if t.resolved_at <= t.due_at else 'Missed'
    if t.due_at and t.status and t.status.slug in ACTIVE_SLUGS and t.due_at < utcnow():
        return 'Overdue (open)'
    return ''


def ticket_row(t):
    c = t.customer
    return [t.ticket_number, _ld(t.created_at), f'{c.first_name} {c.last_name or ""}'.strip(), c.customer_number,
            t.subject, t.category.name if t.category else '', t.priority.name, t.status.name,
            t.department.name if t.department else 'Unassigned',
            t.assigned_user.name if t.assigned_user else 'Unassigned',
            t.creator.name if t.creator else '', _ld(t.due_at), _ld(t.first_response_at),
            _ld(t.resolved_at), _ld(t.closed_at), hours_between(t.created_at, t.resolved_at), sla_result(t)]


def _clean(value):
    if value is None:
        return ''
    if isinstance(value, str):
        # Neutralise spreadsheet formulas (=, +, -, @) coming from user-entered text.
        return "'" + value if value and value[0] in '=+-@\t\r' else value
    if isinstance(value, float):
        return round(value, 2)
    return value


def csv_response(filename, sections):
    """sections: list of (title or None, headers, rows)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    for i, (title, headers, rows) in enumerate(sections):
        if i:
            w.writerow([])
        if title:
            w.writerow([title])
        w.writerow(headers)
        for row in rows:
            w.writerow([_clean(c) for c in row])
    resp = Response('\ufeff' + buf.getvalue(), mimetype='text/csv')
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    resp.headers['Cache-Control'] = 'no-store'
    return resp
