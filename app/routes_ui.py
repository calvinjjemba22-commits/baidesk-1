"""Endpoints that power the interface itself: command palette search and the ticket peek drawer."""
from flask import jsonify, render_template, request, url_for, abort
from sqlalchemy import or_
from .models import Ticket, Customer, TicketComment, Interaction
from .routes import main, require_login, allowed, department_scope, customer_filter, user_can_access_ticket, get_or_404, guard
from .timeutil import utcnow
from . import sla

PALETTE_LIMIT = 6


@main.route('/api/palette')
def palette_api():
    """Search for the command palette (Ctrl+K). Honours the same visibility rules as the normal pages."""
    if require_login():
        return jsonify(error='unauthorized'), 401
    q = request.args.get('q', '').strip()[:80]
    out = {'tickets': [], 'customers': []}
    if len(q) < 2:
        return jsonify(out)
    like = f'%{q}%'
    if allowed('ticket.view'):
        rows = (department_scope(Ticket.query)
                .filter(or_(Ticket.ticket_number.ilike(like), Ticket.subject.ilike(like),
                            Ticket.customer.has(or_(Customer.first_name.ilike(like), Customer.last_name.ilike(like)))))
                .order_by(Ticket.updated_at.desc()).limit(PALETTE_LIMIT).all())
        out['tickets'] = [{'id': t.id, 'number': t.ticket_number, 'subject': t.subject, 'status': t.status.name,
                           'priority': t.priority.name,
                           'customer': f'{t.customer.first_name} {t.customer.last_name}'.strip(),
                           'url': url_for('main.ticket_detail', ticket_id=t.id)} for t in rows]
    if allowed('customer.view'):
        rows = Customer.query.filter(customer_filter(q)).order_by(Customer.updated_at.desc()).limit(PALETTE_LIMIT).all()
        out['customers'] = [{'id': c.id, 'name': f'{c.first_name} {c.last_name}'.strip(), 'number': c.customer_number,
                             'phone': c.phones[0].phone_number if c.phones else '',
                             'url': url_for('main.customer_detail', customer_id=c.id)} for c in rows]
    return jsonify(out)


@main.route('/tickets/<int:ticket_id>/peek')
def ticket_peek(ticket_id):
    """A read-only summary shown in a side drawer so agents can scan the queue without opening every ticket."""
    r = guard('ticket.view')
    if r:
        return r
    t = get_or_404(Ticket, ticket_id)
    if not user_can_access_ticket(t):
        abort(403)
    comments = (TicketComment.query.filter_by(ticket_id=t.id).order_by(TicketComment.created_at.desc()).limit(3).all())
    contacts = Interaction.query.filter_by(ticket_id=t.id).order_by(Interaction.interaction_at.desc()).limit(2).all()
    return render_template('ticket_peek.html', t=t, comments=comments, contacts=contacts, running=not t.resolved_at
                           and not t.sla_paused_at and bool(t.due_at), late=sla.is_late(t, utcnow()))
