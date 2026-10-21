"""'What changed?' in plain language — rule-based, computed from your own tickets (no external service)."""
from collections import Counter
from datetime import timedelta
from sqlalchemy import or_
from .models import Ticket, TicketStatus, Category, Department, PaymentPromise, User
from .services import ACTIVE_SLUGS
from .timeutil import local_midnight_utc, to_local, utcnow


def _pct(now, before):
    if not before:
        return None
    return round((now - before) / before * 100)


def _top_change(rows_now, rows_before, label_of):
    """Which label grew the most between two lists of tickets."""
    a, b = Counter(label_of(t) for t in rows_now), Counter(label_of(t) for t in rows_before)
    deltas = {k: a[k] - b[k] for k in set(a) | set(b) if k}
    if not deltas:
        return None
    k = max(deltas, key=deltas.get)
    return (k, deltas[k]) if deltas[k] > 0 else None


def weekly(base, now=None):
    """Compare the last 7 days with the 7 before. Returns a list of {'tone': good|bad|info, 'text': str}."""
    now = now or utcnow()
    w0 = now - timedelta(days=7)
    w1 = now - timedelta(days=14)
    cur = base.filter(Ticket.created_at >= w0).all()
    prev = base.filter(Ticket.created_at >= w1, Ticket.created_at < w0).all()
    out = []

    # 1. volume + what drove it
    if cur or prev:
        change = _pct(len(cur), len(prev))
        if change is None:
            out.append({'tone': 'info', 'text': f'{len(cur)} new tickets in the last 7 days (none in the week before).'})
        elif abs(change) >= 5:
            word = 'up' if change > 0 else 'down'
            text = f'Ticket volume is {word} {abs(change)}% on last week ({len(cur)} vs {len(prev)}).'
            if change > 0:
                cat = _top_change(cur, prev, lambda t: t.category.name if t.category else None)
                dep = _top_change(cur, prev, lambda t: t.department.name if t.department else None)
                bits = []
                if cat:
                    bits.append(f'{cat[0]} (+{cat[1]})')
                if dep:
                    bits.append(f'the {dep[0]} department (+{dep[1]})')
                if bits:
                    text += ' Most of the increase came from ' + ' and '.join(bits) + '.'
            out.append({'tone': 'bad' if change > 0 else 'good', 'text': text})
        else:
            out.append({'tone': 'info', 'text': f'Ticket volume is steady ({len(cur)} this week vs {len(prev)} last week).'})

    # 2. backlog direction
    resolved_cur = base.filter(Ticket.resolved_at >= w0).count()
    gap = len(cur) - resolved_cur
    if abs(gap) >= 5:
        out.append({'tone': 'bad' if gap > 0 else 'good',
                    'text': (f'The backlog grew by about {gap}: {len(cur)} tickets came in and {resolved_cur} were resolved.'
                             if gap > 0 else f'The backlog shrank by about {-gap}: {resolved_cur} resolved vs {len(cur)} new.')})

    # 3. speed of first reply
    def frt(rows):
        v = [(t.first_response_at - t.created_at).total_seconds() / 60 for t in rows if t.first_response_at]
        return sum(v) / len(v) if v else None
    a, b = frt(cur), frt(prev)
    if a is not None and b:
        ch = _pct(a, b)
        if ch is not None and abs(ch) >= 15:
            out.append({'tone': 'bad' if ch > 0 else 'good',
                        'text': f'First replies are {"slower" if ch > 0 else "faster"} — {a:.0f} min on average vs {b:.0f} min last week.'})

    # 4. where the late tickets are
    active = Ticket.status.has(TicketStatus.slug.in_(ACTIVE_SLUGS))
    late = base.filter(active, Ticket.sla_paused_at.is_(None), Ticket.due_at < now).all()
    if late:
        by_dept = Counter(t.department.name if t.department else 'no department' for t in late)
        by_user = Counter(t.assigned_user.name if t.assigned_user else 'nobody' for t in late)
        d, dn = by_dept.most_common(1)[0]
        u, un = by_user.most_common(1)[0]
        text = f'{len(late)} ticket{"s are" if len(late) != 1 else " is"} past deadline'
        text += f', {dn} in {d}' if len(by_dept) > 1 else f' (all in {d})'
        if un >= 2 and len(by_user) > 1:
            text += f', and {un} are held by {u}' if u != 'nobody' else f', and {un} have no owner'
        out.append({'tone': 'bad', 'text': text + '.'})
    unassigned = base.filter(active, Ticket.assigned_user_id.is_(None)).count()
    if unassigned:
        out.append({'tone': 'bad' if unassigned >= 5 else 'info',
                    'text': f'{unassigned} open ticket{"s have" if unassigned != 1 else " has"} no owner yet.'})

    # 5. busiest day
    if len(cur) >= 10:
        days = Counter(to_local(t.created_at).strftime('%A') for t in cur)
        d, n = days.most_common(1)[0]
        out.append({'tone': 'info', 'text': f'{d} was the busiest day, with {n} new tickets.'})

    # 6. promises
    kept = PaymentPromise.query.filter(PaymentPromise.status == 'Kept', PaymentPromise.closed_at >= w0).count()
    broken = PaymentPromise.query.filter(PaymentPromise.status == 'Broken', PaymentPromise.closed_at >= w0).count()
    if kept + broken:
        rate = round(kept / (kept + broken) * 100)
        out.append({'tone': 'good' if rate >= 70 else 'bad',
                    'text': f'{rate}% of payment promises were kept this week ({kept} kept, {broken} broken).'})
    return out
