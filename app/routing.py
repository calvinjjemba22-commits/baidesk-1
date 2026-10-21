"""Auto-assignment by department and need.

For a ticket the pool is: active, available agents in the ticket's department (the category's department when
none was chosen) who are still under their capacity. Within the pool we prefer, in this order:

  1. specialists  — agents who listed the ticket's category among their skills
  2. generalists  — agents with no skills listed (they take anything in their department)
  3. anyone else in the department

and inside each group the agent with the lowest load (active tickets ÷ capacity) wins. Tickets that find nobody
stay in the queue and are handed out by `drain_queue()` as agents come back or free up capacity.
"""
from sqlalchemy import func
from .extensions import db
from .models import User, Ticket, TicketStatus, TicketHistory, CompanySetting, Category, Priority
from .services import ACTIVE_SLUGS


def auto_assign_enabled():
    row = CompanySetting.query.filter_by(key='auto_assign').first()
    return (row.value if row else 'on') == 'on'


def active_loads():
    """{user_id: number of active tickets}"""
    rows = (db.session.query(Ticket.assigned_user_id, func.count(Ticket.id))
            .filter(Ticket.assigned_user_id.isnot(None),
                    Ticket.status.has(TicketStatus.slug.in_(ACTIVE_SLUGS)))
            .group_by(Ticket.assigned_user_id).all())
    return dict(rows)


def eligible_agents(dept_id=None):
    q = User.query.filter(User.is_active.is_(True), User.is_available.is_(True))
    if dept_id:
        q = q.filter(User.departments.any(id=dept_id))
    return [u for u in q.order_by(User.id).all() if u.has_permission('ticket.view') and u.has_permission('ticket.edit')]


def department_for(dept_id, category_id):
    """The ticket's department, or the category's home department when none was chosen."""
    if dept_id:
        return dept_id
    cat = db.session.get(Category, category_id) if category_id else None
    return cat.department_id if cat else None


def pick(dept_id=None, category_id=None, exclude_ids=(), loads=None):
    """(agent, reason) — agent is None when everybody is away or at capacity."""
    loads = loads if loads is not None else active_loads()
    tiers = ([], [], [])
    for u in eligible_agents(dept_id):
        if u.id in exclude_ids:
            continue
        load, cap = loads.get(u.id, 0), max(1, u.capacity or 1)
        if load >= cap:
            continue
        skills = {c.id for c in u.skills}
        tier = 0 if (not category_id or category_id in skills) else 1 if not skills else 2
        tiers[tier].append(((load / cap, load, u.id), u))
    reasons = ('Matched on skill and department' if category_id else 'Least-loaded in department',
               'Generalist with the lowest load', 'Only agent with free capacity')
    for tier, rows in enumerate(tiers):
        if rows:
            return min(rows, key=lambda r: r[0])[1], reasons[tier]
    return None, None


def pick_agent(dept_id=None, category_id=None, exclude_ids=()):
    return pick(dept_id, category_id, exclude_ids)[0]


def assign(ticket, agent, reason, actor_id=None):
    """Assign + audit trail. Imported lazily because routes imports this module."""
    from .routes import change_assignee
    actor = db.session.get(User, actor_id or ticket.created_by) or agent
    if not ticket.department_id:
        ticket.department_id = department_for(None, ticket.category_id)
    changed = change_assignee(ticket, agent.id, actor)
    if changed:
        db.session.add(TicketHistory(ticket_id=ticket.id, user_id=actor.id, action_type='AUTO_ASSIGNED',
                                     field_name='assigned_user_id', new_value=agent.name, reason=reason))
    return changed


def drain_queue(limit=100):
    """Hand waiting (unassigned) tickets to whoever can take them now, most urgent first.
    Returns the number of tickets assigned. Safe to call often."""
    if not auto_assign_enabled():
        return 0
    # A ticket someone deliberately set back to "Unassigned" is left for people to hand out; the
    # "Assign to best available agent" button and the bulk action still work on it.
    released = db.select(TicketHistory.ticket_id).where(TicketHistory.field_name == 'assigned_user_id',
                                                        TicketHistory.new_value == 'Unassigned')
    waiting = (Ticket.query.filter(Ticket.assigned_user_id.is_(None), Ticket.sla_paused_at.is_(None),
                                   ~Ticket.id.in_(released),
                                   Ticket.status.has(TicketStatus.slug.in_(ACTIVE_SLUGS)))
               .join(Priority, Ticket.priority_id == Priority.id)
               .order_by(Priority.sort_order.desc(), Ticket.due_at.is_(None), Ticket.due_at.asc(), Ticket.id)
               .limit(limit).all())
    if not waiting:
        return 0
    loads, done = active_loads(), 0
    for t in waiting:
        dept = department_for(t.department_id, t.category_id)
        agent, reason = pick(dept, t.category_id, loads=loads)
        if not agent:
            continue
        if assign(t, agent, reason):
            loads[agent.id] = loads.get(agent.id, 0) + 1
            done += 1
    if done:
        db.session.commit()
    return done
