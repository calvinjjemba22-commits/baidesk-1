"""Quality safety nets: duplicate-ticket detection and queue navigation."""
import re
from datetime import timedelta
from difflib import SequenceMatcher
from sqlalchemy import or_
from .models import Ticket, TicketStatus
from .services import ACTIVE_SLUGS
from .timeutil import utcnow

DUPLICATE_WINDOW_DAYS = 14
_WORD = re.compile(r'[a-z0-9]+')


def _tokens(text):
    return {w for w in _WORD.findall((text or '').lower()) if len(w) > 2}


def similarity(a, b):
    """0..1. Shared words decide; raw character similarity only counts when it is near-identical (typos, plurals),
    because unrelated short subjects share plenty of letters."""
    a, b = (a or '').strip().lower(), (b or '').strip().lower()
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta, tb = _tokens(a), _tokens(b)
    jac = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    return max(jac, ratio if ratio >= 0.8 else 0.0)


def find_duplicates(customer_id, subject, exclude_id=None, threshold=0.6):
    """Recent open tickets from the same customer that look like the one being created."""
    since = utcnow() - timedelta(days=DUPLICATE_WINDOW_DAYS)
    q = Ticket.query.filter(Ticket.customer_id == customer_id, Ticket.created_at >= since,
                            Ticket.status.has(TicketStatus.slug.in_(ACTIVE_SLUGS)))
    if exclude_id:
        q = q.filter(Ticket.id != exclude_id)
    hits = [(similarity(subject, t.subject), t) for t in q.all()]
    return [t for score, t in sorted(hits, key=lambda x: -x[0]) if score >= threshold][:3]


def next_ticket_for(user, exclude_id=None):
    """The most urgent active ticket waiting for this person: their own first, then unassigned in their departments."""
    active = Ticket.status.has(TicketStatus.slug.in_(ACTIVE_SLUGS))
    dept_ids = [d.id for d in user.departments]
    mine = or_(Ticket.assigned_user_id == user.id,
               (Ticket.assigned_user_id.is_(None) & Ticket.department_id.in_(dept_ids)) if dept_ids else False)
    q = Ticket.query.filter(active, mine, Ticket.sla_paused_at.is_(None))
    if exclude_id:
        q = q.filter(Ticket.id != exclude_id)
    # own tickets before unassigned ones, then soonest deadline (no deadline last)
    return q.order_by(Ticket.assigned_user_id.is_(None), Ticket.due_at.is_(None), Ticket.due_at.asc()).first()


def customer_context(ticket):
    """Everything an agent wants to know about the person on the other end, without leaving the ticket."""
    from .models import Interaction, PaymentPromise
    cid = ticket.customer_id
    active = Ticket.status.has(TicketStatus.slug.in_(ACTIVE_SLUGS))
    other_open = (Ticket.query.filter(Ticket.customer_id == cid, Ticket.id != ticket.id, active)
                  .order_by(Ticket.updated_at.desc()).all())
    since = utcnow() - timedelta(days=30)
    repeat = Ticket.query.filter(Ticket.customer_id == cid, Ticket.id != ticket.id, Ticket.created_at >= since).count()
    recent = (Interaction.query.filter_by(customer_id=cid).order_by(Interaction.interaction_at.desc()).limit(3).all())
    promises = (PaymentPromise.query.filter_by(customer_id=cid)
                .order_by(PaymentPromise.promised_date.desc()).limit(5).all())
    return {'other_open': other_open, 'repeat_30d': repeat, 'recent_interactions': recent, 'promises': promises,
            'lifetime_tickets': Ticket.query.filter_by(customer_id=cid).count()}


def next_unassigned_for(user):
    """The most urgent unassigned, running ticket in this person's departments (the one they can 'claim')."""
    from sqlalchemy import or_ as _or
    from .models import Priority
    active = Ticket.status.has(TicketStatus.slug.in_(ACTIVE_SLUGS))
    dept_ids = [d.id for d in user.departments]
    q = Ticket.query.filter(active, Ticket.assigned_user_id.is_(None), Ticket.sla_paused_at.is_(None))
    if dept_ids:
        q = q.filter(_or(Ticket.department_id.in_(dept_ids), Ticket.department_id.is_(None)))
    return (q.join(Priority, Ticket.priority_id == Priority.id)
            .order_by(Priority.sort_order.desc(), Ticket.due_at.is_(None), Ticket.due_at.asc(), Ticket.id).first())
