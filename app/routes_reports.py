"""Reports: date-filtered KPIs and breakdowns, with CSV export."""
from collections import defaultdict
from datetime import timedelta
from flask import render_template, request, abort
from .extensions import db
from .models import Ticket, TicketStatus, Priority, Department, SLASettings
from .services import ACTIVE_SLUGS, audit
from .routes import main, guard, department_scope, active_departments, is_active_status
from .exports import csv_response, TICKET_HEADERS, TICKET_LOAD, ticket_row, hours_between
from .timeutil import utcnow, to_local, local_today, local_date_to_utc, parse_date
from . import insights as insights_mod

MAX_RANGE_DAYS = 366


def _avg(values):
    return round(sum(values) / len(values), 2) if values else None


def _hours(a, b):
    return (b - a).total_seconds() / 3600


def report_params():
    today = local_today()
    d_to = parse_date(request.args.get('to')) or today
    d_from = parse_date(request.args.get('from')) or d_to - timedelta(days=29)
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    if (d_to - d_from).days >= MAX_RANGE_DAYS:
        d_from = d_to - timedelta(days=MAX_RANGE_DAYS - 1)
    dept_id = request.args.get('department', type=int)
    return {'d_from': d_from, 'd_to': d_to, 'dept_id': dept_id,
            'start': local_date_to_utc(d_from), 'end': local_date_to_utc(d_to + timedelta(days=1))}


def build_report(p):
    base = department_scope(Ticket.query)
    if p['dept_id']:
        base = base.filter(Ticket.department_id == p['dept_id'])
    base = base.options(*TICKET_LOAD)
    created = base.filter(Ticket.created_at >= p['start'], Ticket.created_at < p['end']
                          ).order_by(Ticket.created_at.asc(), Ticket.id.asc()).all()
    resolved = base.filter(Ticket.resolved_at >= p['start'], Ticket.resolved_at < p['end']).all()
    open_now = base.filter(is_active_status()).all()
    now = utcnow()
    overdue_now = [t for t in open_now if t.due_at and t.due_at < now and not t.sla_paused_at]

    sla_first = {s.priority_id: s.first_response_minutes
                 for s in SLASettings.query.filter_by(active=True).all()}
    res_hours = [_hours(t.created_at, t.resolved_at) for t in resolved]
    frt = [t for t in created if t.first_response_at]
    frt_hours = [_hours(t.created_at, t.first_response_at) for t in frt]
    res_met = [t for t in resolved if t.due_at and t.resolved_at <= t.due_at]
    res_judged = [t for t in resolved if t.due_at]
    frt_judged = [t for t in frt if t.priority_id in sla_first]
    frt_met = [t for t in frt_judged
               if t.first_response_at <= t.created_at + timedelta(minutes=sla_first[t.priority_id])]

    def pct(n, d):
        return round(100 * n / d, 1) if d else None

    kpis = {'created': len(created), 'resolved': len(resolved), 'open_now': len(open_now),
            'overdue_now': len(overdue_now), 'avg_first_response': _avg(frt_hours),
            'avg_resolution': _avg(res_hours), 'res_sla': pct(len(res_met), len(res_judged)),
            'frt_sla': pct(len(frt_met), len(frt_judged))}

    # status (current status of tickets created in range)
    by_status = defaultdict(int)
    for t in created:
        by_status[t.status_id] += 1
    status_rows = [[s.name, by_status.get(s.id, 0)] for s in TicketStatus.query.order_by(TicketStatus.sort_order)]

    # priority
    prios = Priority.query.order_by(Priority.sort_order).all()
    priority_rows = []
    for pr in prios:
        cr = [t for t in created if t.priority_id == pr.id]
        rs = [t for t in resolved if t.priority_id == pr.id]
        priority_rows.append([pr.name, len(cr), len(rs), _avg([_hours(t.created_at, t.resolved_at) for t in rs])])

    # department
    dept_rows = []
    names = [(d.id, d.name) for d in active_departments()] + [(None, 'Unassigned')]
    for did, name in names:
        cr = [t for t in created if t.department_id == did]
        rs = [t for t in resolved if t.department_id == did]
        op = [t for t in open_now if t.department_id == did]
        if cr or rs or op:
            dept_rows.append([name, len(cr), len(rs), _avg([_hours(t.created_at, t.resolved_at) for t in rs]), len(op)])

    # agent
    agents = {}
    def slot(t):
        key = t.assigned_user_id
        return agents.setdefault(key, {'name': t.assigned_user.name if t.assigned_user else 'Unassigned',
                                       'cr': [], 'rs': [], 'op': 0})
    for t in created:
        slot(t)['cr'].append(t)
    for t in resolved:
        slot(t)['rs'].append(t)
    for t in open_now:
        slot(t)['op'] += 1
    agent_rows = sorted(([a['name'], len(a['cr']), len(a['rs']),
                          _avg([_hours(t.created_at, t.resolved_at) for t in a['rs']]), a['op']]
                         for a in agents.values()), key=lambda r: (-r[1], r[0]))

    # category
    by_cat = defaultdict(int)
    for t in created:
        by_cat[t.category.name if t.category else 'Uncategorised'] += 1
    category_rows = sorted(([k, v] for k, v in by_cat.items()), key=lambda r: (-r[1], r[0]))

    # daily volume (local days)
    cdays, rdays = defaultdict(int), defaultdict(int)
    for t in created:
        cdays[to_local(t.created_at).date()] += 1
    for t in resolved:
        rdays[to_local(t.resolved_at).date()] += 1
    daily_rows, d = [], p['d_from']
    while d <= p['d_to']:
        daily_rows.append([d.isoformat(), cdays.get(d, 0), rdays.get(d, 0)])
        d += timedelta(days=1)

    datasets = [
        {'key': 'status', 'title': 'Tickets by status', 'note': 'Current status of tickets created in the period',
         'headers': ['Status', 'Tickets'], 'rows': status_rows},
        {'key': 'priority', 'title': 'By priority', 'note': 'Created and resolved in the period',
         'headers': ['Priority', 'Created', 'Resolved', 'Avg resolution (h)'], 'rows': priority_rows},
        {'key': 'department', 'title': 'By department', 'note': 'Open now reflects the current queue',
         'headers': ['Department', 'Created', 'Resolved', 'Avg resolution (h)', 'Open now'], 'rows': dept_rows},
        {'key': 'agent', 'title': 'By agent', 'note': 'Assigned = tickets created in the period',
         'headers': ['Agent', 'Assigned', 'Resolved', 'Avg resolution (h)', 'Open now'], 'rows': agent_rows},
        {'key': 'category', 'title': 'By category', 'note': 'Tickets created in the period',
         'headers': ['Category', 'Tickets'], 'rows': category_rows},
        {'key': 'daily', 'title': 'Daily volume', 'note': 'Created vs resolved per day',
         'headers': ['Date', 'Created', 'Resolved'], 'rows': daily_rows},
    ]
    return {'kpis': kpis, 'datasets': datasets, 'created': created, 'daily': daily_rows}


def _query_args(p):
    args = {'from': p['d_from'].isoformat(), 'to': p['d_to'].isoformat()}
    if p['dept_id']:
        args['department'] = p['dept_id']
    return args


@main.route('/reports')
def reports():
    r = guard('reports.view')
    if r:
        return r
    p = report_params()
    rep = build_report(p)
    today = local_today()
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    presets = [('Last 7 days', today - timedelta(days=6), today), ('Last 30 days', today - timedelta(days=29), today),
               ('Last 90 days', today - timedelta(days=89), today), ('This month', first_this, today),
               ('Last month', last_prev.replace(day=1), last_prev)]
    peak = max([1] + [max(row[1], row[2]) for row in rep['daily']])
    return render_template('reports.html', p=p, kpis=rep['kpis'], datasets=rep['datasets'], daily=rep['daily'],
                           peak=peak, presets=presets, dept_name=next((d.name for d in active_departments() if d.id == p['dept_id']), None), departments=active_departments(),
                           qargs=_query_args(p), ticket_count=len(rep['created']),
                           insights=insights_mod.weekly(department_scope(Ticket.query)))


@main.route('/reports/export/<kind>')
def report_export(kind):
    r = guard('reports.view')
    if r:
        return r
    p = report_params()
    rep = build_report(p)
    tag = f"{p['d_from'].isoformat()}_to_{p['d_to'].isoformat()}"
    by_key = {d['key']: d for d in rep['datasets']}
    if kind == 'tickets':
        sections = [(None, TICKET_HEADERS, [ticket_row(t) for t in rep['created']])]
    elif kind == 'summary':
        k = rep['kpis']
        sections = [(f'BaiDesk report {p["d_from"]} to {p["d_to"]}', ['Metric', 'Value'], [
            ['Tickets created', k['created']], ['Tickets resolved', k['resolved']],
            ['Open now', k['open_now']], ['Overdue now', k['overdue_now']],
            ['Avg first response (h)', k['avg_first_response']], ['Avg resolution (h)', k['avg_resolution']],
            ['Resolution SLA met (%)', k['res_sla']], ['First-response SLA met (%)', k['frt_sla']]])]
        sections += [(d['title'], d['headers'], d['rows']) for d in rep['datasets']]
    elif kind in by_key:
        d = by_key[kind]
        sections = [(None, d['headers'], d['rows'])]
    else:
        abort(404)
    audit('REPORT_EXPORTED', 'Report', None, f'{kind} {tag}')
    db.session.commit()
    return csv_response(f'baidesk-{kind}-{tag}.csv', sections)
