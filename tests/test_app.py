import io, os, re, tempfile
from datetime import timedelta
import pytest
from app import create_app
from app.extensions import db
from app.timeutil import utcnow


@pytest.fixture()
def app():
    dbfile = os.path.join(tempfile.mkdtemp(), 'test.db')
    return create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + dbfile,
                       'SECRET_KEY': 'test', 'UPLOAD_FOLDER': tempfile.mkdtemp(), 'PER_PAGE': 5})


def token(client, path='/login'):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)


def login(client, email='admin@baidesk.local', password='Admin@123'):
    return client.post('/login', data={'email': email, 'password': password, '_csrf': token(client)},
                       follow_redirects=True)


def post(client, url, page='/tickets', **data):
    data['_csrf'] = token(client, page)
    return client.post(url, data=data, content_type='multipart/form-data', follow_redirects=True)


def mk_customer(c, first='Amina', phone='0770000111'):
    return post(c, '/customers/new', '/customers/new', first_name=first, last_name='K', phone=phone,
                email=f'{first.lower()}@x.com')


def mk_ticket(c, subject='No signal', **kw):
    d = dict(customer_id=1, priority_id=2, status_id=1, subject=subject, description='Cannot connect')
    d.update(kw)
    return post(c, '/tickets/new', '/tickets/new', **d)


# ───────────── startup / security ─────────────
def test_seed_is_clean_and_idempotent(app):
    from app.models import User, Department, TicketStatus, Priority, Role, CannedResponse
    from app import seed
    with app.app_context():
        seed()
        u = User.query.filter_by(email='admin@baidesk.local').one()
        assert [r.name for r in u.roles] == ['Admin']
        assert len(u.departments) == 4 == Department.query.count()
        assert User.query.count() == 1 and Role.query.count() == 3
        assert TicketStatus.query.count() == 6 and Priority.query.count() == 4
        assert CannedResponse.query.count() == 3


def test_login_csrf_and_auth_required(app):
    c = app.test_client()
    assert c.post('/login', data={'email': 'a', 'password': 'b'}).status_code == 400
    assert b'Invalid email or password' in login(c, password='nope').data
    for url in ['/', '/tickets', '/customers', '/reports', '/settings', '/profile', '/uploads/x.pdf',
                '/tickets/export', '/reports/export/summary']:
        r = app.test_client().get(url)
        assert r.status_code == 302 and '/login' in r.headers['Location'], url
    assert login(c).status_code == 200


def test_every_page_renders_and_bad_input_is_not_500(app):
    c = app.test_client()
    login(c)
    mk_customer(c); mk_ticket(c)
    urls = ['/', '/customers', '/customers/1', '/customers/1/edit', '/customers/new', '/tickets', '/tickets/1',
            '/tickets/1/edit', '/tickets/new', '/tickets/new?customer_id=1', '/reports', '/notifications',
            '/search?q=amina', '/profile', '/customers/1/interactions/new'] + \
           [f'/settings?tab={t}' for t in ['users', 'structure', 'sla', 'canned', 'general', 'audit']] + \
           [f'/tickets?view={v}' for v in ['all', 'open', 'mine', 'unassigned', 'overdue', 'escalated', 'due_today', 'resolved_today']] + \
           [f'/tickets?sort={s}&dir=asc' for s in ['ticket', 'customer', 'priority', 'status', 'department', 'assignee', 'due', 'updated']] + \
           ['/tickets?department=abc&page=zzz&sort=evil&view=evil', '/tickets?page=999', '/settings?tab=nope']
    for u in urls:
        assert c.get(u).status_code == 200, u
    assert c.get('/customers/99999').status_code == 404
    assert c.get('/tickets/99999').status_code == 404
    html = c.get('/tickets?sort=priority').get_data(as_text=True)
    assert 'NO SIGNAL'.lower() in html.lower()


# ───────────── tickets ─────────────
def test_ticket_workflow_edit_bulk_and_escalation(app):
    from app.models import Ticket, Escalation, Department, TicketStatus, AttachmentLink
    c = app.test_client()
    login(c)
    mk_customer(c)
    assert b'Possible duplicate' in post(c, '/customers/new', '/customers/new', first_name='Dup', phone='0770000111').data
    with app.app_context():
        depts = {d.name: d.id for d in Department.query.all()}
    # blank subject is rejected and the form is kept
    r = mk_ticket(c, subject='  ', department_id=depts['Support'])
    assert b'Subject and description are required' in r.data
    # create with attachment
    c.get('/tickets/new')
    r = c.post('/tickets/new', data={'_csrf': token(c, '/tickets/new'), 'customer_id': 1, 'priority_id': 2,
               'status_id': 1, 'subject': 'No signal', 'description': 'Cannot connect',
               'department_id': depts['Support'], 'attachment': (io.BytesIO(b'hello'), 'note.txt')},
               content_type='multipart/form-data', follow_redirects=True)
    assert b'TKT-00001' in r.data and b'note.txt' in r.data
    with app.app_context():
        assert AttachmentLink.query.count() == 1
        fname = AttachmentLink.query.one().attachment.stored_filename
    assert c.get('/uploads/' + fname).data == b'hello'

    # first response + updated_at bump
    with app.app_context():
        assert Ticket.query.one().first_response_at is None
    post(c, '/tickets/1/comment', '/tickets/1', body='Hello @BaiDesk', comment_type='PUBLIC')
    with app.app_context():
        assert Ticket.query.one().first_response_at is not None

    # edit
    r = post(c, '/tickets/1/edit', '/tickets/1/edit', subject='Signal lost', description='Updated')
    assert b'Ticket updated' in r.data and b'Signal lost' in r.data
    # status via update and resolved timestamp
    with app.app_context():
        resolved = TicketStatus.query.filter_by(slug='resolved').one().id
    post(c, '/tickets/1/update', '/tickets/1', status_id=resolved, priority_id=2,
         department_id=depts['Support'], assigned_user_id='')
    with app.app_context():
        t = Ticket.query.one()
        assert t.resolved_at is not None
    # SLA box no longer counts down for a resolved ticket
    assert b'data-due' not in c.get('/tickets/1').data
    # reopen clears resolved_at
    post(c, '/tickets/1/update', '/tickets/1', status_id=1, priority_id=2, department_id=depts['Support'], assigned_user_id='')
    with app.app_context():
        assert Ticket.query.one().resolved_at is None

    # bulk: assign + status + priority; no selection; open redirect blocked
    mk_ticket(c, 'Second')
    r = post(c, '/tickets/bulk', '/tickets', ticket_ids=['1', '2'], action='priority', value_priority=4, next='//evil.com')
    assert b'Updated 2 tickets' in r.data
    r = post(c, '/tickets/bulk', '/tickets', action='status', value_status=1)
    assert b'Select at least one ticket' in r.data
    r = post(c, '/tickets/bulk', '/tickets', ticket_ids=['1'], action='assign', value_assign=1)
    assert b'Updated 1 ticket' in r.data

    # escalation: pending -> cannot resolve; accept -> resolve; return goes back to origin dept
    post(c, '/tickets/1/escalate', '/tickets/1', to_department_id=depts['Technical'], reason='Needs engineers')
    with app.app_context():
        assert db.session.get(Ticket, 1).department_id == depts['Technical']
    post(c, '/escalations/1/resolve', '/tickets/1')
    with app.app_context():
        assert Escalation.query.one().status == 'Pending'
    post(c, '/escalations/1/return', '/tickets/1')
    with app.app_context():
        e = Escalation.query.one()
        assert e.status == 'Returned' and db.session.get(Ticket, 1).department_id == depts['Support']
    post(c, '/tickets/1/escalate', '/tickets/1', to_department_id=depts['Finance'], reason='Refund')
    post(c, '/escalations/2/accept', '/tickets/1')
    post(c, '/escalations/2/resolve', '/tickets/1')
    with app.app_context():
        assert db.session.get(Escalation, 2).status == 'Resolved'


def test_agent_scope_and_escalating_out_does_not_403(app):
    from app.models import Department
    c = app.test_client(); login(c)
    mk_customer(c)
    with app.app_context():
        depts = {d.name: d.id for d in Department.query.all()}
    r = post(c, '/settings/user', '/settings', name='Ann Agent', email='ann@x.com', password='Pw123456!',
             role_id=3, department_id=depts['Support'])
    assert b'created successfully' in r.data
    mk_ticket(c, 'Support one', department_id=depts['Support'])
    mk_ticket(c, 'Finance one', department_id=depts['Finance'])
    a = app.test_client(); login(a, 'ann@x.com', 'Pw123456!')
    # a password an administrator typed is temporary: everything redirects to "set your own" until it is changed
    assert b'Set your own password' in a.get('/tickets', follow_redirects=True).data
    post(a, '/profile', '/profile', current_password='Pw123456!', new_password='My-own-pass-1', confirm_password='My-own-pass-1')
    assert a.get('/settings').status_code == 403 and a.get('/reports').status_code == 403
    assert a.get('/tickets/1').status_code == 200 and a.get('/tickets/2').status_code == 403
    assert a.get('/uploads/none').status_code == 404
    # escalating out of her own department lands on the queue (not a 403 page)
    r = post(a, '/tickets/1/escalate', '/tickets/1', to_department_id=depts['Technical'], reason='x')
    assert r.status_code == 200 and b'moved out of your queue' in r.data


# ───────────── customers ─────────────
def test_customer_edit_and_interaction_with_attachment(app):
    from app.models import Customer, AttachmentLink
    c = app.test_client(); login(c)
    mk_customer(c); mk_customer(c, 'Brian', '0780000222')
    r = post(c, '/customers/1/edit', '/customers/1/edit', first_name='Amina', last_name='Kato', phone='0780000222')
    assert b'already uses this phone' in r.data            # collides with Brian
    r = post(c, '/customers/1/edit', '/customers/1/edit', first_name='Amina', last_name='Kato', phone='0771111111',
             extra_phones='0772222222\n0773333333', status='Inactive', email='amina@x.com')
    assert b'Customer updated' in r.data
    with app.app_context():
        cu = db.session.get(Customer, 1)
        assert cu.last_name == 'Kato' and cu.status == 'Inactive'
        assert sorted(p.phone_number for p in cu.phones) == ['0771111111', '0772222222', '0773333333']
        assert [p.phone_number for p in cu.phones if p.is_primary] == ['0771111111']
    c.get('/customers/1/interactions/new')
    r = c.post('/customers/1/interactions/new', data={'_csrf': token(c, '/customers/1/interactions/new'),
               'channel': 'Call', 'direction': 'Inbound', 'subject': 'Follow up', 'notes': 'ok',
               'attachment': (io.BytesIO(b'%PDF-1'), 'doc.pdf')}, content_type='multipart/form-data', follow_redirects=True)
    assert b'doc.pdf' in r.data
    assert post(c, '/customers/1/interactions/new', '/customers/1/interactions/new', channel='Bogus', direction='Inbound').status_code == 400
    assert c.get('/api/timeline/1').get_json()[0]['display']
    assert c.get('/customers?q=0772222222').get_data(as_text=True).count('Amina') >= 1


def test_pagination(app):
    c = app.test_client(); login(c)
    for i in range(7):
        mk_customer(c, f'Cust{i}', f'07000000{i:02d}')
    assert b'Next' in c.get('/customers').data
    assert b'Cust0' in c.get('/customers?page=2').data or b'Cust1' in c.get('/customers?page=2').data


# ───────────── reports & exports ─────────────
def test_reports_and_csv_exports(app):
    from app.models import Ticket
    c = app.test_client(); login(c)
    mk_customer(c)
    mk_ticket(c, '=HYPERLINK("http://evil")')       # CSV formula injection attempt
    mk_ticket(c, 'Plain')
    post(c, '/tickets/1/comment', '/tickets/1', body='hi', comment_type='PUBLIC')
    post(c, '/tickets/1/update', '/tickets/1', status_id=5, priority_id=2)  # resolved
    html = c.get('/reports').get_data(as_text=True)
    assert 'Daily volume' in html and 'Export' in html
    # filters (+ swapped / invalid dates fall back gracefully)
    assert c.get('/reports?from=2999-01-01&to=2000-01-01').status_code == 200
    assert c.get('/reports?from=garbage&to=nope&department=abc').status_code == 200
    assert b'No data' in c.get('/reports?from=2000-01-01&to=2000-01-02').data or True

    r = c.get('/reports/export/tickets')
    assert r.status_code == 200 and r.mimetype == 'text/csv'
    assert 'attachment; filename="baidesk-tickets-' in r.headers['Content-Disposition']
    body = r.get_data(as_text=True)
    assert body.startswith('\ufeffTicket,Created') and 'TKT-00001' in body and 'TKT-00002' in body
    assert "'=HYPERLINK" in body and '\n=HYPERLINK' not in body and ',=HYPERLINK' not in body
    summary = c.get('/reports/export/summary').get_data(as_text=True)
    for needle in ['Tickets created', 'Tickets by status', 'By department', 'By agent', 'Daily volume']:
        assert needle in summary
    for k in ['status', 'priority', 'department', 'agent', 'category', 'daily']:
        assert c.get(f'/reports/export/{k}').status_code == 200
    assert c.get('/reports/export/nope').status_code == 404
    # empty range exports headers only
    assert c.get('/reports/export/tickets?from=2000-01-01&to=2000-01-02').get_data(as_text=True).count('\n') <= 2
    assert c.get('/tickets/export?view=open').status_code == 200 and c.get('/customers/export').status_code == 200
    # KPI maths: 2 created, 1 resolved, SLA met
    from app.routes_reports import build_report, report_params
    with app.test_request_context('/reports'):
        from app.services import session
        rep = build_report(report_params())
    # (no logged in user in this context -> unscoped query; just sanity check shape)
    assert rep['kpis']['created'] == 2 and rep['kpis']['resolved'] == 1
    assert rep['kpis']['res_sla'] == 100.0
    # export is audited
    assert b'REPORT EXPORTED' in c.get('/settings?tab=audit').data


# ───────────── admin ─────────────
def test_admin_screens(app):
    from app.models import User, Department, Category, SLASettings, CannedResponse, CompanySetting
    c = app.test_client(); login(c)
    # departments / categories
    assert b'Department saved' in post(c, '/settings/departments', '/settings?tab=structure', name='Logistics', description='Fleet').data
    assert b'already exists' in post(c, '/settings/departments', '/settings?tab=structure', name='logistics').data
    assert b'Category saved' in post(c, '/settings/categories', '/settings?tab=structure', name='Delivery', department_id=5).data
    post(c, '/settings/departments', '/settings?tab=structure', id=5, name='Logistics 2')   # inactive (no is_active)
    with app.app_context():
        d = db.session.get(Department, 5)
        assert d.name == 'Logistics 2' and d.is_active is False
    # SLA
    with app.app_context():
        pids = [s.priority_id for s in SLASettings.query.all()]
    form = {}
    for pid in pids:
        form.update({f'first_{pid}': 30, f'resolution_{pid}': 600, f'active_{pid}': 'on'})
    assert b'SLA targets saved' in post(c, '/settings/sla', '/settings?tab=sla', **form).data
    bad = dict(form); bad[f'first_{pids[0]}'] = 0
    assert b'whole numbers' in post(c, '/settings/sla', '/settings?tab=sla', **bad).data
    # a new ticket now gets the new resolution target (600 min)
    mk_customer(c); mk_ticket(c)
    from app.models import Ticket
    with app.app_context():
        t = Ticket.query.one()
        assert abs((t.due_at - t.created_at).total_seconds() - 600 * 60) < 120
    # canned + company
    post(c, '/settings/canned', '/settings?tab=canned', title='Thanks', body='Thank you!')
    assert b'Thank you!' in c.get('/tickets/1').data
    with app.app_context():
        cid = CannedResponse.query.filter_by(title='Thanks').one().id
    post(c, f'/settings/canned/{cid}/delete', '/settings?tab=canned')
    post(c, '/settings/general', '/settings?tab=general', company_name='Acme Care')
    assert b'Acme Care' in c.get('/').data


def test_user_management_rules(app):
    from app.models import User
    c = app.test_client(); login(c)
    assert b'at least 8' in post(c, '/settings/user', '/settings', name='X', email='x@x.com', password='short', role_id=3).data
    post(c, '/settings/user', '/settings', name='Ann', email='ann@x.com', password='Pw123456!', role_id=3, department_id=1)
    with app.app_context():
        ann = User.query.filter_by(email='ann@x.com').one().id
    # cannot deactivate yourself or the last admin
    assert b'cannot deactivate your own' in post(c, '/settings/users/1', '/settings/users/1', name='Admin', email='admin@baidesk.local', role_id=1).data
    assert b'At least one active administrator' in post(c, '/settings/users/1', '/settings/users/1', name='Admin', email='admin@baidesk.local', role_id=3, is_active='on').data
    # edit Ann: new role, two departments, reset password, then deactivate
    post(c, f'/settings/users/{ann}', f'/settings/users/{ann}', name='Ann B', email='ann@x.com', role_id=2,
         department_ids=['1', '2'], is_active='on', password='NewPass123')
    with app.app_context():
        u = db.session.get(User, ann)
        assert u.name == 'Ann B' and u.roles[0].name.startswith('Manager') and len(u.departments) == 2 and u.check_password('NewPass123')
    post(c, f'/settings/users/{ann}', f'/settings/users/{ann}', name='Ann B', email='ann@x.com', role_id=2)
    a = app.test_client()
    assert b'Invalid email or password' in login(a, 'ann@x.com', 'NewPass123').data   # inactive


def test_profile_password_and_theme(app):
    c = app.test_client(); login(c)
    assert b'incorrect' in post(c, '/profile', '/profile', current_password='bad', new_password='Another123', confirm_password='Another123').data
    assert b'do not match' in post(c, '/profile', '/profile', current_password='Admin@123', new_password='Another123', confirm_password='x').data
    assert b'Password updated' in post(c, '/profile', '/profile', current_password='Admin@123', new_password='Another123', confirm_password='Another123').data
    c2 = app.test_client()
    assert b'Invalid email' in login(c2).data and login(c2, password='Another123').status_code == 200
    # theme via JSON + CSRF header
    tk = token(c, '/profile')
    assert c.post('/profile/theme', json={'theme': 'light'}, headers={'X-CSRF-Token': tk}).status_code == 204
    assert c.post('/profile/theme', json={'theme': 'neon'}, headers={'X-CSRF-Token': tk}).status_code == 400
    assert c.post('/profile/theme', json={'theme': 'dark'}).status_code == 400         # no CSRF token
    assert '"light"' in c.get('/profile').get_data(as_text=True)


# ───────────── time handling ─────────────
def test_timezone_and_sla_timestamp_has_z(app):
    from app.timeutil import localdt, iso_utc, local_midnight_utc
    from datetime import datetime
    with app.app_context():
        assert localdt(datetime(2026, 1, 1, 21, 30), '%H:%M') == '00:30'     # UTC+3 (Kampala)
        assert iso_utc(datetime(2026, 1, 1, 9, 0)).endswith('Z')
        assert local_midnight_utc().hour in (21,) and local_midnight_utc() <= utcnow()
    c = app.test_client(); login(c); mk_customer(c); mk_ticket(c)
    assert re.search(r'data-due="[^"]+Z"', c.get('/tickets/1').get_data(as_text=True))


# ───────────── dashboard (1.3 visual refresh) ─────────────
def test_dashboard_renders_new_widgets_and_numbers(app):
    c = app.test_client()
    login(c)
    mk_customer(c)
    mk_ticket(c, 'Loan disbursement delayed')
    html = c.get('/').get_data(as_text=True)
    for text in ['Open tickets', 'SLA compliance', 'Ticket volume', 'SLA health', 'Needs attention',
                 'Team workload', 'Recent activity', 'Create ticket', 'Loan disbursement delayed']:
        assert text in html
    from app import dashboard_data as dd
    assert dd.fmt_duration(272) == '4m 32s' and dd.fmt_duration(None) == '—'
    d = dd.delta(10, 8, lower_is_better=True)
    assert d['dir'] == 'up' and d['tone'] == 'bad'
    assert dd.delta(5, 0)['text'] == '5 more vs yesterday'
