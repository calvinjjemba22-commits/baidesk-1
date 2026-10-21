"""Tests for the 1.4 workflow features: SLA clock, routing, promises, coaching, data tools, etc."""
import io, os, sqlite3, tempfile, zipfile
from datetime import datetime, timedelta, timezone, date
import pytest
from test_app import app, login, post, token, mk_customer, mk_ticket  # noqa: F401  (fixtures + helpers)
from app import create_app
from app.extensions import db
from app.timeutil import utcnow


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi)          # naive UTC


def local(y, m, d, h, mi=0):
    """Kampala is UTC+3 all year."""
    return datetime(y, m, d, h, mi) - timedelta(hours=3)


def mk_agent(app, name='Daniel Mugisha', email='daniel@x.com', cap=15, depts=(1,), away=False):
    from app.models import User, Role, Department
    with app.app_context():
        u = User(name=name, email=email)
        u.set_password('Passw0rd!')
        db.session.add(u)
        u.roles = [Role.query.filter_by(name='Agent').one()]
        u.departments = Department.query.filter(Department.id.in_(depts)).all()
        u.capacity, u.is_available = cap, not away
        db.session.commit()
        return u.id


# ───────────── SLA clock ─────────────
def test_deadlines_skip_nights_weekends_and_holidays(app):
    from app import sla
    from app.models import Holiday
    with app.app_context():
        fri_4pm = local(2026, 10, 16, 16)                       # Friday 16:00, 1h left in the working day
        assert sla.add_minutes(fri_4pm, 120) == local(2026, 10, 19, 9)       # → Monday 09:00
        assert sla.add_minutes(fri_4pm, 120, business_only=False) == fri_4pm + timedelta(hours=2)
        assert sla.add_minutes(local(2026, 10, 17, 10), 60) == local(2026, 10, 19, 9)   # Saturday → Monday
        assert sla.add_minutes(local(2026, 10, 14, 7, 0), 30) == local(2026, 10, 14, 8, 30)  # before opening
        db.session.add(Holiday(name='Test day', date=date(2026, 10, 19)))
        db.session.commit()
        assert sla.add_minutes(fri_4pm, 120) == local(2026, 10, 20, 9)       # Monday is a holiday
        assert sla.business_minutes_between(fri_4pm, local(2026, 10, 20, 9)) == 120


def test_pause_stops_the_clock_and_resume_extends_the_deadline(app):
    from app import sla
    from app.models import Ticket
    c = app.test_client(); login(c); mk_customer(c); mk_ticket(c)
    with app.app_context():
        t = db.session.get(Ticket, 1)
        t.created_at = local(2026, 10, 12, 10)                  # Monday 10:00
        sla.apply_deadlines(t)
        before = t.due_at
        sla.on_status_change(t, 'pending', local(2026, 10, 12, 12))
        assert t.sla_paused_at and not sla.is_late(t, utc(2030, 1, 1, 0))   # paused tickets are never late
        sla.on_status_change(t, 'open', local(2026, 10, 12, 15))            # 3 working hours later
        assert t.sla_paused_at is None and t.sla_paused_minutes == 180
        assert sla.business_minutes_between(before, t.due_at) == 180
        assert sla.is_late(t, t.due_at + timedelta(minutes=1))


def test_pending_status_pauses_via_the_ui_and_dashboard_ignores_it(app):
    from app.models import Ticket
    c = app.test_client(); login(c); mk_customer(c); mk_ticket(c)
    post(c, '/tickets/1/update', '/tickets/1', status_id=2, priority_id=2)       # 2 = Pending
    with app.app_context():
        t = db.session.get(Ticket, 1)
        assert t.sla_paused_at is not None
        t.due_at = utcnow() - timedelta(hours=5)                # would be overdue if the clock were running
        db.session.commit()
    html = c.get('/').get_data(as_text=True)
    assert 'data-spark=' in html
    from app import dashboard_data
    with app.app_context():
        from app.routes import is_active_status
        from app.models import Ticket as T
        data = dashboard_data.build(T.query, is_active_status(), utcnow(), __import__('app.models', fromlist=['User']).User.query.first())
        assert data['sla']['breached'] == 0
    assert 'Paused' in c.get('/tickets').get_data(as_text=True)
    post(c, '/tickets/1/update', '/tickets/1', status_id=1, priority_id=2)
    with app.app_context():
        assert db.session.get(Ticket, 1).sla_paused_at is None


def test_priority_change_recomputes_deadlines(app):
    from app.models import Ticket
    c = app.test_client(); login(c); mk_customer(c); mk_ticket(c)
    with app.app_context():
        medium_due = db.session.get(Ticket, 1).due_at
    post(c, '/tickets/1/update', '/tickets/1', status_id=1, priority_id=4)       # Urgent: 4h target
    with app.app_context():
        assert db.session.get(Ticket, 1).due_at < medium_due


def test_business_hours_and_holiday_admin(app):
    from app.models import BusinessHour, Holiday
    c = app.test_client(); login(c)
    assert b'Business hours' in c.get('/settings?tab=hours').data
    form = {f'start_{i}': '09:00' for i in range(7)}
    form.update({f'end_{i}': '18:00' for i in range(7)})
    form.update({f'work_{i}': 'on' for i in range(6)})            # Mon–Sat
    post(c, '/settings/hours', '/settings?tab=hours', **form)
    with app.app_context():
        assert BusinessHour.query.filter_by(day_of_week=5).one().is_working_day
        assert BusinessHour.query.filter_by(day_of_week=0).one().start_time == '09:00'
    bad = dict(form, start_0='18:00', end_0='09:00')
    assert b'closing time later' in post(c, '/settings/hours', '/settings?tab=hours', **bad).data
    post(c, '/settings/holidays', '/settings?tab=hours', name='Heroes', date='2026-06-09')
    post(c, '/settings/holidays/preset', '/settings?tab=hours', year='2027')
    with app.app_context():
        assert Holiday.query.count() == 11 and Holiday.query.filter_by(date=date(2027, 10, 9)).count() == 1


def test_sla_rule_can_ignore_business_hours(app):
    from app.models import SLASettings
    c = app.test_client(); login(c)
    page = c.get('/settings?tab=sla').get_data(as_text=True)
    assert 'Business hours only' in page and page.count('name="bh_') == 4
    post(c, '/settings/sla', '/settings?tab=sla', **{f'first_{i}': 60 for i in range(1, 5)},
         **{f'resolution_{i}': 600 for i in range(1, 5)}, **{f'active_{i}': 'on' for i in range(1, 5)})   # no bh_ → off
    with app.app_context():
        assert not any(s.business_hours_only for s in SLASettings.query.all())


def test_alerts_fire_once(app):
    from app import sla
    from app.models import Ticket, Notification, AlertSent
    c = app.test_client(); login(c); mk_customer(c); mk_ticket(c)
    with app.app_context():
        t = db.session.get(Ticket, 1)
        t.due_at = utcnow() - timedelta(minutes=5)
        t.assigned_user_id = 1
        db.session.commit()
        assert sla.watch(force=True) >= 1
        n = Notification.query.filter_by(type='sla').count()
        assert n >= 1
        sla.watch(force=True)
        assert Notification.query.filter_by(type='sla').count() == n      # no duplicates
        assert AlertSent.query.filter_by(kind='breached').count() == 1


# ───────────── routing ─────────────
def test_routing_picks_least_loaded_respects_capacity_and_away(app):
    from app import routing
    from app.models import Ticket
    a = mk_agent(app, 'Ann A', 'ann@x.com', cap=2)
    b = mk_agent(app, 'Bob B', 'bob@x.com', cap=2)
    away = mk_agent(app, 'Cy C', 'cy@x.com', cap=9, away=True)
    c = app.test_client(); login(c); mk_customer(c)
    with app.app_context():
        from app.models import User
        db.session.get(User, 1).is_available = False                  # keep the admin out of the pool
        db.session.commit()
        assert routing.pick_agent(1).id in (a, b)
    for _ in range(2):
        mk_ticket(c, subject='x', assigned_user_id=a, department_id=1, confirm_duplicate='1')
    with app.app_context():
        assert routing.pick_agent(1).id == b                    # Ann has 2 of 2
    for _ in range(2):
        mk_ticket(c, subject='y', assigned_user_id=b, department_id=1, confirm_duplicate='1')
    with app.app_context():
        assert routing.pick_agent(1) is None                    # both full; the away agent is never chosen
        assert away not in {u.id for u in routing.eligible_agents(1)}


def test_new_tickets_are_auto_assigned_and_it_can_be_switched_off(app):
    from app.models import Ticket, TicketHistory, User
    agent = mk_agent(app)
    c = app.test_client(); login(c); mk_customer(c)
    with app.app_context():
        db.session.get(User, 1).is_available = False
        db.session.commit()
    mk_ticket(c, subject='first', department_id=1)
    with app.app_context():
        assert db.session.get(Ticket, 1).assigned_user_id == agent
        assert TicketHistory.query.filter_by(action_type='AUTO_ASSIGNED').count() == 1
    post(c, '/settings/general', '/settings?tab=general', company_name='BaiDesk')      # box unticked → off
    mk_ticket(c, subject='second', department_id=1)
    with app.app_context():
        assert db.session.get(Ticket, 2).assigned_user_id is None


def test_manual_and_bulk_auto_assign(app):
    from app.models import Ticket, User
    agent = mk_agent(app)
    c = app.test_client(); login(c); mk_customer(c)
    with app.app_context():
        db.session.get(User, 1).is_available = False
        db.session.commit()
    post(c, '/settings/general', '/settings?tab=general', company_name='BaiDesk')      # switch auto-assign off
    mk_ticket(c, subject='a', department_id=1); mk_ticket(c, subject='b', department_id=1)
    post(c, '/tickets/1/auto-assign', '/tickets/1')
    post(c, '/tickets/bulk', '/tickets', ticket_ids='2', action='auto')
    with app.app_context():
        assert db.session.get(Ticket, 1).assigned_user_id == agent
        assert db.session.get(Ticket, 2).assigned_user_id == agent


def test_availability_toggle_and_capacity_settings(app):
    from app.models import User, Department
    c = app.test_client(); login(c)
    post(c, '/profile/availability', '/profile')
    with app.app_context():
        assert db.session.get(User, 1).is_available is False
    post(c, '/settings/departments', '/settings?tab=structure', id=1, name='Support', capacity='65', is_active='on')
    with app.app_context():
        assert db.session.get(Department, 1).capacity == 65
    assert b'Capacity must be' in post(c, '/settings/departments', '/settings?tab=structure', id=1, name='Support', capacity='abc').data
    post(c, '/settings/users/1', '/settings/users/1', name='Admin', email='admin@baidesk.local', role_id=1, capacity='7',
         department_ids=1, is_active='on')
    with app.app_context():
        assert db.session.get(User, 1).capacity == 7


def test_dashboard_shows_capacity_like_the_mockup(app):
    from app.models import Department
    c = app.test_client(); login(c); mk_customer(c)
    for i in range(3):
        mk_ticket(c, subject=f't{i}', department_id=1, assigned_user_id=1)
    with app.app_context():
        db.session.get(Department, 1).capacity = 10
        db.session.commit()
    html = c.get('/').get_data(as_text=True)
    assert '3 / 10' in html and '7 left' in html


# ───────────── workflow & quality ─────────────
def test_duplicate_warning_then_create_anyway(app):
    from app.models import Ticket
    c = app.test_client(); login(c); mk_customer(c)
    mk_ticket(c, subject='Cannot connect to the internet')
    page = mk_ticket(c, subject='cannot connect to internet').get_data(as_text=True)
    assert 'already open' in page and 'Create anyway' in page
    with app.app_context():
        assert Ticket.query.count() == 1
    mk_ticket(c, subject='cannot connect to internet', confirm_duplicate='1')
    mk_ticket(c, subject='Totally different matter about billing')       # not similar → no warning
    with app.app_context():
        assert Ticket.query.count() == 3


def test_similarity_helper():
    from app.quality import similarity
    assert similarity('Loan disbursement delayed', 'loan disbursement is delayed') > 0.6
    assert similarity('Password reset', 'Bond clearance status') < 0.4
    assert similarity('Payment not reflected', 'Password reset request') < 0.4
    assert similarity('Cant login to portal', 'Cant login to portl') >= 0.6       # typo


def test_customer_reply_reopens_resolved_ticket_and_notifies(app):
    from app.models import Ticket, Notification
    agent = mk_agent(app)
    c = app.test_client(); login(c); mk_customer(c)
    mk_ticket(c, assigned_user_id=agent, department_id=1)
    post(c, '/tickets/1/update', '/tickets/1', status_id=5, priority_id=2)              # Resolved
    post(c, '/tickets/1/comment', '/tickets/1', comment_type='CUSTOMER', body='Still not working, please help')
    with app.app_context():
        t = db.session.get(Ticket, 1)
        assert t.status.slug == 'open' and t.resolved_at is None
        assert Notification.query.filter_by(user_id=agent, type='reply').count() == 1
    page = c.get('/tickets/1').get_data(as_text=True)
    assert 'Customer reply ·' in page and 'it msg in' in page and 'Customer replied' in page      # shown as the customer's own message


def test_log_contact_joins_the_timeline_and_counts_as_first_response(app):
    from app.models import Ticket, Interaction
    c = app.test_client(); login(c); mk_customer(c); mk_ticket(c)
    post(c, '/tickets/1/contact', '/tickets/1', channel='WhatsApp', direction='Outbound', notes='Sent payment link')
    with app.app_context():
        assert db.session.get(Ticket, 1).first_response_at is not None
        assert Interaction.query.filter_by(ticket_id=1, channel='WhatsApp').count() == 1
    page = c.get('/tickets/1').get_data(as_text=True)
    assert 'WhatsApp · Outbound' in page and 'Sent payment link' in page and 'data-kind="contact"' in page
    assert b'Add a short note' in post(c, '/tickets/1/contact', '/tickets/1', channel='Call', direction='Inbound', notes=' ').data
    assert post(c, '/tickets/1/contact', '/tickets/1', channel='Pigeon', direction='Inbound', notes='x').status_code == 400


def test_ticket_page_shows_customer_context(app):
    c = app.test_client(); login(c); mk_customer(c)
    mk_ticket(c, subject='First issue'); mk_ticket(c, subject='Billing question about invoice', confirm_duplicate='1')
    page = c.get('/tickets/2').get_data(as_text=True)
    assert 'Other open tickets' in page and 'First issue' in page and '0770000111' in page


def test_resolve_and_next_goes_to_the_next_urgent_ticket(app):
    from app.models import Ticket
    c = app.test_client(); login(c); mk_customer(c)
    mk_ticket(c, subject='alpha one', assigned_user_id=1); mk_ticket(c, subject='bravo two zzz', assigned_user_id=1, confirm_duplicate='1')
    with app.app_context():
        db.session.get(Ticket, 1).due_at = utcnow() + timedelta(hours=1)
        db.session.get(Ticket, 2).due_at = utcnow() + timedelta(hours=9)
        db.session.commit()
    page = post(c, '/tickets/2/update', '/tickets/2', status_id=1, priority_id=2, resolve_next='1').get_data(as_text=True)
    assert 'Up next' in page and 'TKT-00001' in page
    with app.app_context():
        assert db.session.get(Ticket, 2).status.slug == 'resolved'
    page = post(c, '/tickets/1/update', '/tickets/1', status_id=1, priority_id=2, resolve_next='1').get_data(as_text=True)
    assert 'queue is clear' in page


def test_saved_views_are_sanitised_shared_and_deletable(app):
    from app.models import SavedView
    agent = mk_agent(app)
    c = app.test_client(); login(c)
    post(c, '/tickets/views/save', '/tickets', name='Urgent mine', query='view=mine&priority=urgent&evil=1&page=3', is_shared='on')
    with app.app_context():
        v = SavedView.query.one()
        assert v.filters == 'view=mine&priority=urgent' and v.is_shared
    assert 'Urgent mine' in c.get('/tickets').get_data(as_text=True)
    c2 = app.test_client(); login(c2, 'daniel@x.com', 'Passw0rd!')
    assert 'Urgent mine' in c2.get('/tickets').get_data(as_text=True)                 # shared with the team
    assert post(c2, '/tickets/views/1/delete', '/tickets').status_code in (200, 403)
    with app.app_context():
        assert SavedView.query.count() == 1                                            # an agent can't delete the admin's view
    post(c, '/tickets/views/1/delete', '/tickets')
    with app.app_context():
        assert SavedView.query.count() == 0


# ───────────── promises ─────────────
def test_promise_lifecycle_alerts_and_validation(app):
    from app import sla
    from app.models import PaymentPromise, Notification
    from app.timeutil import local_today
    c = app.test_client(); login(c); mk_customer(c); mk_ticket(c)
    assert b'today or later' in post(c, '/promises/new', '/tickets/1', customer_id=1, ticket_id=1, promised_date='2020-01-01').data
    assert b'whole number' in post(c, '/promises/new', '/tickets/1', customer_id=1, ticket_id=1,
                                   promised_date=local_today().isoformat(), amount='12abc').data
    post(c, '/promises/new', '/tickets/1', customer_id=1, ticket_id=1, promised_date=local_today().isoformat(),
         amount='150,000', note='Pays by mobile money')
    with app.app_context():
        p = PaymentPromise.query.one()
        assert p.amount == 150000 and p.status == 'Pending'
        sla.watch(force=True)
        sla.watch(force=True)
        assert Notification.query.filter_by(type='promise').count() == 1             # reminded once
    page = c.get('/promises').get_data(as_text=True)
    assert 'UGX 150,000' in page and 'Due or overdue' in page
    post(c, '/promises/1/kept', '/promises')
    with app.app_context():
        assert PaymentPromise.query.one().status == 'Kept'
    assert '100%' in c.get('/promises?tab=kept').get_data(as_text=True)
    assert 'already closed' in post(c, '/promises/1/broken', '/promises').get_data(as_text=True)
    assert 'UGX 150,000' in c.get('/customers/1').get_data(as_text=True)


def test_agents_cannot_close_other_peoples_promises(app):
    from app.models import PaymentPromise
    from app.timeutil import local_today
    agent = mk_agent(app)
    c = app.test_client(); login(c); mk_customer(c)
    post(c, '/promises/new', '/customers/1', customer_id=1, promised_date=local_today().isoformat())
    c2 = app.test_client(); login(c2, 'daniel@x.com', 'Passw0rd!')
    assert post(c2, '/promises/1/kept', '/promises').status_code == 403
    with app.app_context():
        assert PaymentPromise.query.one().status == 'Pending'


# ───────────── coaching & insights ─────────────
def test_coaching_scorecard_reviews_and_access(app):
    from app import coaching
    from app.models import Ticket, User, TicketReview
    agent = mk_agent(app)
    c = app.test_client(); login(c); mk_customer(c)
    for i in range(3):
        mk_ticket(c, subject=f'issue number {i} {"abc"*i}', assigned_user_id=agent, department_id=1, confirm_duplicate='1')
        post(c, f'/tickets/{i+1}/update', f'/tickets/{i+1}', status_id=5, priority_id=2)
    with app.app_context():
        for t in Ticket.query.all():
            t.first_response_at = t.created_at + timedelta(minutes=10)
            t.due_at = t.resolved_at + timedelta(hours=1)
        db.session.commit()
        start, end = utcnow() - timedelta(days=1), utcnow() + timedelta(days=1)
        card = coaching.scorecard(db.session.get(User, agent), start, end)
        assert card['resolved'] == 3 and round(card['avg_first_response_min']) == 10 and card['sla_met_pct'] == 100
        assert len(coaching.review_samples(db.session.get(User, agent), start, end)) == 3
    assert b'Tickets to review' in c.get(f'/coaching/{agent}').data
    post(c, '/coaching/review', f'/coaching/{agent}', ticket_id=1, score=4, notes='Clear and polite')
    assert b'already been reviewed' in post(c, '/coaching/review', f'/coaching/{agent}', ticket_id=1, score=5).data
    post(c, '/coaching/review', f'/coaching/{agent}', ticket_id=2, score=9)
    with app.app_context():
        assert TicketReview.query.count() == 1
    assert 'Daniel Mugisha' in c.get('/coaching').get_data(as_text=True)
    c2 = app.test_client(); login(c2, 'daniel@x.com', 'Passw0rd!')
    assert b'Tickets to review' not in c2.get('/coaching', follow_redirects=True).data       # agents see only their own card
    assert c2.get('/coaching/1').status_code == 403


def test_reopen_count_in_scorecard(app):
    from app import coaching
    from app.models import User
    agent = mk_agent(app)
    c = app.test_client(); login(c); mk_customer(c); mk_ticket(c, assigned_user_id=agent, department_id=1)
    post(c, '/tickets/1/update', '/tickets/1', status_id=5, priority_id=2)
    post(c, '/tickets/1/update', '/tickets/1', status_id=1, priority_id=2)
    with app.app_context():
        card = coaching.scorecard(db.session.get(User, agent), utcnow() - timedelta(days=1), utcnow() + timedelta(days=1))
        assert card['reopened'] == 1


def test_insights_explain_what_changed(app):
    from app import insights
    from app.models import Ticket
    c = app.test_client(); login(c); mk_customer(c)
    for i in range(6):
        mk_ticket(c, subject=f'distinct topic {i}{"x"*i*3}', category_id=2, department_id=1, confirm_duplicate='1')
    with app.app_context():
        lines = insights.weekly(Ticket.query)
        text = ' '.join(l['text'] for l in lines)
        assert 'no owner' in text or 'unassigned' in text.lower() or 'came in' in text
        assert all(l['tone'] in ('good', 'bad', 'info') for l in lines)
        # last week had 2 tickets, this week has 6 → volume up, and Payments + Support get the blame
        for t in Ticket.query.limit(2).all():
            t.created_at = utcnow() - timedelta(days=10)
        db.session.commit()
        text = ' '.join(l['text'] for l in insights.weekly(Ticket.query))
        assert 'up' in text and 'Payments' in text
    assert b'What changed this week' in c.get('/reports').data


# ───────────── data: backup, import, schema upgrade ─────────────
def test_backup_is_a_valid_zip_with_a_readable_database(app, tmp_path):
    dbfile = tmp_path / 'live.db'
    app2 = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{dbfile}', 'SECRET_KEY': 't',
                       'UPLOAD_FOLDER': str(tmp_path / 'up')})
    (tmp_path / 'up').mkdir(exist_ok=True)
    (tmp_path / 'up' / 'doc.txt').write_text('hello')
    c = app2.test_client(); login(c); mk_customer(c)
    r = post(c, '/settings/backup', '/settings?tab=data')
    assert r.mimetype == 'application/zip' and 'baidesk-backup-' in r.headers['Content-Disposition']
    z = zipfile.ZipFile(io.BytesIO(r.data))
    assert {'baidesk.db', 'uploads/doc.txt', 'README.txt'} <= set(z.namelist())
    out = tmp_path / 'restored.db'
    out.write_bytes(z.read('baidesk.db'))
    n = sqlite3.connect(out).execute('select count(*) from customers').fetchone()[0]
    assert n == 1


def _csv_upload(c, kind, text, validate=False):
    data = {'_csrf': token(c, '/settings?tab=data'), 'file': (io.BytesIO(text.encode('utf-8-sig')), 'x.csv')}
    if validate:
        data['validate_only'] = 'on'
    return c.post(f'/settings/import/{kind}', data=data, content_type='multipart/form-data', follow_redirects=True)


def test_customer_import_validates_then_imports_and_skips_duplicates(app):
    from app.models import Customer, CustomerPhone
    c = app.test_client(); login(c)
    text = ('first_name,last_name,company,email,phone,location,notes\n'
            'Grace,Nakato,,grace@x.com,0772 123 456,Kampala,\n'
            ',Missing,,,,,\n'
            'Bad,Email,,not-an-email,,,\n'
            'Grace,Again,,grace@x.com,,,\n'
            'Peter,Okello,,,0701000222,,\n')
    page = _csv_upload(c, 'customers', text, validate=True).get_data(as_text=True)
    assert 'Nothing was saved' in page and 'first_name is required' in page and 'not a valid email' in page
    with app.app_context():
        assert Customer.query.count() == 0
    page = _csv_upload(c, 'customers', text).get_data(as_text=True)
    assert 'Import finished' in page and 'already exists' in page
    with app.app_context():
        assert Customer.query.count() == 2
        assert CustomerPhone.query.filter_by(phone_number='0772123456').count() == 1
        assert {x.customer_number for x in Customer.query.all()} == {'CUS-00001', 'CUS-00002'}
    assert b'Choose a CSV' in c.post('/settings/import/customers', data={'_csrf': token(c, '/settings?tab=data')},
                                    content_type='multipart/form-data', follow_redirects=True).data
    assert b'column headings' in _csv_upload(c, 'customers', 'foo,bar\n1,2\n').data


def test_ticket_import_links_customers_and_reports_problems(app):
    from app.models import Ticket
    c = app.test_client(); login(c); mk_customer(c)
    text = ('customer,subject,description,priority,status,department,category,assignee_email,created_at\n'
            'CUS-00001,Loan delayed,Waiting,High,Resolved,Support,Payments,admin@baidesk.local,2026-10-01 09:30\n'
            '0770000111,Statement request,,,,Finance,,,\n'
            'nobody,Lost,,,,,,,\n'
            'CUS-00001,Odd status,,Medium,Zzz,,,,\n'
            'CUS-00001,Bad date,,,,,,,31-31-2026\n')
    page = _csv_upload(c, 'tickets', text, validate=True).get_data(as_text=True)
    assert 'Nothing was saved' in page and 'not found' in page and 'unknown priority or status' in page and 'created_at must' in page
    with app.app_context():
        assert Ticket.query.count() == 0
    _csv_upload(c, 'tickets', text)
    with app.app_context():
        assert Ticket.query.count() == 2
        t = Ticket.query.filter_by(subject='Loan delayed').one()
        assert t.created_at == local(2026, 10, 1, 9, 30) and t.resolved_at and t.due_at and t.assigned_user_id == 1
        assert Ticket.query.filter_by(subject='Statement request').one().description == 'Statement request'
    assert b'customer,subject' in c.get('/settings/import/template/tickets.csv').data


def test_data_tools_are_admin_only(app):
    mk_agent(app)
    c = app.test_client(); login(c, 'daniel@x.com', 'Passw0rd!')
    assert post(c, '/settings/backup', '/').status_code == 403
    assert c.get('/settings/import/template/customers.csv').status_code == 403
    assert c.get('/settings?tab=data').status_code == 403


def test_schema_upgrade_adds_missing_columns_to_an_old_database(tmp_path):
    dbfile = tmp_path / 'old.db'
    uri = f'sqlite:///{dbfile}'
    create_app({'SQLALCHEMY_DATABASE_URI': uri, 'UPLOAD_FOLDER': str(tmp_path), 'SECRET_KEY': 't'})
    conn = sqlite3.connect(dbfile)                          # simulate a 1.2 / 1.3.0 database
    for table, col in [('tickets', 'first_response_due_at'), ('tickets', 'sla_paused_at'), ('tickets', 'sla_paused_minutes'),
                       ('users', 'capacity'), ('users', 'is_available'), ('departments', 'capacity')]:
        conn.execute(f'ALTER TABLE {table} DROP COLUMN {col}')
    conn.commit(); conn.close()
    app2 = create_app({'SQLALCHEMY_DATABASE_URI': uri, 'UPLOAD_FOLDER': str(tmp_path), 'SECRET_KEY': 't'})
    cols = {r[1] for r in sqlite3.connect(dbfile).execute('pragma table_info(tickets)')}
    assert {'first_response_due_at', 'sla_paused_at', 'sla_paused_minutes'} <= cols
    c = app2.test_client(); login(c)
    assert c.get('/').status_code == 200 and c.get('/tickets').status_code == 200
    from app.migrate import upgrade_schema
    with app2.app_context():
        assert upgrade_schema() == []                       # idempotent
        from app.models import User
        assert User.query.first().capacity == 15


def test_new_pages_render_and_reject_unauthenticated_access(app):
    anon = app.test_client()
    for url in ['/promises', '/coaching', '/coaching/1', '/settings?tab=hours']:
        assert anon.get(url).status_code in (302, 401, 403)
    c = app.test_client(); login(c)
    for url in ['/promises', '/promises?tab=upcoming', '/promises?tab=all&scope=mine', '/coaching', '/coaching?days=7',
                '/coaching/1', '/settings?tab=hours', '/settings?tab=data', '/settings?tab=general', '/reports']:
        assert c.get(url).status_code == 200, url


# ───────────── routing by department + need, and the queue ─────────────
def set_skills(app, user_id, category_ids):
    from app.models import User, Category
    with app.app_context():
        u = db.session.get(User, user_id)
        u.skills = Category.query.filter(Category.id.in_(category_ids)).all()
        db.session.commit()


def admin_away(app):
    from app.models import User
    with app.app_context():
        db.session.get(User, 1).is_available = False
        db.session.commit()


def test_need_matching_prefers_specialists_then_generalists_then_anyone(app):
    from app import routing
    pay = mk_agent(app, 'Pat Payments', 'pat@x.com', cap=1)
    gen = mk_agent(app, 'Gina General', 'gina@x.com', cap=5)
    tech = mk_agent(app, 'Tom Tech', 'tom@x.com', cap=5)
    set_skills(app, pay, [2])                       # Payments
    set_skills(app, tech, [4])                      # Technical Issue
    c = app.test_client(); login(c); mk_customer(c); admin_away(app)
    with app.app_context():
        agent, why = routing.pick(1, 2)             # a Payments ticket → the Payments specialist
        assert agent.id == pay and 'skill' in why
        assert routing.pick(1, 4)[0].id == tech
        assert routing.pick(1, 6)[0].id == gen      # Complaint: nobody specialises → the generalist
    mk_ticket(c, subject='first payment', category_id=2, department_id=1)
    with app.app_context():
        from app.models import Ticket
        assert db.session.get(Ticket, 1).assigned_user_id == pay
        agent, why = routing.pick(1, 2)             # Pat is now full (cap 1) → generalist covers Payments
        assert agent.id == gen and 'Generalist' in why
        set_skills(app, gen, [3]) if False else None
    set_skills(app, gen, [3])                       # now Gina specialises in Account, Tom in Tech, Pat is full
    with app.app_context():
        agent, why = routing.pick(1, 2)             # nobody matches → fall back to any agent with room
        assert agent.id in (gen, tech) and 'free capacity' in why


def test_department_is_inferred_from_the_category(app):
    from app.models import Ticket, Category
    coll = mk_agent(app, 'Cora Collections', 'cora@x.com', depts=(2,))      # department 2 = Collections only
    sup = mk_agent(app, 'Sam Support', 'sam@x.com', depts=(1,))
    c = app.test_client(); login(c); mk_customer(c); admin_away(app)
    with app.app_context():
        db.session.get(Category, 5).department_id = 2                       # Collections category → Collections dept
        db.session.commit()
    mk_ticket(c, subject='Overdue instalment query', category_id=5)        # no department chosen
    with app.app_context():
        t = db.session.get(Ticket, 1)
        assert t.assigned_user_id == coll and t.department_id == 2


def test_no_department_or_category_still_gets_an_available_agent(app):
    from app.models import Ticket
    only = mk_agent(app, 'Olu Only', 'olu@x.com')
    c = app.test_client(); login(c); mk_customer(c); admin_away(app)
    mk_ticket(c, subject='General question')
    with app.app_context():
        assert db.session.get(Ticket, 1).assigned_user_id == only


def test_waiting_tickets_are_handed_out_when_an_agent_returns(app):
    from app import routing
    from app.models import Ticket
    agent = mk_agent(app, away=True)
    c = app.test_client(); login(c); mk_customer(c); admin_away(app)
    mk_ticket(c, subject='alpha', department_id=1)
    with app.app_context():
        assert db.session.get(Ticket, 1).assigned_user_id is None          # nobody on shift → waits in the queue
        assert routing.drain_queue() == 0
    c2 = app.test_client(); login(c2, 'daniel@x.com', 'Passw0rd!')
    post(c2, '/profile/availability', '/profile')                           # the agent comes back
    with app.app_context():
        assert db.session.get(Ticket, 1).assigned_user_id == agent


def test_queue_drains_most_urgent_first_and_stops_at_capacity(app):
    from app import routing
    from app.models import Ticket, User
    agent = mk_agent(app, cap=2, away=True)
    c = app.test_client(); login(c); mk_customer(c); admin_away(app)
    mk_ticket(c, subject='low one', priority_id=1, department_id=1)
    mk_ticket(c, subject='urgent one qq', priority_id=4, department_id=1, confirm_duplicate='1')
    mk_ticket(c, subject='medium one zz', priority_id=2, department_id=1, confirm_duplicate='1')
    with app.app_context():
        db.session.get(User, agent).is_available = True
        db.session.commit()
        assert routing.drain_queue() == 2                                   # capacity 2 → only two get assigned
        assert db.session.get(Ticket, 2).assigned_user_id == agent          # urgent
        assert db.session.get(Ticket, 3).assigned_user_id == agent          # then medium
        assert db.session.get(Ticket, 1).assigned_user_id is None           # low waits


def test_resolving_a_ticket_frees_capacity_for_the_next_waiting_one(app):
    from app.models import Ticket
    agent = mk_agent(app, cap=1)
    c = app.test_client(); login(c); mk_customer(c); admin_away(app)
    mk_ticket(c, subject='first job', department_id=1)
    mk_ticket(c, subject='second different thing', department_id=1, confirm_duplicate='1')
    with app.app_context():
        assert db.session.get(Ticket, 1).assigned_user_id == agent
        assert db.session.get(Ticket, 2).assigned_user_id is None
    post(c, '/tickets/1/update', '/tickets/1', status_id=5, priority_id=2)  # agent's ticket gets resolved
    with app.app_context():
        assert db.session.get(Ticket, 2).assigned_user_id == agent


def test_deliberately_unassigned_tickets_are_not_grabbed_back(app):
    from app import routing
    from app.models import Ticket
    agent = mk_agent(app)
    c = app.test_client(); login(c); mk_customer(c); admin_away(app)
    mk_ticket(c, subject='keep me manual', department_id=1)
    post(c, '/tickets/1/update', '/tickets/1', status_id=1, priority_id=2, department_id=1, assigned_user_id='')
    with app.app_context():
        assert routing.drain_queue() == 0 and db.session.get(Ticket, 1).assigned_user_id is None
    post(c, '/tickets/1/auto-assign', '/tickets/1')                          # the button still works on demand
    with app.app_context():
        assert db.session.get(Ticket, 1).assigned_user_id == agent


def test_auto_assign_off_means_no_queue_draining(app):
    from app import routing
    from app.models import Ticket
    mk_agent(app)
    c = app.test_client(); login(c); mk_customer(c); admin_away(app)
    post(c, '/settings/general', '/settings?tab=general', company_name='BaiDesk')      # box unticked → off
    mk_ticket(c, subject='manual only', department_id=1)
    with app.app_context():
        assert routing.drain_queue() == 0 and db.session.get(Ticket, 1).assigned_user_id is None


def test_agent_skills_are_edited_in_administration(app):
    from app.models import User
    agent = mk_agent(app)
    c = app.test_client(); login(c)
    page = c.get(f'/settings/users/{agent}').get_data(as_text=True)
    assert 'Handles these needs' in page and page.count('name="skill_ids"') >= 6
    post(c, f'/settings/users/{agent}', f'/settings/users/{agent}', name='Daniel Mugisha', email='daniel@x.com', role_id=3,
         department_ids=1, is_active='on', capacity='10', skill_ids=['2', '4'])
    with app.app_context():
        assert {s.name for s in db.session.get(User, agent).skills} == {'Payments', 'Technical Issue'}
    post(c, f'/settings/users/{agent}', f'/settings/users/{agent}', name='Daniel Mugisha', email='daniel@x.com', role_id=3,
         department_ids=1, is_active='on', capacity='10')                                    # untick everything → generalist
    with app.app_context():
        assert db.session.get(User, agent).skills == []


# ───────────── pilot readiness: accounts, teams, import, server, backups ─────────────
def clear_forced(app, *emails):
    from app.models import User
    with app.app_context():
        for u in User.query.filter(User.email.in_(emails)).all():
            u.must_change_password = False
        db.session.commit()


def test_login_lockout_after_repeated_failures_and_success_resets(app):
    from app.models import LoginAttempt
    mk_agent(app)
    c = app.test_client()
    for _ in range(4):
        assert b'Invalid email or password' in login(c, 'daniel@x.com', 'wrong').data
    assert b'Dashboard' in login(c, 'daniel@x.com', 'Passw0rd!').data or c.get('/').status_code == 200   # 4 failures: still allowed
    with app.app_context():
        assert LoginAttempt.query.filter_by(email='daniel@x.com').count() == 0                          # success clears the count
    c2 = app.test_client()
    for _ in range(5):
        login(c2, 'daniel@x.com', 'wrong')
    r = c2.post('/login', data={'email': 'daniel@x.com', 'password': 'Passw0rd!', '_csrf': token(c2)})
    assert r.status_code == 429 and b'Too many failed attempts' in r.data              # even the right password is refused now
    c3 = app.test_client()
    assert b'Invalid email or password' in login(c3, 'someone.else@x.com', 'nope').data   # other people are unaffected


def test_admin_chosen_passwords_are_temporary(app):
    from app.models import User
    c = app.test_client(); login(c)
    post(c, '/settings/user', '/settings', name='Ann Agent', email='ann@x.com', password='Pw123456!', role_id=3, department_id=1)
    with app.app_context():
        assert User.query.filter_by(email='ann@x.com').one().must_change_password
    clear_forced(app, 'ann@x.com')
    post(c, '/settings/users/2', '/settings/users/2', name='Ann Agent', email='ann@x.com', role_id=3, department_ids=1,
         is_active='on', password='Reset-by-admin-1')
    with app.app_context():
        assert User.query.filter_by(email='ann@x.com').one().must_change_password          # an admin reset is temporary too
    a = app.test_client(); login(a, 'ann@x.com', 'Reset-by-admin-1')
    assert b'Set your own password' in a.get('/reports', follow_redirects=True).data
    assert a.get('/logout').status_code == 302                                              # leaving is always allowed
    a2 = app.test_client(); login(a2, 'ann@x.com', 'Reset-by-admin-1')
    post(a2, '/profile', '/profile', current_password='Reset-by-admin-1', new_password='Brand-new-pass-9', confirm_password='Brand-new-pass-9')
    assert a2.get('/tickets').status_code == 200


def test_teams_leads_membership_and_validation(app):
    from app.models import Team, User, Role
    agent = mk_agent(app)
    with app.app_context():
        lead = User(name='Lena Lead', email='lena@x.com'); lead.set_password('Passw0rd!')
        db.session.add(lead); lead.roles = [Role.query.filter_by(name='Manager / Team Lead').one()]
        db.session.commit(); lead_id = lead.id
    c = app.test_client(); login(c)
    assert b'Teams' in c.get('/settings?tab=teams').data
    assert b'Manager / Team Lead role' in post(c, '/settings/teams', '/settings?tab=teams', name='Bad', lead_id=agent).data
    post(c, '/settings/teams', '/settings?tab=teams', name='Team Lena', lead_id=lead_id)
    assert b'already a team' in post(c, '/settings/teams', '/settings?tab=teams', name='team lena').data
    post(c, f'/settings/users/{agent}', f'/settings/users/{agent}', name='Daniel Mugisha', email='daniel@x.com', role_id=3,
         department_ids=1, is_active='on', capacity='15', team_id='1')
    with app.app_context():
        t = db.session.get(Team, 1)
        assert t.lead.id == lead_id and {m.id for m in t.members} == {lead_id, agent}
        assert db.session.get(User, agent).team.name == 'Team Lena'
    post(c, '/settings/teams/1/delete', '/settings?tab=teams')
    with app.app_context():
        assert Team.query.count() == 0 and db.session.get(User, agent).team is None


def test_team_leads_only_see_their_own_team_in_coaching(app):
    from app.models import Team, User, Role, Ticket
    a1 = mk_agent(app, 'Amy One', 'amy@x.com')
    a2 = mk_agent(app, 'Ben Two', 'ben@x.com')
    with app.app_context():
        role = Role.query.filter_by(name='Manager / Team Lead').one()
        leads = []
        for nm, em in (('Lead One', 'l1@x.com'), ('Lead Free', 'free@x.com')):
            u = User(name=nm, email=em); u.set_password('Passw0rd!')
            db.session.add(u); u.roles = [role]; leads.append(u)
        db.session.flush()
        t1, t2 = Team(name='T1', lead=leads[0]), Team(name='T2')
        db.session.add_all([t1, t2])
        t1.members = [leads[0], db.session.get(User, a1)]
        t2.members = [db.session.get(User, a2)]
        db.session.commit()
    c = app.test_client(); login(c); mk_customer(c)
    mk_ticket(c, subject='for ben', assigned_user_id=a2, department_id=1)
    post(c, '/tickets/1/update', '/tickets/1', status_id=5, priority_id=2)
    lead = app.test_client(); login(lead, 'l1@x.com', 'Passw0rd!')
    page = lead.get('/coaching').get_data(as_text=True)
    with app.app_context():
        lead_id = User.query.filter_by(email='l1@x.com').one().id
    assert 'Amy One' in page and 'Ben Two' not in page                                     # own agents only
    assert f'/coaching/{lead_id}?' not in page and f'/coaching/{lead_id}"' not in page       # and not themselves
    assert lead.get(f'/coaching/{a1}').status_code == 200
    assert lead.get(f'/coaching/{a2}').status_code == 403                                   # another team's agent
    assert post(lead, '/coaching/review', '/coaching', ticket_id=1, score=5).status_code == 403
    free = app.test_client(); login(free, 'free@x.com', 'Passw0rd!')
    assert 'Ben Two' in free.get('/coaching').get_data(as_text=True)                         # a lead with no team sees everyone
    assert 'Ben Two' in c.get('/coaching').get_data(as_text=True)                            # admin too


def test_staff_import_creates_accounts_teams_and_leads(app):
    from app.models import User, Team
    c = app.test_client(); login(c)
    text = ('full_name,email,role,team,departments,capacity\n'
            'Sam Lead,sam@x.com,Team Lead,Team Sam,,\n'
            'Ann Agent,ann@x.com,Agent,Team Sam,Support; Finance,20\n'
            'Bob Agent,bob@x.com,agent,Team Sam,,\n'
            'Eve Boss,eve@x.com,Admin,,,\n'
            'Dup Lead,dup@x.com,Team Lead,Team Sam,,\n'
            'Bad Dept,bd@x.com,Agent,,Warehouse,\n'
            'No Email,,Agent,,,\n'
            'Cap Bad,cap@x.com,Agent,,,9999\n')
    page = _csv_upload(c, 'users', text, validate=True).get_data(as_text=True)
    assert 'Nothing was saved' in page and 'must be Agent or Team Lead' in page and 'already has a lead' in page
    assert 'unknown department' in page and 'valid email' in page and 'capacity must be' in page
    with app.app_context():
        assert User.query.count() == 1
    page = _csv_upload(c, 'users', text).get_data(as_text=True)
    assert 'Import finished' in page and 'shown only once' in page and 'baidesk-temporary-passwords.csv' in page
    with app.app_context():
        sam, ann, bob = (User.query.filter_by(email=e).one() for e in ('sam@x.com', 'ann@x.com', 'bob@x.com'))
        assert User.query.count() == 4 and sam.is_manager() and not ann.is_manager()
        assert all(u.must_change_password for u in (sam, ann, bob))
        assert Team.query.one().lead.id == sam.id and {m.name for m in Team.query.one().members} == {'Sam Lead', 'Ann Agent', 'Bob Agent'}
        assert {d.name for d in ann.departments} == {'Support', 'Finance'} and ann.capacity == 20
        assert len(bob.departments) == 4                                       # blank departments = all of them
    pw = __import__('re').search(r'<code>([^<]+)</code>', page).group(1)
    assert len(pw) == 10
    other = app.test_client(); login(other, 'sam@x.com', pw)                    # the shown temporary password really works
    assert b'Set your own password' in other.get('/', follow_redirects=True).data
    assert _csv_upload(c, 'users', text).get_data(as_text=True).count('already exists') >= 3   # re-import creates no duplicates


def test_first_run_creates_a_private_secret_once(tmp_path):
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from first_run import ensure_env
    first = ensure_env(tmp_path)
    text = (tmp_path / '.env').read_text()
    secret = [l for l in text.splitlines() if l.startswith('SECRET_KEY=')][0]
    assert first and len(first['admin_password']) == 12 and len(secret.split('=')[1]) == 64
    assert 'admin_password' not in text and first['admin_password'] not in text                # never written to .env
    assert ensure_env(tmp_path) is None and (tmp_path / '.env').read_text() == text            # second start changes nothing
    (tmp_path / '.env').write_text('PORT=9000')                                                # an old .env lacking a secret
    assert ensure_env(tmp_path) is None
    assert 'PORT=9000' in (tmp_path / '.env').read_text() and 'SECRET_KEY=' in (tmp_path / '.env').read_text()


def test_wal_mode_and_backup_command_keeps_only_recent_backups(tmp_path):
    uri = f"sqlite:///{tmp_path / 'live.db'}"
    app2 = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': uri, 'SECRET_KEY': 't', 'UPLOAD_FOLDER': str(tmp_path / 'up')})
    with app2.app_context():
        assert db.session.execute(db.text('PRAGMA journal_mode')).scalar() == 'wal'
        assert db.session.execute(db.text('PRAGMA busy_timeout')).scalar() == 15000
    out = tmp_path / 'backups'
    out.mkdir()
    for stamp in ('20200101-0100', '20200102-0100', '20200103-0100'):
        (out / f'baidesk-backup-{stamp}.zip').write_bytes(b'old')
    result = app2.test_cli_runner().invoke(args=['backup', '--out', str(out), '--keep', '2'])
    assert result.exit_code == 0 and 'Backup saved' in result.output
    names = sorted(os.listdir(out))
    assert len(names) == 2 and '20200101-0100' not in ' '.join(names)                          # oldest pruned, newest kept
    new = [n for n in names if '2020' not in n][0]
    z = zipfile.ZipFile(out / new)
    assert 'baidesk.db' in z.namelist() and z.testzip() is None


def test_server_script_refuses_the_default_secret():
    import subprocess, sys
    root = os.path.dirname(os.path.dirname(__file__))
    code = ("import os,sys; sys.path.insert(0,r'%s'); os.environ['SECRET_KEY']='baidesk-dev-secret-change-me'; "
            "import first_run; first_run.ensure_env = lambda base: None; import runpy; runpy.run_path(r'%s/serve.py', run_name='x')" % (root, root))
    r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, cwd=root, timeout=60)
    assert r.returncode != 0 and 'Refusing to start' in (r.stdout + r.stderr)


# ───────────── interface layer: palette, preview drawer, brand colour ─────────────
def test_palette_search_requires_login_and_minimum_length(app):
    assert app.test_client().get('/api/palette?q=abc').status_code == 401
    c = app.test_client(); login(c); mk_customer(c); mk_ticket(c, subject='Loan disbursement delayed')
    assert c.get('/api/palette?q=l').get_json() == {'tickets': [], 'customers': []}               # too short to search
    d = c.get('/api/palette?q=disburse').get_json()
    assert [t['subject'] for t in d['tickets']] == ['Loan disbursement delayed'] and d['tickets'][0]['url'] == '/tickets/1'
    assert c.get('/api/palette?q=TKT-0000').get_json()['tickets'][0]['number'].startswith('TKT-')    # by ticket number
    d = c.get('/api/palette?q=Amina').get_json()                                                   # by customer name
    assert d['customers'] and d['customers'][0]['url'].startswith('/customers/') and d['tickets']
    assert c.get('/api/palette?q=' + 'x' * 500).status_code == 200                                 # oversized input is capped, not an error
    assert c.get("/api/palette?q=%27%3B%20DROP%20TABLE%20tickets%3B--").get_json() == {'tickets': [], 'customers': []}


def test_palette_and_preview_respect_what_an_agent_may_see(app):
    agent = mk_agent(app, depts=(1,))                                       # Support only
    c = app.test_client(); login(c); mk_customer(c)
    mk_ticket(c, subject='Secret collections matter', department_id=2, assigned_user_id=1)
    mk_ticket(c, subject='Visible support matter', department_id=1, assigned_user_id=agent, confirm_duplicate='1')
    a = app.test_client(); login(a, 'daniel@x.com', 'Passw0rd!')
    subjects = [t['subject'] for t in a.get('/api/palette?q=matter').get_json()['tickets']]
    assert subjects == ['Visible support matter']                           # the other department's ticket is not leaked
    assert a.get('/tickets/1/peek').status_code == 403
    assert a.get('/tickets/2/peek').status_code == 200
    assert c.get('/tickets/999/peek').status_code == 404
    assert app.test_client().get('/tickets/1/peek').status_code in (302, 401, 403)


def test_peek_shows_the_facts_an_agent_needs(app):
    c = app.test_client(); login(c); mk_customer(c); mk_ticket(c, subject='Wrong repayment amount')
    post(c, '/tickets/1/comment', '/tickets/1', comment_type='INTERNAL', body='Checked the ledger, amount is off by 5,000')
    html = c.get('/tickets/1/peek').get_data(as_text=True)
    assert 'Wrong repayment amount' in html and 'TKT-00001' in html and 'Checked the ledger' in html
    assert 'Internal note' in html and 'Open ticket' in html and '<html' not in html           # a fragment, not a full page


def test_brand_accent_presets_custom_colours_and_safety(app):
    c = app.test_client(); login(c)
    assert '<style id="brand">' not in c.get('/').get_data(as_text=True)                       # default look: nothing injected
    post(c, '/settings/general', '/settings?tab=general', company_name='Bolga', accent='#0f766e', auto_assign='on')
    page = c.get('/').get_data(as_text=True)
    assert '<style id="brand">' in page and '--accent:#0f766e' in page and 'color-mix' in page
    post(c, '/settings/general', '/settings?tab=general', company_name='Bolga', accent='#0f766e', accent_custom='#7a1fa2')
    assert '--accent:#7a1fa2' in c.get('/').get_data(as_text=True)                              # a custom value wins over the preset
    assert b'too light' in post(c, '/settings/general', '/settings?tab=general', company_name='Bolga', accent_custom='#ffee00').data
    assert b'six hexadecimal' in post(c, '/settings/general', '/settings?tab=general', company_name='Bolga', accent_custom='red').data
    evil = post(c, '/settings/general', '/settings?tab=general', company_name='Bolga', accent_custom='#000;}</style><script>x</script>')
    assert b'six hexadecimal' in evil.data and b'<script>x</script>' not in c.get('/').data
    with app.app_context():                                                                    # even a tampered DB value cannot inject CSS
        from app.models import CompanySetting
        db.session.query(CompanySetting).filter_by(key='accent').one().value = '#000;}</style><script>alert(1)</script>'
        db.session.commit()
    page = c.get('/').get_data(as_text=True)
    assert '<script>alert(1)</script>' not in page and '<style id="brand">' not in page
    post(c, '/settings/general', '/settings?tab=general', company_name='Bolga', accent='#1d5bf0')
    assert '<style id="brand">' not in c.get('/').get_data(as_text=True)                       # back to the default


def test_every_preset_meets_accessible_contrast():
    from app.branding import PRESETS, contrast_with_white, MIN_CONTRAST, normalize_accent
    assert len(PRESETS) >= 6
    for name, hex_ in PRESETS:
        assert contrast_with_white(hex_) >= MIN_CONTRAST, name
        assert normalize_accent(hex_) == (hex_, None)
    assert normalize_accent('')[0] == '#1d5bf0' and normalize_accent('0F766E')[0] == '#0f766e'


def test_shell_has_palette_toasts_nav_groups_and_permissioned_commands(app):
    import json, re
    agent = mk_agent(app)
    c = app.test_client(); login(c)
    page = c.get('/').get_data(as_text=True)
    for needle in ('id="palette"', 'id="toasts"', 'id="peek"', 'Workspace', 'Insight', 'id="navToggle"', 'js/ui.js'):
        assert needle in page
    cmds = json.loads(re.search(r'id="palette-commands">(.*?)</script>', page, re.S).group(1))
    labels = [x['label'] for x in cmds]
    assert 'Administration' in labels and 'Create ticket' in labels and 'Reports' in labels
    a = app.test_client(); login(a, 'daniel@x.com', 'Passw0rd!')
    labels = [x['label'] for x in json.loads(re.search(r'id="palette-commands">(.*?)</script>', a.get('/').get_data(as_text=True), re.S).group(1))]
    assert 'Administration' not in labels and 'Reports' not in labels and 'My scorecard' in labels   # agents never see admin commands


def test_empty_states_are_helpful(app):
    c = app.test_client(); login(c)
    assert 'No tickets match this view' in c.get('/tickets').get_data(as_text=True)
    assert 'No promises here' in c.get('/promises').get_data(as_text=True)
    assert 'You are all caught up' in c.get('/').get_data(as_text=True)


# ───────────── Bolga branding, My day, queue board, conversation page ─────────────
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64


def upload_logo(c, data, name='logo.png', tile=None):
    form = {'_csrf': token(c, '/settings?tab=general'), 'logo': (io.BytesIO(data), name)}
    if tile:
        form['logo_tile'] = tile
    return c.post('/settings/branding/logo', data=form, content_type='multipart/form-data', follow_redirects=True)


def test_bolga_preset_brands_the_app_and_the_sign_in_page(app):
    c = app.test_client(); login(c)
    assert 'Bolga' not in c.get('/').get_data(as_text=True).split('<nav aria-label="Main">')[0]
    post(c, '/settings/branding/bolga', '/settings?tab=general')
    page = c.get('/').get_data(as_text=True)
    assert '<b>Bolga</b><small>Communications</small>' in page and 'viewBox="-300 -300 520 600"' in page
    assert '--accent:#1a5a9d' in page and 'rel="icon"' in page
    anon = app.test_client().get('/login').get_data(as_text=True)
    assert 'Service Beyond Expectations' in anon and 'viewBox="-300 -300 520 600"' in anon and '--accent:#1a5a9d' in anon
    assert 'Admin@123' not in anon and 'demo administrator' not in anon          # no default credentials advertised
    fav = app.test_client().get('/brand/favicon.svg')
    assert fav.mimetype == 'image/svg+xml' and b'#1a5a9d' in fav.data
    post(c, '/settings/branding/mark', '/settings?tab=general', mark='letter')
    assert 'viewBox="-300 -300 520 600"' not in c.get('/').get_data(as_text=True)         # can switch back to a letter mark


def test_uploaded_logo_is_validated_served_safely_and_removable(app):
    c = app.test_client(); login(c)
    assert app.test_client().get('/brand/logo').status_code == 404                    # nothing uploaded yet
    assert b'Logo saved' in upload_logo(c, PNG, tile='accent').data
    r = app.test_client().get('/brand/logo')                                          # public: the sign-in page needs it
    assert r.status_code == 200 and r.mimetype == 'image/png' and r.data == PNG
    assert r.headers['X-Content-Type-Options'] == 'nosniff' and 'sandbox' in r.headers['Content-Security-Policy']
    assert app.test_client().get('/brand/logo', headers={'If-None-Match': r.headers['ETag']}).status_code == 304
    page = c.get('/').get_data(as_text=True)
    assert '/brand/logo?v=' in page and 'mark-img tile-accent' in page
    assert '/brand/logo?v=' in app.test_client().get('/login').get_data(as_text=True)
    bad = {b'': b'Choose an image', b'hello world': b'PNG, JPG, WebP or SVG',
           PNG + b'x' * 1_000_001: b'larger than 1 MB',
           b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>': b'contains scripts',
           b'<svg xmlns="http://www.w3.org/2000/svg" onload="x()"></svg>': b'contains scripts',
           b'<svg xmlns="http://www.w3.org/2000/svg"><image href="http://evil.example/x.png"/></svg>': b'contains scripts'}
    for data, msg in bad.items():
        assert msg in upload_logo(c, data, 'x.svg' if data.startswith(b'<svg') else 'x.png').data, data[:30]
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4" fill="#1a5a9d"/></svg>'
    assert b'Logo saved' in upload_logo(c, svg, 'logo.svg').data
    r = app.test_client().get('/brand/logo')
    assert r.mimetype == 'image/svg+xml' and r.data == svg and "default-src 'none'" in r.headers['Content-Security-Policy']
    post(c, '/settings/branding/logo/remove', '/settings?tab=general')
    assert app.test_client().get('/brand/logo').status_code == 404 and 'mark-img' not in c.get('/').get_data(as_text=True)


def test_only_administrators_change_branding(app):
    mk_agent(app)
    a = app.test_client(); login(a, 'daniel@x.com', 'Passw0rd!')
    assert post(a, '/settings/branding/bolga', '/').status_code == 403
    assert upload_logo(a, PNG).status_code == 403
    assert post(a, '/settings/branding/mark', '/', mark='bolga').status_code == 403


def test_agents_land_on_my_day_and_managers_on_the_overview(app):
    mk_agent(app)
    a = app.test_client()
    r = a.post('/login', data={'email': 'daniel@x.com', 'password': 'Passw0rd!', '_csrf': token(a)})
    assert r.status_code == 302 and r.headers['Location'].endswith('/my-day')
    m = app.test_client()
    r = m.post('/login', data={'email': 'admin@baidesk.local', 'password': 'Admin@123', '_csrf': token(m)})
    assert r.headers['Location'].endswith('/')


def test_my_day_shows_what_needs_the_agent_now(app):
    from app.models import Ticket
    agent = mk_agent(app)
    c = app.test_client(); login(c); mk_customer(c)
    mk_ticket(c, subject='Late payment query', assigned_user_id=agent, department_id=1)
    mk_ticket(c, subject='Another different thing', assigned_user_id=agent, department_id=1, confirm_duplicate='1')
    with app.app_context():
        db.session.get(Ticket, 1).due_at = utcnow() - timedelta(hours=3)
        t2 = db.session.get(Ticket, 2); t2.status_id = 2; t2.sla_paused_at = utcnow()       # Pending → waiting on customer
        db.session.commit()
    a = app.test_client(); login(a, 'daniel@x.com', 'Passw0rd!')
    page = a.get('/my-day').get_data(as_text=True)
    assert 'Up next' in page and 'Late payment query' in page and 'overdue' in page
    assert 'Needs you now' in page and 'Waiting on customers' in page and 'Another different thing' in page
    assert '2</b><small>of 15' in page or '<b>2</b>' in page
    assert 'Available' in page and 'Resolved today' in page


def test_my_day_empty_state_and_claiming_from_the_queue(app):
    from app.models import Ticket
    agent = mk_agent(app, cap=1)
    c = app.test_client(); login(c); mk_customer(c); admin_away(app)
    post(c, '/settings/general', '/settings?tab=general', company_name='BaiDesk')          # auto-assign off, so tickets wait
    a = app.test_client(); login(a, 'daniel@x.com', 'Passw0rd!')
    assert 'All clear' in a.get('/my-day').get_data(as_text=True)
    assert 'Nothing is waiting' in post(a, '/my-day/claim', '/my-day').get_data(as_text=True)
    mk_ticket(c, subject='Waiting in queue', department_id=1); mk_ticket(c, subject='Second one zzz qqq', department_id=1, confirm_duplicate='1', priority_id=4)
    page = a.get('/my-day').get_data(as_text=True)
    assert 'Unassigned — yours to claim' in page and 'Take the next waiting ticket' in page
    r = a.post('/my-day/claim', data={'_csrf': token(a, '/my-day')})
    assert r.status_code == 302 and '/tickets/2' in r.headers['Location']                   # the urgent one is pulled first
    with app.app_context():
        assert db.session.get(Ticket, 2).assigned_user_id == agent
    assert 'at your capacity' in post(a, '/my-day/claim', '/my-day').get_data(as_text=True)   # capacity 1 reached


def test_queue_board_is_for_managers_and_shows_people_and_waiting_work(app):
    from app.models import Ticket
    a1 = mk_agent(app, 'Amy One', 'amy@x.com', cap=2)
    mk_agent(app, 'Bea Away', 'bea@x.com', away=True)
    c = app.test_client(); login(c); mk_customer(c); admin_away(app)
    post(c, '/settings/general', '/settings?tab=general', company_name='BaiDesk')
    mk_ticket(c, subject='Held by Amy', assigned_user_id=a1, department_id=1)
    mk_ticket(c, subject='Nobody has this yet', department_id=1, confirm_duplicate='1', priority_id=4)
    with app.app_context():
        db.session.get(Ticket, 1).due_at = utcnow() - timedelta(minutes=20)
        db.session.commit()
    page = c.get('/board').get_data(as_text=True)
    assert 'Waiting to be assigned' in page and 'Nobody has this yet' in page and 'draggable="true"' in page
    assert 'Amy One' in page and 'Bea Away' in page and 'state-away' in page and '1 late' in page
    assert 'Available' in page and 'Past deadline' in page
    ag = app.test_client(); login(ag, 'amy@x.com', 'Passw0rd!')
    assert ag.get('/board').status_code == 403
    assert app.test_client().get('/board').status_code in (302, 401, 403)


def test_team_leads_see_only_their_own_agents_on_the_board(app):
    from app.models import Team, User, Role
    a1 = mk_agent(app, 'Amy One', 'amy@x.com'); a2 = mk_agent(app, 'Ben Two', 'ben@x.com')
    with app.app_context():
        lead = User(name='Lead One', email='l1@x.com'); lead.set_password('Passw0rd!')
        db.session.add(lead); lead.roles = [Role.query.filter_by(name='Manager / Team Lead').one()]
        t = Team(name='T1'); db.session.add(t); t.members = [lead, db.session.get(User, a1)]; t.lead = lead
        db.session.commit()
    l = app.test_client(); login(l, 'l1@x.com', 'Passw0rd!')
    page = l.get('/board').get_data(as_text=True)
    assert 'Amy One' in page and 'Ben Two' not in page


def test_assign_endpoint_for_drag_and_drop(app):
    from app.models import Ticket
    inside = mk_agent(app, 'Inside Dept', 'in@x.com', depts=(1,))
    outside = mk_agent(app, 'Outside Dept', 'out@x.com', depts=(2,))
    c = app.test_client(); login(c); mk_customer(c); admin_away(app)
    post(c, '/settings/general', '/settings?tab=general', company_name='BaiDesk')
    mk_ticket(c, subject='Drag me', department_id=1)
    def drop(uid):
        return c.post('/tickets/1/assign', data={'_csrf': token(c, '/board'), 'user_id': uid}, headers={'X-Requested-With': 'fetch'})
    r = drop(inside)
    assert r.status_code == 200 and r.get_json()['ok'] and 'assigned to Inside Dept' in r.get_json()['message']
    with app.app_context():
        assert db.session.get(Ticket, 1).assigned_user_id == inside
    assert drop(outside).get_json()['ok']                                                   # an administrator may cross departments
    drop(inside)
    from app.models import User, Role, Department
    with app.app_context():                                                                 # a team lead may not
        lead = User(name='Lena Lead', email='lena@x.com'); lead.set_password('Passw0rd!')
        db.session.add(lead); lead.roles = [Role.query.filter_by(name='Manager / Team Lead').one()]
        lead.departments = Department.query.filter(Department.id.in_((1, 2))).all(); db.session.commit()
    lead = app.test_client(); login(lead, 'lena@x.com', 'Passw0rd!')
    r = lead.post('/tickets/1/assign', data={'_csrf': token(lead, '/board'), 'user_id': outside}, headers={'X-Requested-With': 'fetch'})
    assert r.status_code == 400 and not r.get_json()['ok'] and 'does not belong' in r.get_json()['message']
    with app.app_context():
        assert db.session.get(Ticket, 1).assigned_user_id == inside
        db.session.get(User, outside).is_active = False; db.session.commit()
    assert 'Inactive' in c.post('/tickets/1/assign', data={'_csrf': token(c, '/board'), 'user_id': outside}, headers={'X-Requested-With': 'fetch'}).get_json()['message']
    r = drop('')                                                                            # drop back on the queue = release
    assert r.get_json()['ok'] and 'released' in r.get_json()['message']
    from app import routing
    with app.app_context():
        assert db.session.get(Ticket, 1).assigned_user_id is None
        assert routing.drain_queue() == 0                                                   # a deliberate release is respected
    assert 'No change' in drop('').get_json()['message']
    ag = app.test_client(); login(ag, 'in@x.com', 'Passw0rd!')
    assert ag.get('/board').status_code == 403                                              # the board is for managers
    assert ag.post('/tickets/1/assign', data={'_csrf': token(ag, '/my-day'), 'user_id': inside}).status_code == 302   # agents keep the same assign right as the ticket form
    assert app.test_client().post('/tickets/1/assign', data={'user_id': inside}).status_code in (302, 400, 401, 403)
    drop('')                                                                                # (the agent's call above had assigned it)
    page = post(c, '/tickets/1/assign', '/board', user_id=inside).get_data(as_text=True)    # the no-JavaScript form path
    assert 'assigned to Inside Dept' in page


def test_ticket_page_reads_like_a_conversation(app):
    agent = mk_agent(app)
    c = app.test_client(); login(c); mk_customer(c); mk_ticket(c, subject='Wrong repayment amount', assigned_user_id=agent, department_id=1)
    post(c, '/tickets/1/comment', '/tickets/1', comment_type='PUBLIC', body='Hello, we are checking this now')
    post(c, '/tickets/1/comment', '/tickets/1', comment_type='INTERNAL', body='Ledger shows 5,000 difference')
    post(c, '/tickets/1/comment', '/tickets/1', comment_type='CUSTOMER', body='Please hurry, I am travelling')
    post(c, '/tickets/1/contact', '/tickets/1', channel='Call', direction='Outbound', notes='Called and confirmed the amount')
    post(c, '/tickets/1/update', '/tickets/1', status_id=3, priority_id=3)
    page = c.get('/tickets/1').get_data(as_text=True)
    assert 'class="tk-head"' in page and 'id="stream"' in page and 'data-filter="note"' in page
    assert 'Original request' in page and 'it msg in' in page and 'it msg out' in page            # customer left, agent right
    assert 'class="it note"' in page and 'Internal note · ' in page and 'Ledger shows' in page
    assert 'Please hurry' in page and 'Customer reply' in page
    assert 'data-kind="contact"' in page and 'Call · Outbound' in page
    assert 'data-kind="event"' in page and 'changed priority' in page and '>Today<' in page       # system events + day divider
    assert page.index('Hello, we are checking') < page.index('Ledger shows') < page.index('Please hurry')   # oldest first
    assert 'id="replyForm"' in page and 'Ctrl' in page and 'Full audit history' in page and 'Take it' in page and 'Resolve' in page
    post(c, '/tickets/1/update', '/tickets/1', status_id=5, priority_id=3)
    assert '>Resolve</button>' not in c.get('/tickets/1').get_data(as_text=True).split('class="ticket-layout"')[0]    # finished tickets: no Resolve
    mine = app.test_client(); login(mine, 'daniel@x.com', 'Passw0rd!')
    assert 'Take it' not in mine.get('/tickets/1').get_data(as_text=True)                          # already the owner
