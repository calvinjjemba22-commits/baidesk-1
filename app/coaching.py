"""Agent scorecards and review samples. Everything is computed from tickets, history and promises."""
import hashlib
from datetime import timedelta
from sqlalchemy import func
from .extensions import db
from .models import Ticket, TicketHistory, TicketReview, PaymentPromise, User, Role, TicketStatus

DONE = ('Resolved', 'Closed')


def _avg(values):
    values = list(values)
    return sum(values) / len(values) if values else None


def scorecard(user, start, end):
    """Metrics for tickets *assigned to* the user and resolved in [start, end)."""
    resolved = Ticket.query.filter(Ticket.assigned_user_id == user.id, Ticket.resolved_at >= start,
                                   Ticket.resolved_at < end).all()
    frt = [(t.first_response_at - t.created_at).total_seconds() / 60 for t in resolved
           if t.first_response_at and t.first_response_at >= t.created_at]
    res = [(t.resolved_at - t.created_at).total_seconds() / 3600 for t in resolved]
    judged = [t for t in resolved if t.due_at]
    met = sum(1 for t in judged if t.resolved_at <= t.due_at)
    # reopened = moved from Resolved/Closed back into an active status, on this agent's tickets
    ids = [t.id for t in Ticket.query.filter(Ticket.assigned_user_id == user.id).with_entities(Ticket.id).all()]
    reopened = 0
    if ids:
        reopened = (TicketHistory.query.filter(TicketHistory.ticket_id.in_(ids), TicketHistory.field_name == 'status_id',
                                               TicketHistory.old_value.in_(DONE), ~TicketHistory.new_value.in_(DONE),
                                               TicketHistory.created_at >= start, TicketHistory.created_at < end).count())
    kept = PaymentPromise.query.filter(PaymentPromise.user_id == user.id, PaymentPromise.status == 'Kept',
                                       PaymentPromise.closed_at >= start, PaymentPromise.closed_at < end).count()
    broken = PaymentPromise.query.filter(PaymentPromise.user_id == user.id, PaymentPromise.status == 'Broken',
                                         PaymentPromise.closed_at >= start, PaymentPromise.closed_at < end).count()
    scores = [r.score for r in TicketReview.query.filter(TicketReview.agent_id == user.id,
                                                         TicketReview.created_at >= start,
                                                         TicketReview.created_at < end).all()]
    return {
        'user': user, 'resolved': len(resolved),
        'avg_first_response_min': _avg(frt), 'avg_resolution_h': _avg(res),
        'sla_met_pct': round(met / len(judged) * 100) if judged else None,
        'reopened': reopened,
        'promises_kept': kept, 'promises_broken': broken,
        'promise_rate': round(kept / (kept + broken) * 100) if kept + broken else None,
        'reviews': len(scores), 'avg_score': round(sum(scores) / len(scores), 1) if scores else None,
    }


def team(start, end):
    roles = User.query.filter(User.is_active.is_(True)).order_by(User.name).all()
    agents = [u for u in roles if u.has_permission('ticket.edit') and not u.is_admin()] or roles
    return [scorecard(u, start, end) for u in agents]


def review_samples(user, start, end, n=3):
    """Up to n resolved tickets that nobody has reviewed yet. The pick is stable for a given
    agent + period so the list doesn't reshuffle on every page load."""
    reviewed = db.select(TicketReview.ticket_id)
    pool = (Ticket.query.filter(Ticket.assigned_user_id == user.id, Ticket.resolved_at >= start,
                                Ticket.resolved_at < end, ~Ticket.id.in_(reviewed)).all())
    seed = f'{user.id}:{start:%Y%m%d}:{end:%Y%m%d}'
    pool.sort(key=lambda t: hashlib.sha1(f'{seed}:{t.id}'.encode()).hexdigest())
    return pool[:n]
