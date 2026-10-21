"""Work surfaces: 'My day' for agents and the live queue board for team leads."""
from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from .extensions import db
from .models import Ticket, User
from .services import current_user
from .timeutil import utcnow, to_local, local_today
from .routes import (main, guard, require_login, get_or_404, department_scope, user_can_access_ticket,
                     assignee_error, change_assignee)
from . import myday, quality, routing
from . import board as board_mod


def _greeting():
    h = to_local(utcnow()).hour
    return 'Good morning' if h < 12 else 'Good afternoon' if h < 18 else 'Good evening'


@main.route('/my-day')
def my_day():
    r = guard('ticket.view')
    if r:
        return r
    me = current_user()
    return render_template('my_day.html', d=myday.build(me, utcnow()), greeting=_greeting(), today=local_today())


@main.route('/my-day/claim', methods=['POST'])
def claim_next():
    """Pull the most urgent unassigned ticket from the queue (the 'I can take more' button)."""
    r = guard('ticket.edit')
    if r:
        return r
    me = current_user()
    if routing.active_loads().get(me.id, 0) >= max(1, me.capacity or 1):
        flash(f'You are at your capacity of {me.capacity} tickets. Resolve one first, or ask your team lead.', 'error')
        return redirect(url_for('main.my_day'))
    t = quality.next_unassigned_for(me)
    if not t:
        flash('Nothing is waiting in your queue right now.', 'info')
        return redirect(url_for('main.my_day'))
    change_assignee(t, me.id, me)
    db.session.commit()
    flash(f'{t.ticket_number} is yours.', 'success')
    return redirect(url_for('main.ticket_detail', ticket_id=t.id))


@main.route('/board')
def queue_board():
    r = require_login()
    if r:
        return r
    me = current_user()
    if not me.is_manager():
        abort(403)
    data = board_mod.build(me, utcnow(), department_scope(Ticket.query))
    return render_template('board.html', can_assign=me.has_permission('ticket.assign'), **data)


@main.route('/tickets/<int:ticket_id>/assign', methods=['POST'])
def ticket_assign(ticket_id):
    """Assign (or release) a ticket. Used by the board's drag-and-drop and its dropdowns."""
    r = guard('ticket.assign')
    if r:
        return r
    me = current_user()
    t = get_or_404(Ticket, ticket_id)
    if not user_can_access_ticket(t):
        abort(403)
    uid = request.form.get('user_id', type=int)
    wants_json = request.headers.get('X-Requested-With') == 'fetch'
    err, msg = None, None
    if uid:
        err = assignee_error(uid, t.department_id, me)
    if not err:
        if change_assignee(t, uid, me):
            db.session.commit()
            who = db.session.get(User, uid) if uid else None
            msg = f'{t.ticket_number} assigned to {who.name}.' if who else f'{t.ticket_number} released to the queue.'
            if who and routing.active_loads().get(who.id, 0) > max(1, who.capacity or 1):
                msg += f' {who.name} is now over capacity.'
        else:
            msg = 'No change.'
    if wants_json:
        return jsonify(ok=not err, message=err or msg), (400 if err else 200)
    flash(err or msg, 'error' if err else 'success')
    return redirect(request.referrer or url_for('main.queue_board'))
