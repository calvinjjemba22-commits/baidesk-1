"""Backup download and CSV import (administrators only)."""
import csv, io, os, secrets, sqlite3, tempfile, zipfile
from urllib.parse import quote
from datetime import datetime
from flask import request, redirect, url_for, flash, send_file, Response, current_app, render_template
from sqlalchemy import func
from sqlalchemy.engine import make_url
from .extensions import db
from .models import (Customer, CustomerPhone, Ticket, TicketHistory, Priority, TicketStatus, Department, Category,
                     User, DepartmentTime, Role, Team)
from .services import audit, log_activity, normalize_phone, next_ticket_number, current_user
from .timeutil import utcnow, to_local, get_tz
from .routes import main, guard, duplicate_customers
from . import sla
from .backup import write_backup, sqlite_path

MAX_ROWS = 5000
CUSTOMER_COLUMNS = ['first_name', 'last_name', 'company', 'email', 'phone', 'location', 'notes']
USER_COLUMNS = ['full_name', 'email', 'role', 'team', 'departments', 'capacity']
TICKET_COLUMNS = ['customer', 'subject', 'description', 'priority', 'status', 'department', 'category',
                  'assignee_email', 'created_at']


def _data_back():
    return redirect(url_for('main.settings', tab='data'))


# ───────────────────────── backup ─────────────────────────
@main.route('/settings/backup', methods=['POST'])
def backup_download():
    r = guard('settings.manage')
    if r:
        return r
    url = make_url(current_app.config['SQLALCHEMY_DATABASE_URI'])
    if url.get_backend_name() != 'sqlite' or not url.database:
        flash('Automatic backup supports the built-in SQLite database. Use your database\'s own backup tool.', 'error')
        return _data_back()
    stamp = to_local(utcnow()).strftime('%Y%m%d-%H%M')
    buf = io.BytesIO()
    write_backup(current_app, buf)
    buf.seek(0)
    audit('BACKUP_DOWNLOADED', 'System')
    db.session.commit()
    return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=f'baidesk-backup-{stamp}.zip')


# ───────────────────────── templates ─────────────────────────
@main.route('/settings/import/template/<kind>.csv')
def import_template(kind):
    r = guard('settings.manage')
    if r:
        return r
    cols, example = {
        'customers': (CUSTOMER_COLUMNS, ['Grace', 'Nakato', 'Nakato Traders', 'grace@example.com', '0772123456', 'Kampala', 'Prefers WhatsApp']),
        'users': (USER_COLUMNS, ['Grace Nakato', 'grace.nakato@example.com', 'Agent', 'Team Nakato', 'Support; Collections', '15']),
        'tickets': (TICKET_COLUMNS, ['CUS-00001 or 0772123456', 'Loan disbursement delayed', 'Customer is waiting for funds',
                                     'High', 'Open', 'Support', 'Payments', 'agent@example.com', '2026-10-01 09:30']),
    }.get(kind, (None, None))
    if not cols:
        return Response('Not found', 404)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(cols)
    w.writerow(example)
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=baidesk-{kind}-template.csv'})


def _read_rows(file, columns):
    if not file or not file.filename:
        return None, 'Choose a CSV file first.'
    try:
        text = file.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        return None, 'The file is not UTF-8 text. In Excel use “Save As → CSV UTF-8”.'
    reader = csv.DictReader(io.StringIO(text))
    headers = [(h or '').strip().lower() for h in (reader.fieldnames or [])]
    if not ({'first_name', 'subject', 'full_name'} & set(headers)):
        return None, 'The first row must be the column headings. Download the template to see them.'
    rows = []
    for i, raw in enumerate(reader, start=2):
        row = {(k or '').strip().lower(): (v or '').strip() for k, v in raw.items()}
        if any(row.values()):
            rows.append((i, row))
        if len(rows) > MAX_ROWS:
            return None, f'Too many rows — import up to {MAX_ROWS} at a time.'
    return rows, None


def _parse_local(value):
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d', '%d/%m/%Y %H:%M', '%d/%m/%Y'):
        try:
            d = datetime.strptime(value, fmt)
            return d.replace(tzinfo=get_tz()).astimezone(__import__('datetime').timezone.utc).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _find_customer(ref):
    if not ref:
        return None
    c = Customer.query.filter(func.lower(Customer.customer_number) == ref.lower()).first()
    if c:
        return c
    ph = normalize_phone(ref)
    if ph:
        row = CustomerPhone.query.filter_by(phone_number=ph).first()
        if row:
            return row.customer
    return Customer.query.filter(func.lower(Customer.email) == ref.lower()).first() if '@' in ref else None


# ───────────────────────── import ─────────────────────────
@main.route('/settings/import/<kind>', methods=['POST'])
def import_csv(kind):
    r = guard('settings.manage')
    if r:
        return r
    if kind not in ('customers', 'tickets', 'users'):
        return Response('Not found', 404)
    rows, err = _read_rows(request.files.get('file'), None)
    if err:
        flash(err, 'error')
        return _data_back()
    dry = 'validate_only' in request.form
    result = {'customers': _import_customers, 'tickets': _import_tickets, 'users': _import_users}[kind](rows, dry)
    if dry:
        db.session.rollback()
    else:
        audit('IMPORT_' + kind.upper(), 'System', None, f"{result['created']} created, {len(result['errors'])} errors")
        db.session.commit()
    return render_template('import_result.html', kind=kind, dry=dry, **result)


def _import_customers(rows, dry):
    created, skipped, errors = 0, [], []
    seen_phones, seen_emails = set(), set()
    next_no = (db.session.query(func.max(Customer.id)).scalar() or 0) + 1
    for line, row in rows:
        first = row.get('first_name', '')
        if not first:
            errors.append((line, 'first_name is required'))
            continue
        phone, email = normalize_phone(row.get('phone', '')), row.get('email', '').lower()
        if email and '@' not in email:
            errors.append((line, f'“{email}” is not a valid email'))
            continue
        dup = duplicate_customers([phone] if phone else [], email)
        if dup or (phone and phone in seen_phones) or (email and email in seen_emails):
            skipped.append((line, f'{first} {row.get("last_name", "")}'.strip() + ' already exists (same phone or email)'))
            continue
        c = Customer(customer_number=f'CUS-{next_no:05d}', first_name=first, last_name=row.get('last_name', ''),
                     company=row.get('company') or None, email=email or None, location=row.get('location') or None,
                     notes=row.get('notes') or None)
        db.session.add(c)
        db.session.flush()
        next_no = c.id + 1
        if phone:
            db.session.add(CustomerPhone(customer_id=c.id, phone_number=phone, is_primary=True))
            seen_phones.add(phone)
        if email:
            seen_emails.add(email)
        created += 1
    return {'created': created, 'skipped': skipped, 'errors': errors, 'total': len(rows)}


def _import_tickets(rows, dry):
    created, skipped, errors = 0, [], []
    me = current_user()
    prios = {p.name.lower(): p for p in Priority.query.all()}
    stats = {s.name.lower(): s for s in TicketStatus.query.all()}
    depts = {d.name.lower(): d for d in Department.query.all()}
    cats = {c.name.lower(): c for c in Category.query.all()}
    users = {u.email.lower(): u for u in User.query.all()}
    for line, row in rows:
        c = _find_customer(row.get('customer', ''))
        subject = row.get('subject', '')
        if not c:
            errors.append((line, f'customer “{row.get("customer", "")}” not found — use a customer number, phone or email'))
            continue
        if not subject:
            errors.append((line, 'subject is required'))
            continue
        p = prios.get((row.get('priority') or 'medium').lower())
        s = stats.get((row.get('status') or 'open').lower())
        if not p or not s:
            errors.append((line, f'unknown priority or status (“{row.get("priority", "")}” / “{row.get("status", "")}”)'))
            continue
        created_at = _parse_local(row['created_at']) if row.get('created_at') else utcnow()
        if not created_at:
            errors.append((line, 'created_at must look like 2026-10-01 09:30'))
            continue
        d = depts.get(row.get('department', '').lower()) if row.get('department') else None
        if row.get('department') and not d:
            errors.append((line, f'unknown department “{row["department"]}”'))
            continue
        a = users.get(row.get('assignee_email', '').lower()) if row.get('assignee_email') else None
        if row.get('assignee_email') and not a:
            errors.append((line, f'no user with email {row["assignee_email"]}'))
            continue
        t = Ticket(ticket_number=next_ticket_number(), customer_id=c.id, subject=subject[:255],
                   description=row.get('description') or subject, category_id=(cats.get(row.get('category', '').lower()).id
                                                                                if row.get('category', '').lower() in cats else None),
                   priority_id=p.id, status_id=s.id, department_id=d.id if d else None,
                   assigned_user_id=a.id if a else None, created_by=me.id, created_at=created_at, updated_at=created_at)
        db.session.add(t)
        db.session.flush()
        sla.apply_deadlines(t)
        sla.on_status_change(t, s.slug, created_at)
        if s.slug in ('resolved', 'closed'):
            t.resolved_at = created_at
        db.session.add(TicketHistory(ticket_id=t.id, user_id=me.id, action_type='TICKET_CREATED', new_value=t.ticket_number,
                                     reason='Imported from CSV'))
        if d:
            db.session.add(DepartmentTime(ticket_id=t.id, department_id=d.id))
        created += 1
    return {'created': created, 'skipped': skipped, 'errors': errors, 'total': len(rows)}


# ───────────────────────── staff accounts ─────────────────────────
_PW_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'      # no 0/O/1/l/I look-alikes
_ROLE_WORDS = {'agent': 'Agent', 'team lead': 'Manager / Team Lead', 'teamlead': 'Manager / Team Lead',
               'lead': 'Manager / Team Lead', 'manager': 'Manager / Team Lead'}


def temp_password():
    return ''.join(secrets.choice(_PW_ALPHABET) for _ in range(10))


def _import_users(rows, dry):
    """Create staff accounts with one-time temporary passwords (the person must change it at first sign-in).
    Admin accounts cannot be created this way. Blank departments means all active departments."""
    created, skipped, errors, credentials = 0, [], [], []
    roles = {r.name: r for r in Role.query.all()}
    depts = {d.name.lower(): d for d in Department.query.filter_by(is_active=True).all()}
    teams = {t.name.lower(): t for t in Team.query.all()}
    seen = set()
    for line, row in rows:
        name, email = ' '.join(row.get('full_name', '').split()), row.get('email', '').lower()
        role_name = _ROLE_WORDS.get(row.get('role', 'agent').strip().lower() or 'agent')
        if not name or '@' not in email or ' ' in email:
            errors.append((line, 'full_name and a valid email are required'))
            continue
        if not role_name or role_name not in roles:
            errors.append((line, f'role “{row.get("role", "")}” must be Agent or Team Lead'))
            continue
        if email in seen or User.query.filter(func.lower(User.email) == email).first():
            skipped.append((line, f'{name} ({email}) already exists'))
            continue
        cap_raw = row.get('capacity', '')
        if cap_raw and (not cap_raw.isdigit() or not 1 <= int(cap_raw) <= 500):
            errors.append((line, 'capacity must be a number from 1 to 500'))
            continue
        wanted = [d.strip() for d in row.get('departments', '').replace(',', ';').split(';') if d.strip()]
        bad = [d for d in wanted if d.lower() not in depts]
        if bad:
            errors.append((line, f'unknown department “{bad[0]}”'))
            continue
        team = None
        if row.get('team'):
            team = teams.get(row['team'].lower())
            if not team:
                team = Team(name=row['team'][:100])
                db.session.add(team)
                teams[row['team'].lower()] = team
            if role_name != 'Agent' and team.lead and team.lead.email.lower() != email:
                errors.append((line, f'team “{team.name}” already has a lead ({team.lead.name})'))
                continue
        pw = temp_password()
        u = User(name=name[:160], email=email, capacity=int(cap_raw) if cap_raw else 15, must_change_password=True)
        u.set_password(pw)
        db.session.add(u)
        u.roles = [roles[role_name]]
        u.departments = [depts[d.lower()] for d in wanted] if wanted else list(depts.values())
        if team:
            team.members.append(u)
            if role_name != 'Agent':
                team.lead = u
        seen.add(email)
        credentials.append((name, email, 'Team Lead' if role_name != 'Agent' else 'Agent', team.name if team else '', pw))
        created += 1
    uri = None
    if credentials and not dry:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(['name', 'email', 'role', 'team', 'temporary_password'])
        w.writerows(credentials)
        uri = 'data:text/csv;charset=utf-8,' + quote(out.getvalue())
    return {'created': created, 'skipped': skipped, 'errors': errors, 'total': len(rows),
            'credentials': credentials if not dry else [], 'creds_uri': uri}
