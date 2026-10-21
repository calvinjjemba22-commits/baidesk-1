import os, secrets
from flask import Flask, render_template, redirect, url_for, session, request, abort
from markupsafe import Markup
from sqlalchemy.exc import IntegrityError
from .config import Config
from .extensions import db
from .timeutil import utcnow, localdt, ago, iso_utc

def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    if test_config: app.config.update(test_config)
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    db.init_app(app)
    from .db_tuning import tune_sqlite
    with app.app_context():
        tune_sqlite(db.engine)
    from .routes import main
    from . import routes_admin, routes_reports, routes_ops, routes_data, routes_ui, routes_brand, routes_work  # noqa: F401  (register their routes on `main`)
    app.register_blueprint(main)
    app.jinja_env.filters['localdt'] = localdt
    app.jinja_env.filters['ago'] = ago
    app.jinja_env.filters['iso_utc'] = iso_utc
    with app.app_context():
        db.create_all()
        from .migrate import upgrade_schema
        upgrade_schema()
        try:
            seed()
        except IntegrityError:
            # Another worker/process seeded at the same time; the data is already there.
            db.session.rollback()
    from . import cli
    cli.register(app)
    install_csrf(app)
    @app.context_processor
    def inject_globals():
        from .services import current_user
        from .models import CompanySetting
        from .branding import brand_css
        from .branding import BOLGA_MARK_PATHS
        rows = dict(db.session.query(CompanySetting.key, CompanySetting.value)
                    .filter(CompanySetting.key.in_(('company_name', 'accent', 'brand_mark', 'tagline', 'logo_version', 'logo_tile'))).all())
        name = rows.get('company_name') or 'BaiDesk'
        first, _, rest = name.partition(' ')
        mark = rows.get('brand_mark') if rows.get('brand_mark') in ('bolga', 'custom') else 'letter'
        if mark == 'custom' and not rows.get('logo_version'):
            mark = 'letter'
        brand = {'name': name, 'first': first, 'rest': rest, 'mark': mark, 'tagline': rows.get('tagline') or '',
                 'tile': rows.get('logo_tile') if rows.get('logo_tile') in ('light', 'accent') else 'light',
                 'logo_url': '/brand/logo?v=' + (rows.get('logo_version') or '0')}
        return {'current_user': current_user(), 'now': utcnow(), 'company_name': name, 'brand': brand,
                'bolga_paths': BOLGA_MARK_PATHS, 'brand_css': brand_css(rows.get('accent'))}

    @app.before_request
    def force_password_change():
        from .services import current_user
        if request.endpoint in (None, 'static') or request.endpoint in ('main.profile', 'main.logout', 'main.login'):
            return None
        u = current_user()
        if u and u.must_change_password:
            return redirect(url_for('main.profile'))
        return None

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        resp.headers.setdefault('Referrer-Policy', 'same-origin')
        return resp

    @app.errorhandler(403)
    def forbidden(_):
        return render_template('403.html'), 403
    def plain_error(code, title, msg):
        html = ('<!doctype html><meta charset="utf-8"><title>%s — BaiDesk</title>'
                '<body style="font-family:system-ui;max-width:480px;margin:15vh auto;text-align:center">'
                '<h1>%s</h1><p>%s</p><p><a href="/">Back to BaiDesk</a></p></body>') % (title, code, msg)
        return html, code
    @app.errorhandler(400)
    def bad_request(_):
        return plain_error(400, 'Bad request', 'The request was invalid or incomplete.')
    @app.errorhandler(404)
    def not_found(_):
        return plain_error(404, 'Not found', 'That page or record does not exist.')
    @app.errorhandler(413)
    def too_large(_):
        return plain_error(413, 'File too large', 'The upload exceeds the maximum allowed size.')
    @app.errorhandler(ValueError)
    def bad_value(_):
        db.session.rollback()
        return plain_error(400, 'Bad request', 'One of the submitted values was not valid.')
    @app.errorhandler(500)
    def server_error(_):
        db.session.rollback()
        return plain_error(500, 'Something went wrong', 'The error has been logged.')
    return app


def install_csrf(app):
    """Minimal session-based CSRF protection for every state-changing request."""
    @app.context_processor
    def csrf_helpers():
        def csrf_token():
            if '_csrf' not in session:
                session['_csrf'] = secrets.token_urlsafe(32)
            return session['_csrf']
        def csrf_field():
            return Markup('<input type="hidden" name="_csrf" value="%s">' % csrf_token())
        return {'csrf_token': csrf_token, 'csrf_field': csrf_field}

    @app.before_request
    def check_csrf():
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and not app.config.get('CSRF_DISABLED'):
            sent = request.form.get('_csrf') or request.headers.get('X-CSRF-Token', '')
            expected = session.get('_csrf', '')
            if not expected or not secrets.compare_digest(sent, expected):
                abort(400)

def seed():
    from .models import Role, Permission, Department, User, TicketStatus, Priority, Category, Disposition, SLASettings, BusinessHour, CompanySetting, CannedResponse
    perms = ['dashboard.view','customer.view','customer.create','customer.edit','ticket.view','ticket.create','ticket.edit','ticket.assign','ticket.escalate','ticket.comment','interaction.create','reports.view','settings.manage','users.manage','departments.manage']
    for code in perms:
        if not Permission.query.filter_by(code=code).first(): db.session.add(Permission(code=code))
    db.session.flush()
    roles = {'Admin': perms, 'Manager / Team Lead': [p for p in perms if p not in ('users.manage','departments.manage','settings.manage')], 'Agent': ['dashboard.view','customer.view','customer.create','customer.edit','ticket.view','ticket.create','ticket.edit','ticket.assign','ticket.escalate','ticket.comment','interaction.create']}
    for name, pnames in roles.items():
        role = Role.query.filter_by(name=name).first()
        if not role:
            role = Role(name=name); db.session.add(role); db.session.flush()
        role.permissions = Permission.query.filter(Permission.code.in_(pnames)).all()
    depts = ['Support','Collections','Finance','Technical']
    for name in depts:
        if not Department.query.filter_by(name=name).first(): db.session.add(Department(name=name))
    statuses = [('Open','open','#38bdf8'),('Pending','pending','#f59e0b'),('In Progress','in-progress','#8b5cf6'),('Escalated','escalated','#f97316'),('Resolved','resolved','#22c55e'),('Closed','closed','#64748b')]
    for i,(name,slug,color) in enumerate(statuses):
        if not TicketStatus.query.filter_by(slug=slug).first(): db.session.add(TicketStatus(name=name,slug=slug,color=color,sort_order=i))
    priorities = [('Low','low','#22c55e',0),('Medium','medium','#38bdf8',1),('High','high','#f59e0b',2),('Urgent','urgent','#ef4444',3)]
    for name,slug,color,order in priorities:
        if not Priority.query.filter_by(slug=slug).first(): db.session.add(Priority(name=name,slug=slug,color=color,sort_order=order))
    cats = ['General Support','Payments','Account','Technical Issue','Collections','Complaint']
    for name in cats:
        if not Category.query.filter_by(name=name).first(): db.session.add(Category(name=name))
    for name in ['Resolved','Customer Callback','Information Provided','Escalated','No Answer','Follow-up Required']:
        if not Disposition.query.filter_by(name=name).first(): db.session.add(Disposition(name=name))
    db.session.flush()
    for p in Priority.query.all():
        if not SLASettings.query.filter_by(priority_id=p.id).first():
            mins = {'low':4320,'medium':2880,'high':1440,'urgent':240}[p.slug]
            db.session.add(SLASettings(priority_id=p.id, first_response_minutes={'low':480,'medium':240,'high':60,'urgent':15}[p.slug], resolution_minutes=mins))
    if BusinessHour.query.count() == 0:
        for d in range(7): db.session.add(BusinessHour(day_of_week=d,is_working_day=d<5,start_time='08:00',end_time='17:00'))
    if not CompanySetting.query.filter_by(key='company_name').first(): db.session.add(CompanySetting(key='company_name', value='BaiDesk'))
    canned = [('Acknowledge request', 'Hello, thank you for contacting us. We have received your request and are looking into it. We will update you shortly.'),
              ('Request more information', 'Thank you for your message. To help us resolve this quickly, could you please share more details, such as the date it happened and any reference numbers?'),
              ('Issue resolved', 'We are pleased to let you know that your issue has been resolved. Please reply if you need anything further and we will be glad to help.')]
    for title, body in canned:
        if not CannedResponse.query.filter_by(title=title).first():
            db.session.add(CannedResponse(title=title, body=body))
    if not User.query.filter_by(email='admin@baidesk.local').first():
        # Load related rows FIRST, then build the user and attach it to the session
        # BEFORE assigning relationships. Assigning u.roles / u.departments on a
        # transient User while another query autoflushes is what raised the SAWarning.
        admin_role = Role.query.filter_by(name='Admin').first()
        all_depts = Department.query.all()
        u = User(name='BaiDesk Administrator', email='admin@baidesk.local')
        u.set_password(os.getenv('BAIDESK_ADMIN_PASSWORD', 'Admin@123'))
        u.must_change_password = os.getenv('BAIDESK_ADMIN_MUST_CHANGE') == '1'      # set by serve.py on a brand-new install
        db.session.add(u)
        u.roles = [admin_role]
        u.departments = all_depts
    db.session.commit()
