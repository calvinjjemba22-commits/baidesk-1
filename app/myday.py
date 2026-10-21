"""'My day': everything one person needs to work through their shift, in the order they should do it."""
from datetime import timedelta
from sqlalchemy import or_
from .models import Ticket, TicketStatus, TicketComment, PaymentPromise
from .services import ACTIVE_SLUGS
from .exports import TICKET_LOAD
from .timeutil import local_midnight_utc, local_today
from . import quality, sla
from .dashboard_data import sla_left, fmt_duration

SOON = timedelta(hours=2)


def _reason(t, now):
    """Why this ticket deserves attention, as (tone, short text)."""
    if sla.is_late(t, now):
        label, _ = sla_left(t.due_at, now)
        return 'red', label
    if t.due_at and not t.sla_paused_at and t.due_at <= now + SOON:
        label, _ = sla_left(t.due_at, now)
        return 'amber', label
    if t.priority.sort_order >= 2:
        return 'amber', f'{t.priority.name} priority'
    return 'muted', 'Next in your queue'


def build(user, now):
    active = Ticket.status.has(TicketStatus.slug.in_(ACTIVE_SLUGS))
    mine = Ticket.query.filter(Ticket.assigned_user_id == user.id, active).options(*TICKET_LOAD).all()
    today0 = local_midnight_utc(0)

    def rank(t):
        late = sla.is_late(t, now)
        soon = bool(t.due_at and not t.sla_paused_at and not late and t.due_at <= now + SOON)
        return (0 if late else 1 if soon else 2 if t.priority.sort_order >= 2 else 3,
                t.due_at is None, t.due_at or now)

    running = [t for t in mine if not t.sla_paused_at]
    needs_now = []
    for t in sorted(running, key=rank):
        tone, text = _reason(t, now)
        if tone == 'muted':
            break
        needs_now.append({'t': t, 'tone': tone, 'text': text})
    waiting_customer = [t for t in mine if t.sla_paused_at]

    last = {}
    if mine:
        for c in TicketComment.query.filter(TicketComment.ticket_id.in_([t.id for t in mine])).order_by(TicketComment.id):
            last[c.ticket_id] = c
    replied = [t for t in mine if last.get(t.id) is not None and last[t.id].comment_type == 'CUSTOMER']

    nxt = quality.next_ticket_for(user)
    up_next = None
    if nxt:
        tone, text = _reason(nxt, now)
        up_next = {'t': nxt, 'tone': tone, 'text': text, 'unassigned': nxt.assigned_user_id is None}

    resolved = Ticket.query.filter(Ticket.assigned_user_id == user.id, Ticket.resolved_at >= today0).all()
    frt = [(t.first_response_at - t.created_at).total_seconds() for t in
           Ticket.query.filter(Ticket.assigned_user_id == user.id, Ticket.first_response_at >= today0).all()
           if t.first_response_at >= t.created_at]

    today = local_today()
    promises = (PaymentPromise.query.filter(PaymentPromise.user_id == user.id, PaymentPromise.status == 'Pending',
                                            PaymentPromise.promised_date <= today)
                .order_by(PaymentPromise.promised_date, PaymentPromise.id).all())

    cap = max(1, user.capacity or 1)
    dept_ids = [d.id for d in user.departments]
    waiting_pool = Ticket.query.filter(active, Ticket.assigned_user_id.is_(None))
    if dept_ids:
        waiting_pool = waiting_pool.filter(or_(Ticket.department_id.in_(dept_ids), Ticket.department_id.is_(None)))
    return {
        'load': len(mine), 'capacity': cap, 'load_pct': min(100, round(len(mine) / cap * 100)),
        'over_capacity': len(mine) > cap, 'full': len(mine) >= cap,
        'needs_now': needs_now[:6], 'needs_now_total': len(needs_now),
        'replied': replied[:5], 'waiting_customer': waiting_customer[:5], 'waiting_customer_total': len(waiting_customer),
        'promises': promises[:6], 'promises_total': len(promises),
        'up_next': up_next, 'unassigned_waiting': waiting_pool.count(),
        'resolved_today': len(resolved), 'avg_first_response': fmt_duration(sum(frt) / len(frt)) if frt else '—',
        'overdue_count': sum(1 for t in mine if sla.is_late(t, now)),
    }
