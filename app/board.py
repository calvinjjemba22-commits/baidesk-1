"""Live queue board for team leads: who can take work, who is overloaded, and what is waiting."""
from datetime import timedelta
from .models import Ticket, TicketStatus, User, Priority
from .services import ACTIVE_SLUGS
from .exports import TICKET_LOAD
from . import sla
from .dashboard_data import sla_left, fmt_duration

SOON = timedelta(hours=2)
QUEUE_LIMIT = 30
TOP_TICKETS = 3


def visible_agents(me):
    """Admins and team-less managers see every agent; a team lead sees their own team."""
    pool = [u for u in User.query.filter_by(is_active=True).order_by(User.name).all()
            if u.has_permission('ticket.edit') and not u.is_manager()]
    if me.is_admin() or not me.team:
        return pool
    ids = {m.id for m in me.team.members}
    return [u for u in pool if u.id in ids]


def _urgency(t, now):
    late = sla.is_late(t, now)
    soon = bool(t.due_at and not t.sla_paused_at and not late and t.due_at <= now + SOON)
    return (0 if late else 1 if soon else 2 if t.priority.sort_order >= 2 else 3, t.due_at is None, t.due_at or now)


def build(me, now, scope_query):
    """`scope_query` is Ticket.query already limited to what `me` may see."""
    active = Ticket.status.has(TicketStatus.slug.in_(ACTIVE_SLUGS))
    agents = visible_agents(me)
    ids = [a.id for a in agents]
    by_agent = {i: [] for i in ids}
    if ids:
        for t in scope_query.filter(active, Ticket.assigned_user_id.in_(ids)).options(*TICKET_LOAD).all():
            by_agent[t.assigned_user_id].append(t)

    cards = []
    for a in agents:
        ts = by_agent[a.id]
        cap = max(1, a.capacity or 1)
        late = [t for t in ts if sla.is_late(t, now)]
        risk = [t for t in ts if t.due_at and not t.sla_paused_at and not sla.is_late(t, now) and t.due_at <= now + SOON]
        top = []
        for t in sorted([t for t in ts if not t.sla_paused_at], key=lambda t: _urgency(t, now))[:TOP_TICKETS]:
            label, tone = sla_left(t.due_at, now)
            top.append({'t': t, 'label': label, 'tone': tone})
        state = 'away' if not a.is_available else 'full' if len(ts) >= cap else 'ok'
        cards.append({'u': a, 'load': len(ts), 'cap': cap, 'pct': min(100, round(len(ts) / cap * 100)), 'state': state,
                      'over': len(ts) > cap, 'late': len(late), 'risk': len(risk),
                      'paused': sum(1 for t in ts if t.sla_paused_at), 'top': top})

    waiting_q = scope_query.filter(active, Ticket.assigned_user_id.is_(None)).options(*TICKET_LOAD)
    waiting_total = waiting_q.count()
    waiting = (waiting_q.join(Priority, Ticket.priority_id == Priority.id)
               .order_by(Priority.sort_order.desc(), Ticket.due_at.is_(None), Ticket.due_at.asc(), Ticket.id)
               .limit(QUEUE_LIMIT).all())
    queue = []
    for t in waiting:
        label, tone = sla_left(t.due_at, now)
        waited = int((now - t.created_at).total_seconds() // 60)
        queue.append({'t': t, 'label': label, 'tone': tone,
                      'waited': f'{waited // 60}h {waited % 60}m' if waited >= 60 else f'{max(waited, 0)}m', 'waited_min': waited})

    recent = scope_query.filter(Ticket.first_response_at >= now - timedelta(hours=1)).all()
    frt = [(t.first_response_at - t.created_at).total_seconds() for t in recent if t.first_response_at >= t.created_at]
    free = [c for c in cards if c['state'] == 'ok']
    return {
        'cards': cards, 'queue': queue, 'waiting_total': waiting_total,
        'stats': {'available': len(free), 'away': sum(1 for c in cards if c['state'] == 'away'),
                  'full': sum(1 for c in cards if c['state'] == 'full'),
                  'free_slots': sum(c['cap'] - c['load'] for c in free),
                  'late': sum(c['late'] for c in cards) + sum(1 for q in queue if sla.is_late(q['t'], now)),
                  'longest_wait': max((q['waited'] for q in sorted(queue, key=lambda q: -q['waited_min'])[:1]), default='—'),
                  'avg_first_response': fmt_duration(sum(frt) / len(frt)) if frt else '—'},
    }
