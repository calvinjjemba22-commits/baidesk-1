"""Administration, profile and preferences."""
from flask import render_template, request, redirect, url_for, flash, abort, jsonify
from sqlalchemy import func
from .extensions import db
from .models import (User, Role, Department, Category, Priority, SLASettings, CannedResponse,
                     CompanySetting, AuditLog, BusinessHour, Holiday, Team)
from .timeutil import parse_date
from .services import current_user, audit
from .routes import (main, guard, require_login, get_or_404, paginate, MIN_PASSWORD)

SETTINGS_TABS = [('users', 'Users'), ('teams', 'Teams'), ('structure', 'Departments & categories'), ('sla', 'SLA'),
                 ('hours', 'Business hours'), ('canned', 'Canned responses'), ('general', 'General'),
                 ('data', 'Data & backup'), ('audit', 'Audit log')]
DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


def _back(tab):
    return redirect(url_for('main.settings', tab=tab))


def _active_admins(excluding=None):
    q = User.query.filter(User.is_active.is_(True), User.roles.any(Role.name == 'Admin'))
    if excluding:
        q = q.filter(User.id != excluding)
    return q.count()


@main.route('/settings')
def settings():
    r = guard('settings.manage')
    if r:
        return r
    tab = request.args.get('tab', 'users')
    if tab not in dict(SETTINGS_TABS):
        tab = 'users'
    ctx = {'tab': tab, 'tabs': SETTINGS_TABS}
    if tab == 'users':
        ctx.update(users=User.query.order_by(User.name).all(), roles=Role.query.order_by(Role.id).all(),
                   departments=Department.query.filter_by(is_active=True).order_by(Department.name).all())
    elif tab == 'structure':
        ctx.update(departments=Department.query.order_by(Department.name).all(),
                   categories=Category.query.order_by(Category.name).all())
    elif tab == 'sla':
        ctx['rows'] = [(p, SLASettings.query.filter_by(priority_id=p.id).first())
                       for p in Priority.query.order_by(Priority.sort_order).all()]
    elif tab == 'teams':
        ctx['teams'] = Team.query.order_by(Team.name).all()
        ctx['leaders'] = [u for u in User.query.filter_by(is_active=True).order_by(User.name).all() if u.is_manager()]
    elif tab == 'hours':
        have = {h.day_of_week: h for h in BusinessHour.query.all()}
        ctx['days'] = [(i, DAY_NAMES[i], have.get(i)) for i in range(7)]
        ctx['holidays'] = Holiday.query.order_by(Holiday.date).all()
    elif tab == 'canned':
        ctx['canned'] = CannedResponse.query.order_by(CannedResponse.title).all()
    elif tab == 'general':
        row = CompanySetting.query.filter_by(key='company_name').first()
        ctx['company'] = row.value if row else 'BaiDesk'
        from .routing import auto_assign_enabled
        ctx['auto_assign'] = auto_assign_enabled()
        from .branding import PRESETS
        acc = CompanySetting.query.filter_by(key='accent').first()
        ctx['accent'], ctx['accent_presets'] = (acc.value if acc else '#1d5bf0'), PRESETS
    elif tab == 'audit':
        page = paginate(AuditLog.query.order_by(AuditLog.id.desc()), per_page=50)
        ids = {a.user_id for a in page.items if a.user_id}
        ctx.update(page=page, names={u.id: u.name for u in User.query.filter(User.id.in_(ids)).all()} if ids else {})
    return render_template('settings.html', **ctx)


# ───────────── users ─────────────
@main.route('/settings/user', methods=['POST'])
def settings_user():
    r = guard('users.manage')
    if r:
        return r
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    if not name or not email:
        flash('Name and email are required.', 'error')
        return _back('users')
    if len(password) < MIN_PASSWORD:
        flash(f'Password must be at least {MIN_PASSWORD} characters.', 'error')
        return _back('users')
    if User.query.filter(func.lower(User.email) == email).first():
        flash('A user with that email already exists.', 'error')
        return _back('users')
    # Resolve related rows first; only then build the user and add it to the session.
    role = get_or_404(Role, request.form.get('role_id', type=int) or 0)
    dept_id = request.form.get('department_id', type=int)
    dept = get_or_404(Department, dept_id) if dept_id else None
    u = User(name=name, email=email)
    u.set_password(password)
    u.must_change_password = True      # an administrator chose this password, so the person must replace it
    db.session.add(u)
    u.roles = [role]
    u.departments = [dept] if dept else []
    db.session.flush()
    audit('USER_CREATED', 'User', u.id)
    db.session.commit()
    flash(f'{u.name} was created successfully.', 'success')
    return _back('users')


@main.route('/settings/users/<int:uid>', methods=['GET', 'POST'])
def user_edit(uid):
    r = guard('users.manage')
    if r:
        return r
    u = get_or_404(User, uid)
    roles = Role.query.order_by(Role.id).all()
    depts = Department.query.order_by(Department.name).all()
    me = current_user()
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        role = get_or_404(Role, request.form.get('role_id', type=int) or 0)
        dept_ids = {int(i) for i in request.form.getlist('department_ids') if i.isdigit()}
        active = 'is_active' in request.form
        password = request.form.get('password', '')
        cap_raw = request.form.get('capacity', '').strip()
        error = None
        if cap_raw and (not cap_raw.isdigit() or not 1 <= int(cap_raw) <= 500):
            error = 'Capacity must be a whole number between 1 and 500.'
        elif not name or not email:
            error = 'Name and email are required.'
        elif User.query.filter(func.lower(User.email) == email, User.id != u.id).first():
            error = 'Another user already has that email.'
        elif password and len(password) < MIN_PASSWORD:
            error = f'Password must be at least {MIN_PASSWORD} characters.'
        elif u.id == me.id and not active:
            error = 'You cannot deactivate your own account.'
        elif u.is_admin() and u.is_active and (role.name != 'Admin' or not active) and _active_admins(u.id) == 0:
            error = 'At least one active administrator must remain.'
        if error:
            flash(error, 'error')
            return render_template('user_form.html', target=u, roles=roles, departments=depts,
                                   form=request.form.to_dict(), selected=dept_ids,
                                   categories=Category.query.filter_by(is_active=True).order_by(Category.name).all(),
                                   skill_ids={int(i) for i in request.form.getlist('skill_ids') if i.isdigit()},
                                   teams=Team.query.order_by(Team.name).all())
        u.name, u.email, u.is_active = name, email, active
        u.capacity = int(cap_raw) if cap_raw else 15
        skill_ids = {int(i) for i in request.form.getlist('skill_ids') if i.isdigit()}
        u.skills = [c for c in Category.query.all() if c.id in skill_ids]
        set_team(u, request.form.get('team_id', type=int))
        u.roles = [role]
        u.departments = [d for d in depts if d.id in dept_ids]
        if password:
            u.set_password(password)
            u.must_change_password = True
        audit('USER_UPDATED', 'User', u.id, 'password reset' if password else None)
        db.session.commit()
        flash(f'{u.name} was updated.', 'success')
        return _back('users')
    form = {'name': u.name, 'email': u.email, 'role_id': str(u.roles[0].id) if u.roles else '',
            'is_active': u.is_active, 'capacity': u.capacity}
    return render_template('user_form.html', target=u, roles=roles, departments=depts, form=form,
                           selected={d.id for d in u.departments}, categories=Category.query.filter_by(is_active=True)
                           .order_by(Category.name).all(), skill_ids={c.id for c in u.skills},
                           teams=Team.query.order_by(Team.name).all())


# ───────────── departments & categories ─────────────
@main.route('/settings/departments', methods=['POST'])
def settings_department():
    r = guard('departments.manage')
    if r:
        return r
    dept_id = request.form.get('id', type=int)
    name = request.form.get('name', '').strip()
    desc = request.form.get('description', '').strip() or None
    cap_raw = request.form.get('capacity', '').strip()
    if cap_raw and (not cap_raw.isdigit() or not 1 <= int(cap_raw) <= 100000):
        flash('Capacity must be a whole number of tickets (or left blank).', 'error')
        return _back('structure')
    capacity = int(cap_raw) if cap_raw else None
    if not name:
        flash('Department name is required.', 'error')
        return _back('structure')
    if Department.query.filter(func.lower(Department.name) == name.lower(), Department.id != (dept_id or 0)).first():
        flash(f'A department named “{name}” already exists.', 'error')
        return _back('structure')
    if dept_id:
        d = get_or_404(Department, dept_id)
        d.name, d.description, d.is_active = name, desc, 'is_active' in request.form
        d.capacity = capacity
        audit('DEPARTMENT_UPDATED', 'Department', d.id)
    else:
        d = Department(name=name, description=desc, is_active=True, capacity=capacity)
        db.session.add(d)
        db.session.flush()
        audit('DEPARTMENT_CREATED', 'Department', d.id)
    db.session.commit()
    flash('Department saved.', 'success')
    return _back('structure')


@main.route('/settings/categories', methods=['POST'])
def settings_category():
    r = guard('settings.manage')
    if r:
        return r
    cat_id = request.form.get('id', type=int)
    name = request.form.get('name', '').strip()
    dept_id = request.form.get('department_id', type=int)
    if not name:
        flash('Category name is required.', 'error')
        return _back('structure')
    if dept_id:
        get_or_404(Department, dept_id)
    if Category.query.filter(func.lower(Category.name) == name.lower(), Category.id != (cat_id or 0)).first():
        flash(f'A category named “{name}” already exists.', 'error')
        return _back('structure')
    if cat_id:
        c = get_or_404(Category, cat_id)
        c.name, c.department_id, c.is_active = name, dept_id, 'is_active' in request.form
        audit('CATEGORY_UPDATED', 'Category', c.id)
    else:
        c = Category(name=name, department_id=dept_id, is_active=True)
        db.session.add(c)
        db.session.flush()
        audit('CATEGORY_CREATED', 'Category', c.id)
    db.session.commit()
    flash('Category saved.', 'success')
    return _back('structure')


# ───────────── SLA, canned responses, general ─────────────
@main.route('/settings/sla', methods=['POST'])
def settings_sla():
    r = guard('settings.manage')
    if r:
        return r
    updates = []
    for p in Priority.query.all():
        first = request.form.get(f'first_{p.id}', type=int)
        res = request.form.get(f'resolution_{p.id}', type=int)
        if first is None or res is None or first < 1 or res < 1:
            flash(f'{p.name}: enter whole numbers of minutes (1 or more).', 'error')
            return _back('sla')
        updates.append((p, first, res, f'active_{p.id}' in request.form, f'bh_{p.id}' in request.form))
    for p, first, res, active, biz in updates:
        row = SLASettings.query.filter_by(priority_id=p.id).first()
        if not row:
            row = SLASettings(priority_id=p.id)
            db.session.add(row)
        row.first_response_minutes, row.resolution_minutes, row.active = first, res, active
        row.business_hours_only = biz
    audit('SLA_UPDATED', 'SLASettings')
    db.session.commit()
    flash('SLA targets saved. They apply to tickets created from now on.', 'success')
    return _back('sla')


@main.route('/settings/canned', methods=['POST'])
def settings_canned():
    r = guard('settings.manage')
    if r:
        return r
    cid = request.form.get('id', type=int)
    title = request.form.get('title', '').strip()
    body = request.form.get('body', '').strip()
    if not title or not body:
        flash('Title and message are both required.', 'error')
        return _back('canned')
    if CannedResponse.query.filter(func.lower(CannedResponse.title) == title.lower(),
                                   CannedResponse.id != (cid or 0)).first():
        flash(f'A canned response titled “{title}” already exists.', 'error')
        return _back('canned')
    if cid:
        c = get_or_404(CannedResponse, cid)
        c.title, c.body, c.is_active = title, body, 'is_active' in request.form
    else:
        db.session.add(CannedResponse(title=title, body=body, is_active=True))
    db.session.commit()
    flash('Canned response saved.', 'success')
    return _back('canned')


@main.route('/settings/canned/<int:cid>/delete', methods=['POST'])
def settings_canned_delete(cid):
    r = guard('settings.manage')
    if r:
        return r
    db.session.delete(get_or_404(CannedResponse, cid))
    db.session.commit()
    flash('Canned response deleted.', 'success')
    return _back('canned')


@main.route('/settings/general', methods=['POST'])
def settings_general():
    r = guard('settings.manage')
    if r:
        return r
    name = request.form.get('company_name', '').strip()
    if not name:
        flash('Company name cannot be empty.', 'error')
        return _back('general')
    row = CompanySetting.query.filter_by(key='company_name').first()
    if row:
        row.value = name
    else:
        db.session.add(CompanySetting(key='company_name', value=name))
    from .branding import normalize_accent
    accent, err = normalize_accent(request.form.get('accent_custom', '').strip() or request.form.get('accent', ''))
    if err:
        flash(err, 'error')
        return _back('general')
    tag = CompanySetting.query.filter_by(key='tagline').first() or CompanySetting(key='tagline')
    tag.value = request.form.get('tagline', '').strip()[:80]
    db.session.add(tag)
    ac = CompanySetting.query.filter_by(key='accent').first() or CompanySetting(key='accent')
    ac.value = accent
    db.session.add(ac)
    aa = CompanySetting.query.filter_by(key='auto_assign').first()
    if not aa:
        aa = CompanySetting(key='auto_assign')
        db.session.add(aa)
    aa.value = 'on' if 'auto_assign' in request.form else 'off'
    audit('COMPANY_NAME_UPDATED', 'CompanySetting')
    db.session.commit()
    flash('Settings saved.', 'success')
    return _back('general')


# ───────────── profile & preferences ─────────────
@main.route('/profile', methods=['GET', 'POST'])
def profile():
    r = require_login()
    if r:
        return r
    u = current_user()
    if request.method == 'POST':
        current = request.form.get('current_password', '')
        new = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')
        if not u.check_password(current):
            flash('Your current password is incorrect.', 'error')
        elif len(new) < MIN_PASSWORD:
            flash(f'New password must be at least {MIN_PASSWORD} characters.', 'error')
        elif new != confirm:
            flash('The new passwords do not match.', 'error')
        elif new == current:
            flash('Choose a password different from your current one.', 'error')
        else:
            u.set_password(new)
            u.must_change_password = False
            audit('PASSWORD_CHANGED', 'User', u.id)
            db.session.commit()
            flash('Password updated.', 'success')
            return redirect(url_for('main.profile'))
    return render_template('profile.html', forced=u.must_change_password)


@main.route('/profile/theme', methods=['POST'])
def profile_theme():
    r = require_login()
    if r:
        return jsonify(error='unauthorized'), 401
    theme = (request.get_json(silent=True) or {}).get('theme') or request.form.get('theme')
    if theme not in ('light', 'dark'):
        abort(400)
    current_user().theme = theme
    db.session.commit()
    return '', 204


# ───────────── business hours & holidays ─────────────
@main.route('/settings/hours', methods=['POST'])
def settings_hours():
    r = guard('settings.manage')
    if r:
        return r
    parsed = []
    for i in range(7):
        start, end = request.form.get(f'start_{i}', '08:00'), request.form.get(f'end_{i}', '17:00')
        working = f'work_{i}' in request.form
        try:
            ok = len(start) == 5 and len(end) == 5 and int(start[:2]) < 24 and int(end[:2]) < 24 and start[2] == end[2] == ':'
            int(start[3:]), int(end[3:])
        except ValueError:
            ok = False
        if not ok or (working and start >= end):
            flash(f'{DAY_NAMES[i]}: use HH:MM times and make the closing time later than the opening time.', 'error')
            return _back('hours')
        parsed.append((i, working, start, end))
    for i, working, start, end in parsed:
        row = BusinessHour.query.filter_by(day_of_week=i).first() or BusinessHour(day_of_week=i)
        row.is_working_day, row.start_time, row.end_time = working, start, end
        db.session.add(row)
    audit('BUSINESS_HOURS_UPDATED', 'BusinessHour')
    db.session.commit()
    flash('Business hours saved. They apply to deadlines calculated from now on.', 'success')
    return _back('hours')


@main.route('/settings/holidays', methods=['POST'])
def settings_holiday():
    r = guard('settings.manage')
    if r:
        return r
    name, when = request.form.get('name', '').strip(), parse_date(request.form.get('date'))
    if not name or not when:
        flash('Enter a holiday name and date.', 'error')
    elif Holiday.query.filter_by(date=when).first():
        flash('There is already a holiday on that date.', 'error')
    else:
        db.session.add(Holiday(name=name[:150], date=when))
        audit('HOLIDAY_ADDED', 'Holiday', None, f'{name} {when}')
        db.session.commit()
        flash('Holiday added.', 'success')
    return _back('hours')


@main.route('/settings/holidays/<int:hid>/delete', methods=['POST'])
def settings_holiday_delete(hid):
    r = guard('settings.manage')
    if r:
        return r
    h = get_or_404(Holiday, hid)
    db.session.delete(h)
    audit('HOLIDAY_REMOVED', 'Holiday', hid)
    db.session.commit()
    flash('Holiday removed.', 'success')
    return _back('hours')


UGANDA_FIXED_HOLIDAYS = [(1, 1, "New Year's Day"), (1, 26, 'NRM Liberation Day'), (2, 16, 'Archbishop Janani Luwum Day'),
                         (3, 8, "International Women's Day"), (5, 1, 'Labour Day'), (6, 3, "Martyrs' Day"),
                         (6, 9, "National Heroes' Day"), (10, 9, 'Independence Day'), (12, 25, 'Christmas Day'),
                         (12, 26, 'Boxing Day')]


@main.route('/settings/holidays/preset', methods=['POST'])
def settings_holiday_preset():
    """Add the fixed-date Ugandan public holidays for a year. Movable ones (Easter, Eid) must be added by hand."""
    r = guard('settings.manage')
    if r:
        return r
    from datetime import date
    year = request.form.get('year', type=int)
    if not year or not 2000 <= year <= 2100:
        flash('Enter a year, for example 2026.', 'error')
        return _back('hours')
    added = 0
    for month, day, name in UGANDA_FIXED_HOLIDAYS:
        d = date(year, month, day)
        if not Holiday.query.filter_by(date=d).first():
            db.session.add(Holiday(name=name, date=d))
            added += 1
    audit('HOLIDAY_PRESET', 'Holiday', None, f'{year}: {added} added')
    db.session.commit()
    flash(f'Added {added} fixed-date holidays for {year}. Add movable holidays (Easter, Eid) yourself.', 'success')
    return _back('hours')


# ───────────── teams ─────────────
def set_team(user, team_id):
    """Put a person in exactly one team (or none)."""
    for t in list(user.team_list):
        t.members.remove(user)
    team = db.session.get(Team, team_id) if team_id else None
    if team:
        team.members.append(user)


@main.route('/settings/teams', methods=['POST'])
def settings_team():
    r = guard('settings.manage')
    if r:
        return r
    name = request.form.get('name', '').strip()[:100]
    team_id = request.form.get('id', type=int)
    lead_id = request.form.get('lead_id', type=int)
    lead = db.session.get(User, lead_id) if lead_id else None
    if not name:
        flash('Team name is required.', 'error')
        return _back('teams')
    if lead and not lead.is_manager():
        flash('A team lead must have the Manager / Team Lead role.', 'error')
        return _back('teams')
    clash = Team.query.filter(db.func.lower(Team.name) == name.lower())
    if team_id:
        clash = clash.filter(Team.id != team_id)
    if clash.first():
        flash('There is already a team with that name.', 'error')
        return _back('teams')
    team = db.session.get(Team, team_id) if team_id else Team()
    if team_id and not team:
        abort(404)
    team.name, team.lead = name, lead
    db.session.add(team)
    if lead:
        set_team(lead, None)
        team.members.append(lead)          # the lead belongs to their own team
    audit('TEAM_SAVED', 'Team', team.id, name)
    db.session.commit()
    flash(f'Team “{name}” saved.', 'success')
    return _back('teams')


@main.route('/settings/teams/<int:tid>/delete', methods=['POST'])
def settings_team_delete(tid):
    r = guard('settings.manage')
    if r:
        return r
    t = get_or_404(Team, tid)
    t.members = []
    db.session.delete(t)
    audit('TEAM_DELETED', 'Team', tid, t.name)
    db.session.commit()
    flash('Team removed. Its people are now unassigned to a team.', 'success')
    return _back('teams')
