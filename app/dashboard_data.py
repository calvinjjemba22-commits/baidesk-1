"""Numbers behind the dashboard (KPI cards with trends, ticket volume, SLA health,
needs-attention list, department workload and recent activity).

Everything here is read-only. Historical points (sparklines, "vs yesterday") are rebuilt from the
created / resolved / closed / due timestamps, so a ticket that was reopened looks like it was open
the whole time. That is accurate enough for trend lines; the headline numbers always come from the
live status columns.
"""
from datetime import timedelta
from sqlalchemy import or_
from .models import Ticket, Priority, ActivityLog, Customer, Department
from .timeutil import local_midnight_utc, to_local, ago
from .exports import TICKET_LOAD

DAYS = 7                                # length of the volume chart and sparklines
AT_RISK_WINDOW = timedelta(hours=2)     # an active ticket due within this long is "at risk"
ATTENTION_LIMIT = 5
ACTIVITY_LIMIT = 4


# ───────────────────────── small formatters ─────────────────────────
def fmt_duration(seconds):
    """4m 32s · 1h 05m · 2d 3h. Used for first-response time."""
    if seconds is None:
        return '—'
    s = int(round(seconds))
    if s < 60:
        return f'{s}s'
    if s < 3600:
        return f'{s // 60}m {s % 60:02d}s'
    if s < 86400:
        return f'{s // 3600}h {(s % 3600) // 60:02d}m'
    return f'{s // 86400}d {(s % 86400) // 3600}h'


def sla_left(due_at, now):
    """('12m left', 'red') for the Needs-attention table."""
    if not due_at:
        return 'No deadline', 'muted'
    secs = int((due_at - now).total_seconds())
    if secs < 0:
        late = -secs
        label = (f'{late // 86400}d overdue' if late >= 86400 else
                 f'{late // 3600}h overdue' if late >= 3600 else f'{max(1, late // 60)}m overdue')
        return label, 'red'
    if secs < 3600:
        label = f'{max(1, secs // 60)}m left'
    elif secs < 86400:
        label = f'{secs // 3600}h {(secs % 3600) // 60}m left'
    else:
        label = f'{secs // 86400}d {(secs % 86400) // 3600}h left'
    tone = 'red' if secs < 1800 else 'amber' if secs < 3 * 3600 else 'muted'
    return label, tone


def delta(today, before, *, lower_is_better=False, neutral=False, unit='%'):
    """Trend chip data: {'text','dir','tone'}. tone ∈ good | bad | neutral."""
    if today is None or before is None:
        return {'text': 'No data yesterday', 'dir': 'flat', 'tone': 'neutral'}
    if unit == 'pts':                         # percentage-point change (SLA compliance)
        diff = round(today - before, 1)
        text = f'{abs(diff):g} pts'
    else:
        if before == 0:
            diff = today
            if today == 0:
                return {'text': 'No change vs yesterday', 'dir': 'flat', 'tone': 'neutral'}
            text = f'{abs(today):g} more' if today > 0 else f'{abs(today):g} fewer'
        else:
            diff = (today - before) / before * 100
            text = f'{abs(round(diff)):g}%'
    if not diff:
        return {'text': 'No change vs yesterday', 'dir': 'flat', 'tone': 'neutral'}
    up = diff > 0
    if neutral:
        tone = 'neutral'
    else:
        good = (not up) if lower_is_better else up
        tone = 'good' if good else 'bad'
    return {'text': f'{text} vs yesterday', 'dir': 'up' if up else 'down', 'tone': tone}


# ───────────────────────── trend series ─────────────────────────
def _series(base, now):
    """Per-day figures for the last DAYS local days (oldest → today), from one query."""
    starts = [local_midnight_utc(-i) for i in range(DAYS - 1, -1, -1)]
    ends = starts[1:] + [now]
    first = starts[0]
    rows = (base.with_entities(Ticket.created_at, Ticket.due_at, Ticket.resolved_at,
                               Ticket.closed_at, Ticket.first_response_at)
            .filter(or_(Ticket.resolved_at.is_(None), Ticket.resolved_at >= first,
                        Ticket.created_at >= first)).all())
    out = {k: [] for k in ('new', 'resolved', 'open', 'overdue', 'sla', 'frt')}
    for start, end in zip(starts, ends):
        open_then = [r for r in rows if r.created_at <= end
                     and (r.resolved_at is None or r.resolved_at > end)
                     and (r.closed_at is None or r.closed_at > end)]
        late = sum(1 for r in open_then if r.due_at and r.due_at < end)
        frts = [(r.first_response_at - r.created_at).total_seconds() for r in rows
                if r.first_response_at and start <= r.first_response_at < end]
        out['new'].append(sum(1 for r in rows if start <= r.created_at < end))
        out['resolved'].append(sum(1 for r in rows if r.resolved_at and start <= r.resolved_at < end))
        out['open'].append(len(open_then))
        out['overdue'].append(late)
        out['sla'].append(round((len(open_then) - late) / len(open_then) * 100, 1) if open_then else 100.0)
        out['frt'].append(sum(frts) / len(frts) if frts else None)
    out['labels'] = [f"{to_local(s).strftime('%b')} {to_local(s).day}" for s in starts]
    out['starts'] = starts
    return out


def _resolved_same_time_yesterday(base, now):
    """Resolved yesterday up to the same time of day, so the 'vs yesterday' comparison is fair."""
    today = local_midnight_utc(0)
    y0 = local_midnight_utc(-1)
    return base.filter(Ticket.resolved_at >= y0, Ticket.resolved_at < y0 + (now - today)).count()


# ───────────────────────── activity feed ─────────────────────────
def _activity_item(a, tickets, customers):
    t = tickets.get(a.ticket_id)
    num = t.ticket_number if t else ''
    who = a.actor.name if a.actor else 'System'
    kind = a.activity_type
    if kind == 'TICKET_STATUS_ID_CHANGED':
        new = a.description.split('→')[-1].strip().rstrip('.')
        if new.lower() in ('resolved', 'closed'):
            return dict(icon='check', tone='green', title=f'Ticket {new.lower()}',
                        detail=f'{num} · {t.subject}' if t else new)
        return dict(icon='refresh', tone='teal', title=f'Ticket {num}'.strip(),
                    detail=f'Status changed to {new}')
    if kind == 'TICKET_CREATED':
        return dict(icon='ticket', tone='blue', title='Ticket created',
                    detail=f'{num} · {t.subject}' if t else a.description)
    if kind == 'INTERNAL_NOTE_ADDED':
        return dict(icon='note', tone='purple', title='Internal note added', detail=f'By {who} on {num}')
    if kind == 'COMMENT_ADDED':
        return dict(icon='message', tone='blue', title='Reply sent', detail=f'By {who} on {num}')
    if kind == 'CUSTOMER_CREATED':
        c = customers.get(a.customer_id)
        name = f'{c.first_name} {c.last_name}'.strip() if c else a.description
        return dict(icon='user-plus', tone='blue', title='New customer registered', detail=name)
    if kind == 'TICKET_ASSIGNED_USER_CHANGED':
        return dict(icon='users', tone='teal', title=f'{num} reassigned'.strip(),
                    detail=a.description.split('→')[-1].strip().rstrip('.') or a.description)
    if kind == 'ESCALATED':
        return dict(icon='alert', tone='amber', title='Ticket escalated', detail=f'{num} · {a.description}')
    return dict(icon='clock', tone='blue', title=kind.replace('_', ' ').capitalize(), detail=a.description)


def _recent_activity(base, me):
    q = ActivityLog.query
    if not me.is_manager():
        visible = base.with_entities(Ticket.id)
        q = q.filter(or_(ActivityLog.ticket_id.in_(visible), ActivityLog.actor_user_id == me.id))
    rows = q.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc()).limit(ACTIVITY_LIMIT).all()
    ticket_ids = {a.ticket_id for a in rows if a.ticket_id}
    customer_ids = {a.customer_id for a in rows if a.customer_id}
    tickets = {t.id: t for t in Ticket.query.filter(Ticket.id.in_(ticket_ids)).all()} if ticket_ids else {}
    customers = {c.id: c for c in Customer.query.filter(Customer.id.in_(customer_ids)).all()} if customer_ids else {}
    items = []
    for a in rows:
        item = _activity_item(a, tickets, customers)
        item['ago'] = ago(a.created_at)
        item['href_ticket'] = a.ticket_id
        items.append(item)
    return items


# ───────────────────────── public entry point ─────────────────────────
def build(base, active, now, me):
    s = _series(base, now)
    today_start = local_midnight_utc(0)

    open_count = base.filter(active).count()
    running = Ticket.sla_paused_at.is_(None)          # a ticket waiting on the customer is not late
    overdue = base.filter(active, running, Ticket.due_at < now).count()
    at_risk = base.filter(active, running, Ticket.due_at >= now, Ticket.due_at < now + AT_RISK_WINDOW).count()
    on_track = max(0, open_count - overdue - at_risk)
    compliance = round((open_count - overdue) / open_count * 100, 1) if open_count else 100.0
    resolved_today = base.filter(Ticket.resolved_at >= today_start).count()
    resolved_before = _resolved_same_time_yesterday(base, now)

    frt_now, frt_before = s['frt'][-1], s['frt'][-2]
    # Yesterday's end-of-day figures are the baseline for "vs yesterday".
    kpis = {
        'open': dict(value=open_count, spark=s['open'],
                     delta=delta(open_count, s['open'][-2], neutral=True)),
        'overdue': dict(value=overdue, spark=s['overdue'],
                        delta=delta(overdue, s['overdue'][-2], lower_is_better=True)),
        'first_response': dict(value=fmt_duration(frt_now), spark=s['frt'],
                               delta=(delta(frt_now, frt_before, lower_is_better=True)
                                      if frt_now is not None else
                                      {'text': 'No replies yet today', 'dir': 'flat', 'tone': 'neutral'})),
        'sla': dict(value=f'{compliance:g}%', spark=s['sla'],
                    delta=delta(compliance, s['sla'][-2], unit='pts')),
        'resolved': dict(value=resolved_today, spark=s['resolved'],
                         delta=delta(resolved_today, resolved_before)),
    }

    volume = {'labels': s['labels'], 'new': s['new'], 'resolved': s['resolved']}
    sla = {'compliance': compliance, 'on_track': on_track, 'at_risk': at_risk,
           'breached': overdue, 'total': open_count}

    # Active tickets that are late, about to be late, or High/Urgent — soonest deadline first.
    urgent = Ticket.priority.has(Priority.sort_order >= 2)
    attention = (base.filter(active, or_(running & (Ticket.due_at < now + AT_RISK_WINDOW), urgent))
                 .options(*TICKET_LOAD)
                 .order_by(Ticket.due_at.is_(None), Ticket.due_at.asc())
                 .limit(ATTENTION_LIMIT).all())
    attention_rows = []
    for t in attention:
        label, tone = sla_left(t.due_at, now)
        attention_rows.append({'ticket': t, 'sla_label': label, 'sla_tone': tone})

    # Department workload: active tickets, share of everything open, and how many are overdue.
    workload = []
    for d in Department.query.filter_by(is_active=True).order_by(Department.name).all():
        n = base.filter(active, Ticket.department_id == d.id).count()
        late = base.filter(active, running, Ticket.department_id == d.id, Ticket.due_at < now).count()
        cap = d.capacity
        workload.append({'id': d.id, 'name': d.name, 'count': n, 'overdue': late, 'capacity': cap,
                         'left': (cap - n) if cap else None,
                         'share': round(n / open_count * 100) if open_count else 0,
                         # the bar fills against capacity when one is set, otherwise against all open tickets
                         'fill': min(100, round(n / cap * 100)) if cap else (round(n / open_count * 100) if open_count else 0),
                         'full': bool(cap and n >= cap)})
    workload.sort(key=lambda w: w['count'], reverse=True)

    return {'kpis': kpis, 'volume': volume, 'sla': sla, 'attention': attention_rows,
            'workload': workload, 'activity': _recent_activity(base, me)}
