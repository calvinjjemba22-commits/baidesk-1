import re
from datetime import timedelta
from urllib.parse import urlparse
from flask import (Blueprint, render_template, request, redirect, url_for, session, flash,
                   jsonify, abort, send_from_directory, current_app)
from sqlalchemy import or_, func
from sqlalchemy.orm import aliased
from .extensions import db
from .models import *
from .services import *
from .timeutil import utcnow, local_midnight_utc, to_local, localdt, iso_utc
from .exports import csv_response, TICKET_HEADERS, TICKET_LOAD, ticket_row
from . import dashboard_data, sla, routing, quality

main = Blueprint('main', __name__)

CHANNELS = ['Call', 'WhatsApp', 'SMS', 'Email', 'Walk-in']
DIRECTIONS = ['Inbound', 'Outbound']
MIN_PASSWORD = 8


# ───────────────────────── helpers ─────────────────────────
def require_login():
    u = current_user()
    if not u:
        return redirect(url_for('main.login'))
    if not u.is_active:
        session.clear()
        return redirect(url_for('main.login'))
    return None


def allowed(code):
    u = current_user()
    return bool(u and u.has_permission(code))


def guard(code):
    r = require_login()
    if r:
        return r
    if not allowed(code):
        abort(403)


def get_or_404(model, ident):
    return db.session.get(model, ident) or abort(404)


def safe_next(target, default):
    """Only allow same-site relative redirects."""
    if target and target.startswith('/') and not target.startswith('//') and '\\' not in target:
        p = urlparse(target)
        if not p.scheme and not p.netloc:
            return target
    return default


def paginate(query, per_page=None):
    page = request.args.get('page', 1, type=int) or 1
    return query.paginate(page=max(1, page), per_page=per_page or current_app.config['PER_PAGE'], error_out=False)


def department_scope(query):
    """Limit non-managers to tickets they own or tickets in their departments."""
    u = current_user()
    if not u or u.is_manager():
        return query
    dept_ids = [d.id for d in u.departments]
    return query.filter(or_(Ticket.assigned_user_id == u.id,
                            Ticket.department_id.in_(dept_ids) if dept_ids else False))


def user_can_access_ticket(ticket):
    u = current_user()
    if not u or u.is_manager():
        return True
    return ticket.assigned_user_id == u.id or any(d.id == ticket.department_id for d in u.departments)


def redirect_ticket(t, anchor=''):
    """Back to the ticket, or to the queue if the change moved it out of the user's reach."""
    if user_can_access_ticket(t):
        return redirect(url_for('main.ticket_detail', ticket_id=t.id) + anchor)
    flash('The ticket was moved out of your queue.', 'success')
    return redirect(url_for('main.tickets'))


def active_statuses():
    return TicketStatus.query.filter_by(is_active=True).order_by(TicketStatus.sort_order).all()


def active_departments():
    return Department.query.filter_by(is_active=True).order_by(Department.name).all()


def active_users():
    return User.query.filter_by(is_active=True).order_by(User.name).all()


def is_active_status():
    return Ticket.status.has(TicketStatus.slug.in_(ACTIVE_SLUGS))


def resolution_hours(base):
    rows = base.filter(Ticket.resolved_at.isnot(None)).all()
    if not rows:
        return 0
    seconds = sum(max(0, (t.resolved_at - t.created_at).total_seconds()) for t in rows)
    return seconds / len(rows) / 3600


def customer_filter(q):
    like = f'%{q}%'
    return or_(Customer.customer_number.ilike(like), Customer.first_name.ilike(like),
               Customer.last_name.ilike(like), Customer.email.ilike(like), Customer.company.ilike(like),
               Customer.phones.any(CustomerPhone.phone_number.ilike(like)))


def duplicate_customers(phones, email, exclude_id=None):
    found = {}
    for ph in phones:
        for row in CustomerPhone.query.filter_by(phone_number=ph).all():
            found[row.customer_id] = row.customer
    if email:
        for c in Customer.query.filter(func.lower(Customer.email) == email.lower()).all():
            found[c.id] = c
    found.pop(exclude_id, None)
    return list(found.values())


# ───────────── ticket change helpers (shared by update / bulk / escalation) ─────────────
def change_status(t, status, u):
    if status.id == t.status_id:
        return False
    old = t.status.name if t.status else '—'
    now = utcnow()
    t.status_id, t.status = status.id, status
    if status.slug == 'resolved':
        t.resolved_at = t.resolved_at or now
        t.closed_at = None
    elif status.slug == 'closed':
        t.closed_at = now
        t.resolved_at = t.resolved_at or now
    elif status.slug in ACTIVE_SLUGS:
        t.resolved_at = None
        t.closed_at = None
    sla.on_status_change(t, status.slug, now)
    db.session.add(TicketHistory(ticket_id=t.id, user_id=u.id, action_type='FIELD_CHANGED',
                                 field_name='status_id', old_value=old, new_value=status.name))
    log_activity('TICKET_STATUS_ID_CHANGED', f'Status: {old} → {status.name}', customer_id=t.customer_id,
                 ticket_id=t.id, department_id=t.department_id, entity_type='Ticket', entity_id=t.id)
    return True


def change_priority(t, prio, u):
    if prio.id == t.priority_id:
        return False
    old = t.priority.name if t.priority else '—'
    t.priority_id, t.priority = prio.id, prio
    sla.apply_deadlines(t)
    db.session.add(TicketHistory(ticket_id=t.id, user_id=u.id, action_type='FIELD_CHANGED',
                                 field_name='priority_id', old_value=old, new_value=prio.name))
    log_activity('TICKET_PRIORITY_ID_CHANGED', f'Priority: {old} → {prio.name}', customer_id=t.customer_id,
                 ticket_id=t.id, department_id=t.department_id, entity_type='Ticket', entity_id=t.id)
    return True


def assignee_error(assignee_id, dept_id, actor):
    au = get_or_404(User, assignee_id)
    if not au.is_active:
        return 'Inactive users cannot be assigned tickets.'
    if dept_id and not any(d.id == dept_id for d in au.departments) and not actor.is_admin():
        return 'The selected agent does not belong to the selected department.'
    return None


def change_assignee(t, new_id, u):
    if new_id == t.assigned_user_id:
        return False
    old = t.assigned_user.name if t.assigned_user else 'Unassigned'
    new_user = db.session.get(User, new_id) if new_id else None
    t.assigned_user_id, t.assigned_user = new_id, new_user
    new = new_user.name if new_user else 'Unassigned'
    db.session.add(TicketHistory(ticket_id=t.id, user_id=u.id, action_type='FIELD_CHANGED',
                                 field_name='assigned_user_id', old_value=old, new_value=new))
    log_activity('TICKET_ASSIGNED_USER_CHANGED', f'Assigned user: {old} → {new}', customer_id=t.customer_id,
                 ticket_id=t.id, department_id=t.department_id, entity_type='Ticket', entity_id=t.id)
    if new_id and new_id != u.id:
        notify(new_id, 'assignment', 'Ticket assigned', f'{t.ticket_number} was assigned to you.', 'Ticket', t.id)
    return True


def close_department_time(t):
    if t.department_id:
        row = (DepartmentTime.query.filter_by(ticket_id=t.id, department_id=t.department_id, ended_at=None)
               .order_by(DepartmentTime.started_at.desc()).first())
        if row:
            row.ended_at = utcnow()
            row.duration_seconds = int((row.ended_at - row.started_at).total_seconds())


def change_department(t, new_id, u, record=True):
    if new_id == t.department_id:
        return False
    old = t.department.name if t.department else 'Unassigned'
    close_department_time(t)
    new_dept = db.session.get(Department, new_id) if new_id else None
    t.department_id, t.department = new_id, new_dept
    if new_id:
        db.session.add(DepartmentTime(ticket_id=t.id, department_id=new_id))
    if record:
        new = new_dept.name if new_dept else 'Unassigned'
        db.session.add(TicketHistory(ticket_id=t.id, user_id=u.id, action_type='FIELD_CHANGED',
                                     field_name='department_id', old_value=old, new_value=new))
        log_activity('TICKET_DEPARTMENT_ID_CHANGED', f'Department: {old} → {new}', customer_id=t.customer_id,
                     ticket_id=t.id, department_id=t.department_id, entity_type='Ticket', entity_id=t.id)
    return True


# ───────────────────────── auth ─────────────────────────
@main.route('/login', methods=['GET', 'POST'])
def login():
    if current_user():
        return redirect(url_for('main.dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        ip = (request.remote_addr or '')[:64]
        if login_locked(email, ip):
            flash(f'Too many failed attempts. Please wait {LOCKOUT_MINUTES} minutes and try again.', 'error')
            return render_template('login.html'), 429
        u = User.query.filter(func.lower(User.email) == email).first()
        if u and u.is_active and u.check_password(password):
            LoginAttempt.query.filter_by(email=email).delete()
            session.clear()
            session['user_id'] = u.id
            session.permanent = True
            audit('USER_LOGIN', 'User', u.id)
            db.session.commit()
            if u.must_change_password:
                flash('Please choose your own password before you continue.', 'info')
                return redirect(url_for('main.profile'))
            return redirect(url_for('main.dashboard' if u.is_manager() else 'main.my_day'))
        db.session.add(LoginAttempt(email=email[:255], ip=ip))
        db.session.commit()
        flash('Invalid email or password.', 'error')
    return render_template('login.html')


LOCKOUT_MINUTES = 15
MAX_FAILS_PER_EMAIL = 5      # failed sign-ins for one address in the window
MAX_FAILS_PER_IP = 30        # ... or from one computer (a whole office can share one address)


def login_locked(email, ip):
    since = utcnow() - timedelta(minutes=LOCKOUT_MINUTES)
    LoginAttempt.query.filter(LoginAttempt.created_at < since - timedelta(days=1)).delete()    # housekeeping
    by_email = LoginAttempt.query.filter(LoginAttempt.email == email, LoginAttempt.created_at >= since).count()
    by_ip = LoginAttempt.query.filter(LoginAttempt.ip == ip, LoginAttempt.created_at >= since).count() if ip else 0
    return by_email >= MAX_FAILS_PER_EMAIL or by_ip >= MAX_FAILS_PER_IP


@main.route('/logout')
def logout():
    u = current_user()
    if u:
        audit('USER_LOGOUT', 'User', u.id)
        db.session.commit()
    session.clear()
    return redirect(url_for('main.login'))


# ───────────────────────── dashboard & search ─────────────────────────
@main.route('/')
def dashboard():
    r = guard('dashboard.view')
    if r:
        return r
    me = current_user()
    now = utcnow()
    base = department_scope(Ticket.query)
    data = dashboard_data.build(base, is_active_status(), now, me)
    hour = to_local(now).hour
    greeting = 'Good morning' if hour < 12 else 'Good afternoon' if hour < 18 else 'Good evening'
    return render_template('dashboard.html', greeting=greeting, **data)


@main.route('/search')
def global_search():
    r = guard('dashboard.view')
    if r:
        return r
    q = request.args.get('q', '').strip()
    customers, tickets, interactions = [], [], []
    if q:
        like = f'%{q}%'
        customers = Customer.query.filter(customer_filter(q)).order_by(Customer.updated_at.desc()).limit(10).all()
        tickets = department_scope(Ticket.query).filter(or_(
            Ticket.ticket_number.ilike(like), Ticket.subject.ilike(like), Ticket.description.ilike(like),
            Ticket.customer.has(or_(Customer.first_name.ilike(like), Customer.last_name.ilike(like),
                                    Customer.email.ilike(like))))).order_by(Ticket.updated_at.desc()).limit(10).all()
        interactions = Interaction.query.filter(or_(Interaction.subject.ilike(like), Interaction.notes.ilike(like))
                                                ).order_by(Interaction.created_at.desc()).limit(10).all()
    return render_template('search.html', q=q, customers=customers, tickets=tickets, interactions=interactions)


# ───────────────────────── customers ─────────────────────────
@main.route('/customers')
def customers():
    r = guard('customer.view')
    if r:
        return r
    q = request.args.get('q', '').strip()
    status = request.args.get('status', '')
    query = Customer.query
    if q:
        query = query.filter(customer_filter(q))
    if status:
        query = query.filter(Customer.status == status)
    page = paginate(query.order_by(Customer.updated_at.desc(), Customer.id.desc()))
    return render_template('customers.html', page=page, q=q, status=status)


@main.route('/customers/export')
def customers_export():
    r = guard('customer.view')
    if r:
        return r
    q = request.args.get('q', '').strip()
    status = request.args.get('status', '')
    query = Customer.query
    if q:
        query = query.filter(customer_filter(q))
    if status:
        query = query.filter(Customer.status == status)
    rows = [[c.customer_number, f'{c.first_name} {c.last_name or ""}'.strip(), c.company or '', c.email or '',
             ', '.join(p.phone_number for p in c.phones), c.location or '', c.status,
             localdt(c.created_at, '%Y-%m-%d %H:%M'), len(c.tickets)]
            for c in query.order_by(Customer.id).all()]
    audit('CUSTOMERS_EXPORTED', 'Customer', None, f'{len(rows)} rows')
    db.session.commit()
    return csv_response('baidesk-customers.csv', [(None, ['Customer no.', 'Name', 'Company', 'Email', 'Phones',
                                                           'Location', 'Status', 'Created', 'Tickets'], rows)])


def _customer_from_form():
    return dict(first_name=request.form.get('first_name', '').strip(),
                last_name=request.form.get('last_name', '').strip(),
                company=request.form.get('company', '').strip(),
                email=request.form.get('email', '').strip().lower(),
                location=request.form.get('location', '').strip(),
                notes=request.form.get('notes', '').strip())


@main.route('/customers/new', methods=['GET', 'POST'])
def customer_new():
    r = guard('customer.create')
    if r:
        return r
    if request.method == 'POST':
        data = _customer_from_form()
        phone = normalize_phone(request.form.get('phone', ''))
        if not data['first_name']:
            flash('First name is required.', 'error')
            return render_template('customer_form.html', form=request.form, duplicates=[], customer=None)
        duplicates = duplicate_customers([phone] if phone else [], data['email'])
        if duplicates:
            flash('Possible duplicate detected. Review the existing customer before creating another record.', 'error')
            return render_template('customer_form.html', form=request.form, duplicates=duplicates, customer=None)
        next_no = (db.session.query(func.max(Customer.id)).scalar() or 0) + 1
        c = Customer(customer_number=f'CUS-{next_no:05d}', **data)
        db.session.add(c)
        db.session.flush()
        if phone:
            db.session.add(CustomerPhone(customer_id=c.id, phone_number=phone, is_primary=True))
        log_activity('CUSTOMER_CREATED', f'Customer {c.customer_number} created.', customer_id=c.id,
                     entity_type='Customer', entity_id=c.id)
        audit('CUSTOMER_CREATED', 'Customer', c.id)
        db.session.commit()
        flash('Customer created.', 'success')
        return redirect(url_for('main.customer_detail', customer_id=c.id))
    return render_template('customer_form.html', form={}, duplicates=[], customer=None)


@main.route('/customers/<int:customer_id>/edit', methods=['GET', 'POST'])
def customer_edit(customer_id):
    r = guard('customer.edit')
    if r:
        return r
    c = get_or_404(Customer, customer_id)
    primary = next((p for p in c.phones if p.is_primary), c.phones[0] if c.phones else None)
    if request.method == 'POST':
        data = _customer_from_form()
        status = request.form.get('status', 'Active')
        phone = normalize_phone(request.form.get('phone', ''))
        extras = []
        for line in request.form.get('extra_phones', '').splitlines():
            n = normalize_phone(line)
            if n and n != phone and n not in extras:
                extras.append(n)
        if status not in ('Active', 'Inactive'):
            abort(400)
        form_for_render = request.form.to_dict()
        if not data['first_name']:
            flash('First name is required.', 'error')
            return render_template('customer_form.html', form=form_for_render, duplicates=[], customer=c)
        duplicates = duplicate_customers(([phone] if phone else []) + extras, data['email'], exclude_id=c.id)
        if duplicates:
            flash('Another customer already uses this phone number or email.', 'error')
            return render_template('customer_form.html', form=form_for_render, duplicates=duplicates, customer=c)
        for k, v in data.items():
            setattr(c, k, v)
        c.status = status
        existing = {p.phone_number: p for p in c.phones}
        wanted = ([phone] if phone else []) + extras
        for number, row in list(existing.items()):
            if number not in wanted:
                c.phones.remove(row)
        for number in wanted:
            row = existing.get(number)
            if row is None:
                row = CustomerPhone(phone_number=number)
                c.phones.append(row)
            row.is_primary = (number == phone)
        log_activity('CUSTOMER_UPDATED', f'Customer {c.customer_number} details updated.', customer_id=c.id,
                     entity_type='Customer', entity_id=c.id)
        audit('CUSTOMER_UPDATED', 'Customer', c.id)
        db.session.commit()
        flash('Customer updated.', 'success')
        return redirect(url_for('main.customer_detail', customer_id=c.id))
    form = {'first_name': c.first_name, 'last_name': c.last_name or '', 'company': c.company or '',
            'email': c.email or '', 'location': c.location or '', 'notes': c.notes or '', 'status': c.status,
            'phone': primary.phone_number if primary else '',
            'extra_phones': '\n'.join(p.phone_number for p in c.phones if p is not primary)}
    return render_template('customer_form.html', form=form, duplicates=[], customer=c)


@main.route('/customers/<int:customer_id>')
def customer_detail(customer_id):
    r = guard('customer.view')
    if r:
        return r
    c = get_or_404(Customer, customer_id)
    timeline = ActivityLog.query.filter_by(customer_id=c.id).order_by(ActivityLog.created_at.desc()).limit(250).all()
    open_tickets = [t for t in c.tickets if t.status and t.status.slug not in ('resolved', 'closed')]
    interactions = (Interaction.query.filter_by(customer_id=c.id)
                    .order_by(Interaction.interaction_at.desc()).limit(10).all())
    links = AttachmentLink.query.filter_by(customer_id=c.id).order_by(AttachmentLink.id.desc()).limit(50).all()
    attachments = [l for l in links if l.attachment and (not l.ticket or user_can_access_ticket(l.ticket))]
    promises = (PaymentPromise.query.filter_by(customer_id=c.id)
                .order_by(PaymentPromise.promised_date.desc()).limit(10).all())
    return render_template('customer_detail.html', customer=c, timeline=timeline, open_tickets=open_tickets,
                           interactions=interactions, attachments=attachments, promises=promises)


@main.route('/customers/<int:customer_id>/interactions/new', methods=['GET', 'POST'])
def interaction_new(customer_id):
    r = guard('interaction.create')
    if r:
        return r
    c = get_or_404(Customer, customer_id)
    if request.method == 'POST':
        channel = request.form.get('channel')
        direction = request.form.get('direction')
        if channel not in CHANNELS or direction not in DIRECTIONS:
            abort(400)
        ticket_id = request.form.get('ticket_id', type=int)
        if ticket_id:
            linked = get_or_404(Ticket, ticket_id)
            if linked.customer_id != c.id:
                abort(400)
        x = Interaction(customer_id=c.id, user_id=current_user().id, channel=channel, direction=direction,
                        disposition=request.form.get('disposition') or None,
                        subject=request.form.get('subject', '').strip(),
                        notes=request.form.get('notes', '').strip(), ticket_id=ticket_id)
        db.session.add(x)
        db.session.flush()
        try:
            att = save_upload(request.files.get('attachment'), current_user().id)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'error')
            return redirect(url_for('main.interaction_new', customer_id=c.id))
        if att:
            db.session.add(AttachmentLink(attachment_id=att.id, interaction_id=x.id, ticket_id=ticket_id,
                                          customer_id=c.id))
        log_activity('INTERACTION_LOGGED', f'{x.channel} {x.direction.lower()} interaction logged.',
                     customer_id=c.id, ticket_id=x.ticket_id, entity_type='Interaction', entity_id=x.id)
        db.session.commit()
        flash('Interaction logged.', 'success')
        return redirect(url_for('main.customer_detail', customer_id=c.id))
    return render_template('interaction_form.html', customer=c, channels=CHANNELS, directions=DIRECTIONS,
                           dispositions=Disposition.query.filter_by(is_active=True).all(),
                           tickets=Ticket.query.filter_by(customer_id=c.id).order_by(Ticket.created_at.desc()).all())


# ───────────────────────── tickets ─────────────────────────
TICKET_VIEWS = [('all', 'All'), ('open', 'Open'), ('mine', 'Mine'), ('unassigned', 'Unassigned'),
                ('overdue', 'Overdue'), ('escalated', 'Escalated')]
TICKET_VIEW_KEYS = {k for k, _ in TICKET_VIEWS} | {'due_today', 'resolved_today'}
TICKET_SORTS = ('ticket', 'customer', 'priority', 'status', 'department', 'assignee', 'due', 'updated')


def build_ticket_query(args):
    """Filtered + sorted ticket query for the queue and its CSV export. Returns (query, state)."""
    me = current_user()
    q = args.get('q', '').strip()
    view = args.get('view') or ('mine' if args.get('mine') else 'overdue' if args.get('overdue') else 'all')
    if view not in TICKET_VIEW_KEYS:
        view = 'all'
    sort = args.get('sort', 'updated')
    sort = sort if sort in TICKET_SORTS else 'updated'
    direction = 'asc' if args.get('dir') == 'asc' else 'desc'
    status = args.get('status', '')
    priority = args.get('priority', '')
    department = args.get('department', type=int)
    assignee = args.get('assignee', type=int)

    query = department_scope(Ticket.query)
    now = utcnow()
    if view == 'open':
        query = query.filter(is_active_status())
    elif view == 'mine':
        query = query.filter(Ticket.assigned_user_id == me.id, is_active_status())
    elif view == 'unassigned':
        query = query.filter(Ticket.assigned_user_id.is_(None), is_active_status())
    elif view == 'overdue':
        query = query.filter(Ticket.due_at < now, Ticket.sla_paused_at.is_(None), is_active_status())
    elif view == 'escalated':
        query = query.filter(Ticket.status.has(TicketStatus.slug == 'escalated'))
    elif view == 'due_today':
        query = query.filter(Ticket.due_at >= local_midnight_utc(), Ticket.due_at < local_midnight_utc(1),
                             is_active_status())
    elif view == 'resolved_today':
        query = query.filter(Ticket.resolved_at >= local_midnight_utc())
    if q:
        like = f'%{q}%'
        query = query.filter(or_(Ticket.ticket_number.ilike(like), Ticket.subject.ilike(like),
                                 Ticket.description.ilike(like),
                                 Ticket.customer.has(or_(Customer.first_name.ilike(like),
                                                         Customer.last_name.ilike(like),
                                                         Customer.email.ilike(like)))))
    if status:
        query = query.filter(Ticket.status.has(TicketStatus.slug == status))
    if priority:
        query = query.filter(Ticket.priority.has(Priority.slug == priority))
    if department:
        query = query.filter(Ticket.department_id == department)
    if assignee:
        query = query.filter(Ticket.assigned_user_id == assignee)

    # Sorting uses aliased joins so it can never collide with the EXISTS filters above.
    if sort == 'priority':
        a = aliased(Priority)
        query, col = query.join(a, Ticket.priority_id == a.id), a.sort_order
    elif sort == 'status':
        a = aliased(TicketStatus)
        query, col = query.join(a, Ticket.status_id == a.id), a.sort_order
    elif sort == 'customer':
        a = aliased(Customer)
        query, col = query.join(a, Ticket.customer_id == a.id), func.lower(a.first_name + ' ' + func.coalesce(a.last_name, ''))
    elif sort == 'department':
        a = aliased(Department)
        query, col = query.outerjoin(a, Ticket.department_id == a.id), func.lower(a.name)
    elif sort == 'assignee':
        a = aliased(User)
        query, col = query.outerjoin(a, Ticket.assigned_user_id == a.id), func.lower(a.name)
    elif sort == 'due':
        col = Ticket.due_at
    elif sort == 'ticket':
        col = Ticket.id
    else:
        col = Ticket.updated_at
    query = query.order_by(col.asc() if direction == 'asc' else col.desc(), Ticket.id.desc())
    return query, {'view': view, 'sort': sort, 'dir': direction, 'q': q}


@main.route('/tickets')
def tickets():
    r = guard('ticket.view')
    if r:
        return r
    query, state = build_ticket_query(request.args)
    page = paginate(query.options(*TICKET_LOAD))
    return render_template('tickets.html', page=page, statuses=active_statuses(),
                           priorities=Priority.query.order_by(Priority.sort_order).all(),
                           departments=active_departments(), users=active_users(),
                           views=TICKET_VIEWS, can_edit=allowed('ticket.edit'),
                           saved_views=SavedView.query.filter(or_(SavedView.user_id == current_user().id,
                                                                  SavedView.is_shared.is_(True)))
                                                      .order_by(SavedView.name).all(), **state)


@main.route('/tickets/export')
def tickets_export():
    r = guard('ticket.view')
    if r:
        return r
    query, _ = build_ticket_query(request.args)
    rows = [ticket_row(t) for t in query.options(*TICKET_LOAD).all()]
    audit('TICKETS_EXPORTED', 'Ticket', None, f'{len(rows)} rows')
    db.session.commit()
    return csv_response('baidesk-tickets.csv', [(None, TICKET_HEADERS, rows)])


@main.route('/tickets/bulk', methods=['POST'])
def tickets_bulk():
    r = guard('ticket.edit')
    if r:
        return r
    back = safe_next(request.form.get('next'), url_for('main.tickets'))
    ids = {int(i) for i in request.form.getlist('ticket_ids') if i.isdigit()}
    action = request.form.get('action', '')
    value = request.form.get('value_' + action, '')
    if not ids:
        flash('Select at least one ticket first.', 'error')
        return redirect(back)
    if action not in ('assign', 'auto', 'status', 'priority'):
        flash('Choose an action to apply.', 'error')
        return redirect(back)
    u = current_user()
    target = None
    if action == 'status':
        target = get_or_404(TicketStatus, int(value)) if value.isdigit() else abort(400)
    elif action == 'priority':
        target = get_or_404(Priority, int(value)) if value.isdigit() else abort(400)
    elif value and not value.isdigit():
        abort(400)
    done = skipped = 0
    for tid in sorted(ids):
        t = db.session.get(Ticket, tid)
        if not t or not user_can_access_ticket(t):
            skipped += 1
            continue
        if action == 'auto':
            if t.assigned_user_id:
                skipped += 1
                continue
            agent, why = routing.pick(routing.department_for(t.department_id, t.category_id), t.category_id)
            if not agent:
                skipped += 1
                continue
            changed = routing.assign(t, agent, why, actor_id=u.id)
        elif action == 'assign':
            new_id = int(value) if value else None
            if new_id and assignee_error(new_id, t.department_id, u):
                skipped += 1
                continue
            changed = change_assignee(t, new_id, u)
        elif action == 'status':
            changed = change_status(t, target, u)
        else:
            changed = change_priority(t, target, u)
        done += 1 if changed else 0
    audit('TICKETS_BULK_' + action.upper(), 'Ticket', None, f'{done} changed, {skipped} skipped')
    db.session.commit()
    msg = f'Updated {done} ticket{"s" if done != 1 else ""}.'
    if skipped:
        msg += f' {skipped} skipped (no access, or the assignee is not in the ticket\'s department).'
    flash(msg, 'success' if done else 'error')
    return redirect(back)


def _ticket_form_context(form, customer_id=None):
    return dict(customers=Customer.query.order_by(Customer.first_name, Customer.last_name).all(),
                statuses=active_statuses(), priorities=Priority.query.order_by(Priority.sort_order).all(),
                categories=Category.query.filter_by(is_active=True).order_by(Category.name).all(),
                departments=active_departments(), users=active_users(), form=form, preselect=customer_id,
                duplicates=[])


@main.route('/tickets/new', methods=['GET', 'POST'])
def ticket_new():
    r = guard('ticket.create')
    if r:
        return r
    me = current_user()
    if request.method == 'POST':
        subject = request.form.get('subject', '').strip()
        description = request.form.get('description', '').strip()
        c = get_or_404(Customer, request.form.get('customer_id', type=int) or 0)
        p = get_or_404(Priority, request.form.get('priority_id', type=int) or 0)
        s = get_or_404(TicketStatus, request.form.get('status_id', type=int) or 0)
        dept_id = request.form.get('department_id', type=int)
        assignee = request.form.get('assigned_user_id', type=int)
        category_id = request.form.get('category_id', type=int)
        error = None
        if not subject or not description:
            error = 'Subject and description are required.'
        elif assignee:
            error = assignee_error(assignee, dept_id, me)
        if error:
            flash(error, 'error')
            return render_template('ticket_form.html', **_ticket_form_context(request.form.to_dict(),
                                   c.id))
        if not request.form.get('confirm_duplicate'):
            dups = quality.find_duplicates(c.id, subject)
            if dups:
                flash('This customer already has a similar open ticket. Open it instead, or create a new one anyway.',
                      'info')
                ctx = _ticket_form_context(request.form.to_dict(), c.id)
                ctx['duplicates'] = dups
                return render_template('ticket_form.html', **ctx)
        auto_agent = auto_reason = None
        if not assignee and routing.auto_assign_enabled():
            # department + need: use the chosen department, else the category's home department
            pool_dept = routing.department_for(dept_id, category_id)
            auto_agent, auto_reason = routing.pick(pool_dept, category_id)
            if auto_agent:
                assignee, dept_id = auto_agent.id, dept_id or pool_dept
        t = Ticket(ticket_number=next_ticket_number(), customer_id=c.id, subject=subject, description=description,
                   category_id=category_id, priority_id=p.id, status_id=s.id, department_id=dept_id,
                   assigned_user_id=assignee, created_by=me.id, created_at=utcnow())
        db.session.add(t)
        db.session.flush()
        sla.apply_deadlines(t)
        sla.on_status_change(t, s.slug, t.created_at)
        try:
            att = save_upload(request.files.get('attachment'), me.id)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'error')
            return render_template('ticket_form.html', **_ticket_form_context(request.form.to_dict(), c.id))
        if att:
            db.session.add(AttachmentLink(attachment_id=att.id, ticket_id=t.id, customer_id=c.id))
        if dept_id:
            db.session.add(DepartmentTime(ticket_id=t.id, department_id=dept_id))
        db.session.add(TicketHistory(ticket_id=t.id, user_id=me.id, action_type='TICKET_CREATED',
                                     new_value=t.ticket_number))
        if auto_agent:
            db.session.add(TicketHistory(ticket_id=t.id, user_id=me.id, action_type='AUTO_ASSIGNED',
                                         field_name='assigned_user_id', old_value='Unassigned',
                                         new_value=auto_agent.name, reason=auto_reason))
        log_activity('TICKET_CREATED', f'{t.ticket_number} created.', customer_id=c.id, ticket_id=t.id,
                     entity_type='Ticket', entity_id=t.id, department_id=t.department_id)
        if t.assigned_user_id and t.assigned_user_id != me.id:
            notify(t.assigned_user_id, 'assignment', 'Ticket assigned', f'{t.ticket_number} was assigned to you.',
                   'Ticket', t.id)
        db.session.commit()
        flash(f'{t.ticket_number} created.', 'success')
        return redirect_ticket(t)
    return render_template('ticket_form.html', **_ticket_form_context({}, request.args.get('customer_id', type=int)))


@main.route('/tickets/<int:ticket_id>')
def ticket_detail(ticket_id):
    r = guard('ticket.view')
    if r:
        return r
    t = get_or_404(Ticket, ticket_id)
    if not user_can_access_ticket(t):
        abort(403)
    comments = TicketComment.query.filter_by(ticket_id=t.id).order_by(TicketComment.created_at.asc()).all()
    history = TicketHistory.query.filter_by(ticket_id=t.id).order_by(TicketHistory.created_at.desc(), TicketHistory.id.desc()).all()
    escalations = Escalation.query.filter_by(ticket_id=t.id).order_by(Escalation.created_at.desc()).all()
    attachment_rows = AttachmentLink.query.filter_by(ticket_id=t.id).all()
    dept_names = {d.id: d.name for d in Department.query.all()}
    contacts = Interaction.query.filter_by(ticket_id=t.id).all()
    # one stream for replies, notes and logged calls / SMS / WhatsApp, oldest first
    events = [('event', h, h.created_at) for h in history if h.action_type != 'TICKET_CREATED']
    convo = sorted([('comment', c, c.created_at) for c in comments] +
                   [('contact', x, x.interaction_at) for x in contacts] + events, key=lambda row: row[2])
    return render_template('ticket_detail.html', ticket=t, comments=comments, history=history, convo=convo,
                           ctx=quality.customer_context(t), today_s=to_local(utcnow()).strftime('%Y-%m-%d'),
                           yesterday_s=to_local(utcnow() - timedelta(days=1)).strftime('%Y-%m-%d'), channels=CHANNELS, directions=DIRECTIONS,
                           dispositions=Disposition.query.filter_by(is_active=True).order_by(Disposition.name).all(),
                           can_auto_assign=not t.assigned_user_id,
                           escalations=escalations, attachments=attachment_rows, dept_names=dept_names,
                           departments=active_departments(), users=active_users(), statuses=active_statuses(),
                           priorities=Priority.query.order_by(Priority.sort_order).all(),
                           canned=CannedResponse.query.filter_by(is_active=True).order_by(CannedResponse.title).all(),
                           is_running=bool(t.status and t.status.slug in ACTIVE_SLUGS))


@main.route('/tickets/<int:ticket_id>/edit', methods=['GET', 'POST'])
def ticket_edit(ticket_id):
    r = guard('ticket.edit')
    if r:
        return r
    t = get_or_404(Ticket, ticket_id)
    if not user_can_access_ticket(t):
        abort(403)
    categories = Category.query.filter_by(is_active=True).order_by(Category.name).all()
    if request.method == 'POST':
        subject = request.form.get('subject', '').strip()
        description = request.form.get('description', '').strip()
        category_id = request.form.get('category_id', type=int)
        if not subject or not description:
            flash('Subject and description are required.', 'error')
            return render_template('ticket_edit.html', ticket=t, categories=categories, form=request.form.to_dict())
        u = current_user()
        changed = []
        if subject != t.subject:
            changed.append(('subject', t.subject, subject))
            t.subject = subject
        if description != t.description:
            changed.append(('description', '(edited)', '(edited)'))
            t.description = description
        if category_id != t.category_id:
            old = t.category.name if t.category else '—'
            cat = get_or_404(Category, category_id) if category_id else None
            changed.append(('category', old, cat.name if cat else '—'))
            t.category_id, t.category = category_id, cat
        for field, old, new in changed:
            db.session.add(TicketHistory(ticket_id=t.id, user_id=u.id, action_type='FIELD_CHANGED',
                                         field_name=field, old_value=old, new_value=new))
        if changed:
            log_activity('TICKET_EDITED', f'{t.ticket_number} details edited ({", ".join(c[0] for c in changed)}).',
                         customer_id=t.customer_id, ticket_id=t.id, department_id=t.department_id,
                         entity_type='Ticket', entity_id=t.id)
            t.updated_at = utcnow()
        db.session.commit()
        flash('Ticket updated.' if changed else 'No changes to save.', 'success' if changed else 'info')
        return redirect_ticket(t)
    return render_template('ticket_edit.html', ticket=t, categories=categories,
                           form={'subject': t.subject, 'description': t.description,
                                 'category_id': str(t.category_id or '')})


@main.route('/tickets/<int:ticket_id>/update', methods=['POST'])
def ticket_update(ticket_id):
    r = guard('ticket.edit')
    if r:
        return r
    t = get_or_404(Ticket, ticket_id)
    if not user_can_access_ticket(t):
        abort(403)
    u = current_user()
    status_id = request.form.get('status_id', type=int)
    priority_id = request.form.get('priority_id', type=int)
    new_dept = request.form.get('department_id', type=int) if 'department_id' in request.form else t.department_id
    new_assignee = (request.form.get('assigned_user_id', type=int) if 'assigned_user_id' in request.form
                    else t.assigned_user_id)
    status = get_or_404(TicketStatus, status_id) if status_id else None
    prio = get_or_404(Priority, priority_id) if priority_id else None
    if new_dept:
        get_or_404(Department, new_dept)
    # Only validate the agent/department pairing when one of them is actually changing.
    if new_assignee and (new_assignee != t.assigned_user_id or new_dept != t.department_id):
        err = assignee_error(new_assignee, new_dept, u)
        if err:
            flash(err, 'error')
            return redirect(url_for('main.ticket_detail', ticket_id=t.id))
    go_next = request.form.get('resolve_next') == '1'
    if go_next:
        status = TicketStatus.query.filter_by(slug='resolved').first() or status
    changed = False
    if status:
        changed |= change_status(t, status, u)
    if prio:
        changed |= change_priority(t, prio, u)
    changed |= change_department(t, new_dept, u)
    changed |= change_assignee(t, new_assignee, u)
    if changed:
        t.updated_at = utcnow()
    db.session.commit()
    routing.drain_queue()
    flash('Ticket updated.' if changed else 'No changes to save.', 'success' if changed else 'info')
    if go_next:
        nxt = quality.next_ticket_for(u, exclude_id=t.id)
        if nxt:
            flash(f'Up next: {nxt.ticket_number}.', 'info')
            return redirect(url_for('main.ticket_detail', ticket_id=nxt.id))
        flash('Your queue is clear.', 'success')
        return redirect(url_for('main.tickets', view='mine'))
    return redirect_ticket(t)


@main.route('/tickets/<int:ticket_id>/comment', methods=['POST'])
def ticket_comment(ticket_id):
    r = guard('ticket.comment')
    if r:
        return r
    t = get_or_404(Ticket, ticket_id)
    if not user_can_access_ticket(t):
        abort(403)
    me = current_user()
    body = request.form.get('body', '').strip()
    typ = request.form.get('comment_type', 'PUBLIC')
    if typ not in ('PUBLIC', 'INTERNAL', 'CUSTOMER'):
        abort(400)
    if not body:
        flash('Comment cannot be empty.', 'error')
        return redirect(url_for('main.ticket_detail', ticket_id=t.id))
    c = TicketComment(ticket_id=t.id, user_id=me.id, comment_type=typ, body=body)
    db.session.add(c)
    db.session.flush()
    uploaded = request.files.get('attachment')
    try:
        att = save_upload(uploaded, me.id) if uploaded and uploaded.filename else None
    except ValueError as exc:
        flash(str(exc), 'error')
        db.session.rollback()
        return redirect(url_for('main.ticket_detail', ticket_id=t.id))
    if att:
        db.session.add(AttachmentLink(attachment_id=att.id, ticket_id=t.id, comment_id=c.id, customer_id=t.customer_id))
    for name in set(re.findall(r'@([A-Za-z][A-Za-z0-9_.-]*)', body)):
        user = User.query.filter(User.is_active.is_(True), func.lower(User.name).like(name.lower() + '%')).first()
        if user and user.id != me.id:
            db.session.add(CommentMention(comment_id=c.id, mentioned_user_id=user.id))
            notify(user.id, 'mention', 'You were mentioned', f'{me.name} mentioned you on {t.ticket_number}.',
                   'Ticket', t.id)
    if typ == 'PUBLIC' and not t.first_response_at:
        t.first_response_at = utcnow()
    reopened = False
    if typ == 'CUSTOMER':
        # The customer wrote back: bring a finished / waiting ticket back to the queue and restart its clock.
        if t.status and t.status.slug in ('resolved', 'closed', 'pending'):
            open_status = TicketStatus.query.filter_by(slug='open').first()
            reopened = bool(open_status) and change_status(t, open_status, me)
        if t.assigned_user_id and t.assigned_user_id != me.id:
            notify(t.assigned_user_id, 'reply', 'Customer replied',
                   f'{t.ticket_number}: the customer replied' + (' and the ticket was reopened.' if reopened else '.'),
                   'Ticket', t.id)
    t.updated_at = utcnow()
    log_activity('INTERNAL_NOTE_ADDED' if typ == 'INTERNAL' else 'CUSTOMER_REPLIED' if typ == 'CUSTOMER' else 'COMMENT_ADDED',
                 f'{typ.title()} comment added by {me.name}.', customer_id=t.customer_id,
                 ticket_id=t.id, entity_type='Comment', entity_id=c.id, department_id=t.department_id)
    db.session.commit()
    return redirect(url_for('main.ticket_detail', ticket_id=t.id) + '#conversation')


@main.route('/tickets/<int:ticket_id>/escalate', methods=['POST'])
def escalate(ticket_id):
    r = guard('ticket.escalate')
    if r:
        return r
    t = get_or_404(Ticket, ticket_id)
    if not user_can_access_ticket(t):
        abort(403)
    me = current_user()
    to = request.form.get('to_department_id', type=int)
    reason = request.form.get('reason', '').strip()
    if not to or not reason:
        flash('Receiving department and escalation reason are mandatory.', 'error')
        return redirect(url_for('main.ticket_detail', ticket_id=t.id))
    to_dept = get_or_404(Department, to)
    if to == t.department_id:
        flash('Choose a different department for escalation.', 'error')
        return redirect(url_for('main.ticket_detail', ticket_id=t.id))
    from_dept = t.department_id
    e = Escalation(ticket_id=t.id, from_department_id=from_dept, to_department_id=to,
                   initiated_by=me.id, reason=reason)
    db.session.add(e)
    change_department(t, to, me, record=False)
    esc = TicketStatus.query.filter_by(slug='escalated').first()
    if esc:
        t.status_id, t.status = esc.id, esc
    t.updated_at = utcnow()
    db.session.flush()
    db.session.add(TicketHistory(ticket_id=t.id, user_id=me.id, action_type='ESCALATED', reason=reason,
                                 new_value=to_dept.name))
    log_activity('ESCALATED', f'Escalated to {to_dept.name}: {reason}', customer_id=t.customer_id,
                 ticket_id=t.id, department_id=to, entity_type='Escalation', entity_id=e.id)
    for user in User.query.filter(User.is_active.is_(True), User.departments.any(Department.id == to)).all():
        notify(user.id, 'escalation', 'Ticket escalated', f'{t.ticket_number} was escalated to {to_dept.name}.',
               'Ticket', t.id)
    db.session.commit()
    flash(f'Escalated to {to_dept.name}.', 'success')
    return redirect_ticket(t, '#escalations')


@main.route('/escalations/<int:eid>/<action>', methods=['POST'])
def escalation_action(eid, action):
    r = guard('ticket.escalate')
    if r:
        return r
    e = get_or_404(Escalation, eid)
    t = get_or_404(Ticket, e.ticket_id)
    if not user_can_access_ticket(t):
        abort(403)
    now = utcnow()
    u = current_user()
    past = {'accept': 'accepted', 'return': 'returned', 'resolve': 'resolved'}
    if action not in past:
        abort(400)
    required = {'accept': 'Pending', 'return': 'Pending', 'resolve': 'Accepted'}[action]
    if e.status != required:
        flash(f'This escalation is {e.status.lower()} and cannot be {past[action]}.', 'error')
        return redirect(url_for('main.ticket_detail', ticket_id=t.id) + '#escalations')
    in_progress = TicketStatus.query.filter_by(slug='in-progress').first()
    if action == 'accept':
        e.status, e.accepted_by, e.accepted_at = 'Accepted', u.id, now
        if in_progress and t.status and t.status.slug == 'escalated':
            change_status(t, in_progress, u)
    elif action == 'return':
        e.status, e.returned_by, e.returned_at = 'Returned', u.id, now
        # Hand the ticket back to the department that escalated it.
        if e.from_department_id and e.from_department_id != t.department_id:
            change_department(t, e.from_department_id, u)
        if in_progress and t.status and t.status.slug == 'escalated':
            change_status(t, in_progress, u)
    else:
        e.status, e.resolved_at = 'Resolved', now
    t.updated_at = now
    log_activity('ESCALATION_' + action.upper(), f'Escalation {past[action]} by {u.name}.',
                 customer_id=t.customer_id, ticket_id=t.id, department_id=t.department_id,
                 entity_type='Escalation', entity_id=e.id)
    db.session.commit()
    flash(f'Escalation {past[action]}.', 'success')
    return redirect_ticket(t, '#escalations')


# ───────────────────────── notifications & APIs ─────────────────────────
@main.route('/notifications')
def notifications():
    r = require_login()
    if r:
        return r
    page = paginate(Notification.query.filter_by(user_id=current_user().id)
                    .order_by(Notification.created_at.desc(), Notification.id.desc()))
    return render_template('notifications.html', page=page)


@main.route('/notifications/read/<int:nid>', methods=['POST'])
def notification_read(nid):
    r = require_login()
    if r:
        return r
    n = get_or_404(Notification, nid)
    if n.user_id != current_user().id:
        abort(403)
    n.is_read = True
    n.read_at = utcnow()
    db.session.commit()
    return redirect(safe_next(request.referrer and urlparse(request.referrer).path, url_for('main.notifications')))


@main.route('/notifications/read-all', methods=['POST'])
def notifications_read_all():
    r = require_login()
    if r:
        return r
    Notification.query.filter_by(user_id=current_user().id, is_read=False).update({'is_read': True, 'read_at': utcnow()})
    db.session.commit()
    return redirect(url_for('main.notifications'))


@main.route('/api/notifications/unread')
def unread_api():
    r = require_login()
    if r:
        return jsonify(error='unauthorized'), 401
    sla.watch()      # throttled; raises SLA / promise alerts while anyone has the app open
    return jsonify(count=Notification.query.filter_by(user_id=current_user().id, is_read=False).count())


@main.route('/api/timeline/<int:customer_id>')
def timeline_api(customer_id):
    r = guard('customer.view')
    if r:
        return jsonify(error='unauthorized'), 401
    get_or_404(Customer, customer_id)
    query = ActivityLog.query.filter_by(customer_id=customer_id)
    activity_type = request.args.get('type', '').strip()
    agent_id = request.args.get('agent_id', type=int)
    direction = request.args.get('direction', 'newest')
    if activity_type:
        query = query.filter(ActivityLog.activity_type == activity_type)
    if agent_id:
        query = query.filter(ActivityLog.actor_user_id == agent_id)
    order = ActivityLog.created_at.desc() if direction != 'oldest' else ActivityLog.created_at.asc()
    rows = query.order_by(order, ActivityLog.id.desc() if direction != 'oldest' else ActivityLog.id.asc()).limit(500).all()
    return jsonify([{'type': x.activity_type, 'description': x.description, 'timestamp': iso_utc(x.created_at),
                     'display': localdt(x.created_at), 'actor': x.actor.name if x.actor else 'System'} for x in rows])


@main.route('/uploads/<path:filename>')
def uploaded_file(filename):
    r = require_login()
    if r:
        return r
    att = Attachment.query.filter_by(stored_filename=filename).first() or abort(404)
    u = current_user()
    if not u.is_manager() and att.uploaded_by != u.id:
        ok = False
        for link in AttachmentLink.query.filter_by(attachment_id=att.id).all():
            if link.ticket_id:
                tk = db.session.get(Ticket, link.ticket_id)
                ok = ok or (tk is not None and user_can_access_ticket(tk))
            elif link.customer_id:
                ok = ok or u.has_permission('customer.view')
        if not ok:
            abort(403)
    return send_from_directory(current_app.config['UPLOAD_FOLDER'], filename, download_name=att.filename)


# Admin and report routes live in their own modules but share this blueprint.
from . import routes_admin, routes_reports  # noqa: E402,F401
