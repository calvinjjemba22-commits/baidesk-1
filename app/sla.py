"""SLA clock: deadlines that respect business hours and holidays, and pause while we wait on the customer.

All stored datetimes are naive UTC. Business hours are configured in the display timezone (DISPLAY_TZ), so
"08:00-17:00" means 08:00-17:00 Kampala time.
"""
from datetime import datetime, timedelta, time, timezone
from .extensions import db
from .models import (SLASettings, BusinessHour, Holiday, Ticket, TicketStatus, User, Role, AlertSent,
                     PaymentPromise)
from .timeutil import utcnow, get_tz, to_local, local_today

PAUSE_SLUGS = ('pending',)          # statuses that mean "waiting on the customer"
AT_RISK_MINUTES = 30                # alert this long before a deadline
_MAX_DAYS = 3650


class Calendar:
    """Working windows per weekday + holiday dates, loaded once per calculation."""

    def __init__(self):
        self.days = {}
        for row in BusinessHour.query.all():
            self.days[row.day_of_week] = (row.is_working_day, _t(row.start_time, '08:00'), _t(row.end_time, '17:00'))
        self.holidays = {h.date for h in Holiday.query.all()}
        # If nothing is configured as a working day, fall back to wall-clock time instead of looping forever.
        self.usable = any(v[0] and v[1] < v[2] for v in self.days.values())

    def window(self, d):
        """(start, end) local times for date d, or None on a day off / holiday."""
        if d in self.holidays:
            return None
        working, start, end = self.days.get(d.weekday(), (d.weekday() < 5, time(8), time(17)))
        return (start, end) if working and start < end else None


def _t(value, default):
    try:
        h, m = (value or default).split(':')
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        h, m = default.split(':')
        return time(int(h), int(m))


def _local(dt_utc):
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(get_tz())


def _utc(dt_local):
    return dt_local.astimezone(timezone.utc).replace(tzinfo=None)


def add_minutes(start_utc, minutes, business_only=True, cal=None):
    """The UTC moment `minutes` of SLA time after `start_utc`."""
    if minutes <= 0:
        return start_utc
    cal = cal or Calendar()
    if not business_only or not cal.usable:
        return start_utc + timedelta(minutes=minutes)
    tz = get_tz()
    cur = _local(start_utc)
    remaining = timedelta(minutes=minutes)
    for _ in range(_MAX_DAYS):
        win = cal.window(cur.date())
        if win:
            open_at = datetime.combine(cur.date(), win[0], tzinfo=tz)
            close_at = datetime.combine(cur.date(), win[1], tzinfo=tz)
            if cur < open_at:
                cur = open_at
            if cur < close_at:
                free = close_at - cur
                if remaining <= free:
                    return _utc(cur + remaining)
                remaining -= free
        cur = datetime.combine(cur.date() + timedelta(days=1), time.min, tzinfo=tz)
    return start_utc + timedelta(minutes=minutes)


def business_minutes_between(a_utc, b_utc, business_only=True, cal=None):
    """SLA minutes that elapsed between two moments (0 if b <= a)."""
    if b_utc <= a_utc:
        return 0
    cal = cal or Calendar()
    if not business_only or not cal.usable:
        return int((b_utc - a_utc).total_seconds() // 60)
    tz = get_tz()
    a, b = _local(a_utc), _local(b_utc)
    total = timedelta()
    day = a.date()
    for _ in range(_MAX_DAYS):
        if day > b.date():
            break
        win = cal.window(day)
        if win:
            lo = max(a, datetime.combine(day, win[0], tzinfo=tz))
            hi = min(b, datetime.combine(day, win[1], tzinfo=tz))
            if hi > lo:
                total += hi - lo
        day += timedelta(days=1)
    return int(total.total_seconds() // 60)


# ───────────────────────── ticket-level helpers ─────────────────────────
def rule_for(priority_id):
    return SLASettings.query.filter_by(priority_id=priority_id, active=True).first()


def compute_deadlines(ticket, cal=None):
    """(first_response_due, resolution_due) for the ticket's current priority, including paused time."""
    rule = rule_for(ticket.priority_id)
    paused = ticket.sla_paused_minutes or 0
    if not rule:
        return None, ticket.created_at + timedelta(minutes=1440 + paused)
    cal = cal or Calendar()
    biz = bool(rule.business_hours_only)
    first = add_minutes(ticket.created_at, rule.first_response_minutes + paused, biz, cal)
    res = add_minutes(ticket.created_at, rule.resolution_minutes + paused, biz, cal)
    return first, res


def apply_deadlines(ticket):
    ticket.first_response_due_at, ticket.due_at = compute_deadlines(ticket)


def on_status_change(ticket, new_slug, now=None):
    """Pause the clock when waiting on the customer; resume (and push the deadline out) otherwise."""
    now = now or utcnow()
    if new_slug in PAUSE_SLUGS:
        if not ticket.sla_paused_at:
            ticket.sla_paused_at = now
        return
    if ticket.sla_paused_at:
        rule = rule_for(ticket.priority_id)
        biz = bool(rule.business_hours_only) if rule else True
        ticket.sla_paused_minutes = (ticket.sla_paused_minutes or 0) + business_minutes_between(
            ticket.sla_paused_at, now, biz)
        ticket.sla_paused_at = None
        apply_deadlines(ticket)


def is_late(ticket, now=None):
    """Overdue and the clock is running (a paused ticket is never 'late')."""
    now = now or utcnow()
    return bool(ticket.due_at and ticket.due_at < now and not ticket.sla_paused_at)


# ───────────────────────── automatic alerts ─────────────────────────
_last_run = {'t': None}
WATCH_EVERY = timedelta(seconds=90)


def _notify(user_id, title, message, ticket_id):
    from .services import notify
    notify(user_id, 'sla', title, message, 'Ticket', ticket_id)


def _manager_ids():
    return [u.id for u in User.query.filter(User.is_active.is_(True),
            User.roles.any(Role.name.in_(('Admin', 'Manager / Team Lead')))).all()]


def _first_time(kind, entity_id):
    """True the first time this (kind, entity) is seen; records it so we never alert twice."""
    if AlertSent.query.filter_by(kind=kind, entity_id=entity_id).first():
        return False
    db.session.add(AlertSent(kind=kind, entity_id=entity_id))
    return True


def watch(force=False):
    """Raise notifications for tickets about to breach / already breached and for promises due today.
    Cheap and throttled; called whenever the browser polls for unread notifications."""
    now = utcnow()
    if not force and _last_run['t'] and now - _last_run['t'] < WATCH_EVERY:
        return 0
    _last_run['t'] = now
    from . import routing
    routing.drain_queue()                # keep the queue moving even when nobody touches a ticket
    from .services import ACTIVE_SLUGS
    raised = 0
    active = Ticket.status.has(TicketStatus.slug.in_(ACTIVE_SLUGS))
    soon = now + timedelta(minutes=AT_RISK_MINUTES)
    rows = (Ticket.query.filter(active, Ticket.sla_paused_at.is_(None), Ticket.due_at.isnot(None),
                                Ticket.due_at < soon).order_by(Ticket.due_at).limit(200).all())
    managers = None
    for t in rows:
        if t.due_at < now:
            if _first_time('breached', t.id):
                managers = managers if managers is not None else _manager_ids()
                for uid in set(managers + ([t.assigned_user_id] if t.assigned_user_id else [])):
                    _notify(uid, 'SLA breached', f'{t.ticket_number} is past its deadline.', t.id)
                raised += 1
        elif _first_time('at_risk', t.id):
            if t.assigned_user_id:
                _notify(t.assigned_user_id, 'SLA at risk',
                        f'{t.ticket_number} is due within {AT_RISK_MINUTES} minutes.', t.id)
            raised += 1
    today = local_today()
    for p in PaymentPromise.query.filter(PaymentPromise.status == 'Pending',
                                         PaymentPromise.promised_date <= today).limit(200).all():
        if _first_time('promise_due', p.id):
            what = f'{p.customer.first_name} {p.customer.last_name}'.strip()
            from .services import notify
            notify(p.user_id, 'promise', 'Promise due', f'{what} promised to pay by {p.promised_date:%d %b}. '
                   'Confirm whether it was kept.', 'Ticket' if p.ticket_id else None, p.ticket_id)
            raised += 1
    if raised:
        db.session.commit()
    return raised
