"""Operations features: contact logging, auto-assign, promises, saved views, coaching, availability."""
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode
from flask import render_template, request, redirect, url_for, flash, abort
from .extensions import db
from .models import (Ticket, Customer, Interaction, PaymentPromise, SavedView, TicketReview, User,
                     TicketHistory)
from .services import current_user, log_activity, notify, audit
from .timeutil import utcnow, local_today, local_midnight_utc, parse_date
from .routes import (main, guard, require_login, get_or_404, safe_next, change_assignee, user_can_access_ticket,
                     CHANNELS, DIRECTIONS)
from . import routing, coaching


def _back(default):
    return redirect(safe_next(request.form.get('next'), default))


# ───────────────────────── log a call / SMS / WhatsApp on a ticket ─────────────────────────
@main.route('/tickets/<int:ticket_id>/contact', methods=['POST'])
def ticket_contact(ticket_id):
    r = guard('interaction.create')
    if r:
        return r
    t = get_or_404(Ticket, ticket_id)
    if not user_can_access_ticket(t):
        abort(403)
    me = current_user()
    channel, direction = request.form.get('channel', ''), request.form.get('direction', '')
    notes = request.form.get('notes', '').strip()
    if channel not in CHANNELS or direction not in DIRECTIONS:
        abort(400)
    if not notes:
        flash('Add a short note about the conversation.', 'error')
        return redirect(url_for('main.ticket_detail', ticket_id=t.id) + '#conversation')
    x = Interaction(customer_id=t.customer_id, ticket_id=t.id, user_id=me.id, channel=channel, direction=direction,
                    disposition=request.form.get('disposition') or None, subject=t.subject, notes=notes,
                    interaction_at=utcnow())
    db.session.add(x)
    if direction == 'Outbound' and not t.first_response_at:
        t.first_response_at = utcnow()          # reaching out to the customer counts as the first response
    t.updated_at = utcnow()
    log_activity('INTERACTION_LOGGED', f'{channel} {direction.lower()} logged on {t.ticket_number}.',
                 customer_id=t.customer_id, ticket_id=t.id, entity_type='Interaction', department_id=t.department_id)
    db.session.commit()
    flash(f'{channel} {direction.lower()} logged.', 'success')
    return redirect(url_for('main.ticket_detail', ticket_id=t.id) + '#conversation')


# ───────────────────────── auto-assign ─────────────────────────
@main.route('/tickets/<int:ticket_id>/auto-assign', methods=['POST'])
def ticket_auto_assign(ticket_id):
    r = guard('ticket.assign')
    if r:
        return r
    t = get_or_404(Ticket, ticket_id)
    if not user_can_access_ticket(t):
        abort(403)
    agent, why = routing.pick(routing.department_for(t.department_id, t.category_id), t.category_id)
    if not agent:
        flash('No agent is available right now (everyone is away or at capacity). The ticket stays in the queue '
              'and is assigned automatically when someone frees up.', 'error')
    else:
        routing.assign(t, agent, why, actor_id=current_user().id)
        db.session.commit()
        flash(f'Assigned to {agent.name} ({why.lower()}).', 'success')
    return redirect(url_for('main.ticket_detail', ticket_id=t.id))


@main.route('/profile/availability', methods=['POST'])
def profile_availability():
    r = require_login()
    if r:
        return r
    me = current_user()
    me.is_available = not me.is_available
    audit('AVAILABILITY_CHANGED', 'User', me.id, 'available' if me.is_available else 'away')
    db.session.commit()
    if me.is_available:
        routing.drain_queue()          # waiting tickets go to whoever is back, including this person
    flash('You are now available for new tickets.' if me.is_available else
          'You are marked away — new tickets will not be auto-assigned to you.', 'success')
    return _back(url_for('main.dashboard'))


# ───────────────────────── promises ─────────────────────────
PROMISE_ACTIONS = {'kept': 'Kept', 'broken': 'Broken', 'cancel': 'Cancelled'}


@main.route('/promises')
def promises():
    r = guard('customer.view')
    if r:
        return r
    me = current_user()
    scope = request.args.get('scope', 'all' if me.is_manager() else 'mine')
    if scope == 'all' and not me.is_manager():
        scope = 'mine'
    tab = request.args.get('tab', 'due')
    today = local_today()
    q = PaymentPromise.query
    if scope == 'mine':
        q = q.filter(PaymentPromise.user_id == me.id)
    base = q
    if tab == 'due':
        q = q.filter(PaymentPromise.status == 'Pending', PaymentPromise.promised_date <= today)
    elif tab == 'upcoming':
        q = q.filter(PaymentPromise.status == 'Pending', PaymentPromise.promised_date > today)
    elif tab in ('kept', 'broken'):
        q = q.filter(PaymentPromise.status == tab.title())
    else:
        tab = 'all'
    order = PaymentPromise.promised_date.asc() if tab in ('due', 'upcoming') else PaymentPromise.created_at.desc()
    rows = q.order_by(order, PaymentPromise.id).limit(300).all()
    month_ago = utcnow() - timedelta(days=30)
    kept = base.filter(PaymentPromise.status == 'Kept', PaymentPromise.closed_at >= month_ago).count()
    broken = base.filter(PaymentPromise.status == 'Broken', PaymentPromise.closed_at >= month_ago).count()
    pending = base.filter(PaymentPromise.status == 'Pending')
    stats = {'due': pending.filter(PaymentPromise.promised_date <= today).count(),
             'upcoming': pending.filter(PaymentPromise.promised_date > today).count(),
             'kept_rate': round(kept / (kept + broken) * 100) if kept + broken else None,
             'amount_due': sum(p.amount or 0 for p in pending.filter(PaymentPromise.promised_date <= today).all())}
    return render_template('promises.html', rows=rows, tab=tab, scope=scope, stats=stats, today=today)


@main.route('/promises/new', methods=['POST'])
def promise_new():
    r = guard('interaction.create')
    if r:
        return r
    me = current_user()
    c = get_or_404(Customer, request.form.get('customer_id', type=int) or 0)
    ticket = None
    if request.form.get('ticket_id', type=int):
        ticket = get_or_404(Ticket, request.form.get('ticket_id', type=int))
        if not user_can_access_ticket(ticket):
            abort(403)
    back = url_for('main.ticket_detail', ticket_id=ticket.id) if ticket else url_for('main.customer_detail', customer_id=c.id)
    when = parse_date(request.form.get('promised_date'))
    amount_raw = request.form.get('amount', '').replace(',', '').strip()
    if not when or when < local_today() - timedelta(days=1) or when > local_today() + timedelta(days=365):
        flash('Choose the date the customer promised to pay (today or later).', 'error')
        return redirect(back)
    if amount_raw and (not amount_raw.isdigit() or int(amount_raw) > 10 ** 10):
        flash('Amount must be a whole number.', 'error')
        return redirect(back)
    p = PaymentPromise(customer_id=c.id, ticket_id=ticket.id if ticket else None, user_id=me.id,
                       amount=int(amount_raw) if amount_raw else None, promised_date=when,
                       note=request.form.get('note', '').strip()[:300] or None)
    db.session.add(p)
    log_activity('PROMISE_RECORDED', f'Promise to pay by {when:%d %b %Y} recorded.', customer_id=c.id,
                 ticket_id=ticket.id if ticket else None, entity_type='Promise')
    db.session.commit()
    flash('Promise recorded. You will be reminded on the day.', 'success')
    return redirect(back)


@main.route('/promises/<int:pid>/<action>', methods=['POST'])
def promise_action(pid, action):
    r = guard('interaction.create')
    if r:
        return r
    p = get_or_404(PaymentPromise, pid)
    me = current_user()
    if action not in PROMISE_ACTIONS:
        abort(400)
    if p.user_id != me.id and not me.is_manager():
        abort(403)
    if p.status != 'Pending':
        flash('That promise is already closed.', 'info')
    else:
        p.status, p.closed_at = PROMISE_ACTIONS[action], utcnow()
        log_activity('PROMISE_' + p.status.upper(), f'Promise of {p.promised_date:%d %b} marked {p.status.lower()}.',
                     customer_id=p.customer_id, ticket_id=p.ticket_id, entity_type='Promise', entity_id=p.id)
        db.session.commit()
        flash(f'Marked {p.status.lower()}.', 'success')
    return _back(url_for('main.promises'))


# ───────────────────────── saved ticket views ─────────────────────────
VIEW_KEYS = ('view', 'q', 'status', 'priority', 'department', 'assignee', 'sort', 'dir')


@main.route('/tickets/views/save', methods=['POST'])
def saved_view_save():
    r = guard('ticket.view')
    if r:
        return r
    me = current_user()
    name = request.form.get('name', '').strip()[:60]
    if not name:
        flash('Give the view a name.', 'error')
        return _back(url_for('main.tickets'))
    pairs = [(k, v) for k, v in parse_qsl(request.form.get('query', '')) if k in VIEW_KEYS and v]
    shared = 'is_shared' in request.form and me.is_manager()
    existing = SavedView.query.filter_by(user_id=me.id, name=name).first()
    if existing:
        existing.filters, existing.is_shared = urlencode(pairs), shared
    elif SavedView.query.filter_by(user_id=me.id).count() >= 20:
        flash('You can keep up to 20 saved views. Delete one first.', 'error')
        return _back(url_for('main.tickets'))
    else:
        db.session.add(SavedView(user_id=me.id, name=name, filters=urlencode(pairs), is_shared=shared))
    db.session.commit()
    flash(f'View “{name}” saved.', 'success')
    return redirect(url_for('main.tickets') + ('?' + urlencode(pairs) if pairs else ''))


@main.route('/tickets/views/<int:vid>/delete', methods=['POST'])
def saved_view_delete(vid):
    r = guard('ticket.view')
    if r:
        return r
    v = get_or_404(SavedView, vid)
    me = current_user()
    if v.user_id != me.id and not me.is_admin():
        abort(403)
    db.session.delete(v)
    db.session.commit()
    flash('View removed.', 'success')
    return redirect(url_for('main.tickets'))


# ───────────────────────── coaching ─────────────────────────
def _visible_agent_ids(me):
    """None = everyone. Admins and managers without a team see everyone; a team lead sees only their team."""
    if me.is_admin() or not me.team:
        return None
    return {u.id for u in me.team.members}


def _period():
    days = request.args.get('days', 30, type=int)
    days = days if days in (7, 30, 90) else 30
    return days, local_midnight_utc(-(days - 1)), local_midnight_utc(1)


@main.route('/coaching')
def coaching_index():
    r = require_login()
    if r:
        return r
    me = current_user()
    if not me.is_manager():
        return redirect(url_for('main.coaching_agent', uid=me.id))
    days, start, end = _period()
    cards = coaching.team(start, end)
    ids = _visible_agent_ids(me)
    if ids is not None:
        cards = [c for c in cards if c['user'].id in ids and c['user'].id != me.id]
    return render_template('coaching.html', cards=cards, days=days)


@main.route('/coaching/<int:uid>')
def coaching_agent(uid):
    r = require_login()
    if r:
        return r
    me = current_user()
    if uid != me.id and not me.is_manager():
        abort(403)
    agent = get_or_404(User, uid)
    ids = _visible_agent_ids(me)
    if uid != me.id and ids is not None and uid not in ids:
        abort(403)
    days, start, end = _period()
    reviews = (TicketReview.query.filter_by(agent_id=agent.id).order_by(TicketReview.created_at.desc()).limit(10).all())
    return render_template('coaching_agent.html', agent=agent, card=coaching.scorecard(agent, start, end), days=days,
                           samples=coaching.review_samples(agent, start, end) if me.is_manager() else [],
                           reviews=reviews, can_review=me.is_manager())


@main.route('/coaching/review', methods=['POST'])
def coaching_review():
    r = guard('reports.view')
    if r:
        return r
    me = current_user()
    t = get_or_404(Ticket, request.form.get('ticket_id', type=int) or 0)
    score = request.form.get('score', type=int)
    ids = _visible_agent_ids(me)
    if ids is not None and t.assigned_user_id not in ids:
        abort(403)
    if not t.assigned_user_id or score not in (1, 2, 3, 4, 5):
        flash('Choose a score from 1 to 5.', 'error')
    elif TicketReview.query.filter_by(ticket_id=t.id).first():
        flash('That ticket has already been reviewed.', 'info')
    else:
        db.session.add(TicketReview(ticket_id=t.id, agent_id=t.assigned_user_id, reviewer_id=me.id, score=score,
                                    notes=request.form.get('notes', '').strip()[:1000] or None))
        notify(t.assigned_user_id, 'review', 'Ticket reviewed',
               f'{me.name} reviewed {t.ticket_number}: {score}/5.', 'Ticket', t.id)
        db.session.commit()
        flash('Review saved.', 'success')
    return _back(url_for('main.coaching_index'))
