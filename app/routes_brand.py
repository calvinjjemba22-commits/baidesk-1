"""Brand identity: logo upload, built-in marks, favicon and the one-click Bolga Communications preset."""
import base64
import hashlib
from flask import Response, abort, flash, redirect, request, url_for
from .extensions import db
from .models import CompanySetting
from .services import audit
from .routes import main, guard
from . import branding

LOGO_KEYS = ('logo_data', 'logo_mime', 'logo_version')


def get_setting(key, default=None):
    row = CompanySetting.query.filter_by(key=key).first()
    return row.value if row and row.value is not None else default


def set_setting(key, value):
    row = CompanySetting.query.filter_by(key=key).first()
    if value is None:
        if row:
            db.session.delete(row)
        return
    if not row:
        row = CompanySetting(key=key)
        db.session.add(row)
    row.value = value


def _back():
    return redirect(url_for('main.settings', tab='general'))


# ───────────────────────── public: the logo and favicon (needed on the sign-in page) ─────────────────────────
@main.route('/brand/logo')
def brand_logo():
    data, mime = get_setting('logo_data'), get_setting('logo_mime')
    if not data or not mime:
        abort(404)
    raw = base64.b64decode(data)
    etag = get_setting('logo_version', '0')
    if request.headers.get('If-None-Match') == f'"{etag}"':
        return Response(status=304)
    resp = Response(raw, mimetype=mime)
    resp.headers['ETag'] = f'"{etag}"'
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    # an SVG opened directly in a tab cannot run anything: no scripts, no network, sandboxed
    resp.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"
    return resp


@main.route('/brand/favicon.svg')
def brand_favicon():
    accent, _ = branding.normalize_accent(get_setting('accent'))
    mark = get_setting('brand_mark')
    if mark == 'bolga':
        inner = f'<g fill="#fff" transform="translate(32 32) scale(.072) translate(40 0)">{branding.BOLGA_MARK_PATHS}</g>'
    else:
        letter = (get_setting('company_name') or 'B')[:1].upper().replace('&', '').replace('<', '')
        inner = f'<text x="32" y="44" font-family="Arial,sans-serif" font-size="36" font-weight="800" text-anchor="middle" fill="#fff">{letter}</text>'
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="15" fill="{accent}"/>{inner}</svg>'
    resp = Response(svg, mimetype='image/svg+xml')
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


# ───────────────────────── admin ─────────────────────────
@main.route('/settings/branding/logo', methods=['POST'])
def branding_logo_upload():
    r = guard('settings.manage')
    if r:
        return r
    f = request.files.get('logo')
    data = f.read(branding.LOGO_MAX_BYTES + 1) if f and f.filename else b''
    mime, err = branding.validate_logo(data)
    if err:
        flash(err, 'error')
        return _back()
    set_setting('logo_data', base64.b64encode(data).decode('ascii'))
    set_setting('logo_mime', mime)
    set_setting('logo_version', hashlib.sha1(data).hexdigest()[:10])
    set_setting('logo_tile', 'accent' if request.form.get('logo_tile') == 'accent' else 'light')
    set_setting('brand_mark', 'custom')
    audit('LOGO_UPLOADED', 'CompanySetting', None, mime)
    db.session.commit()
    flash('Logo saved and applied.', 'success')
    return _back()


@main.route('/settings/branding/mark', methods=['POST'])
def branding_mark():
    r = guard('settings.manage')
    if r:
        return r
    choice = request.form.get('mark', '')
    if choice == 'custom' and not get_setting('logo_version'):
        flash('Upload a logo first.', 'error')
        return _back()
    if choice not in ('letter', 'bolga', 'custom'):
        abort(400)
    set_setting('brand_mark', choice)
    if choice == 'custom':
        set_setting('logo_tile', 'accent' if request.form.get('logo_tile') == 'accent' else 'light')
    audit('BRAND_MARK_CHANGED', 'CompanySetting', None, choice)
    db.session.commit()
    flash('Logo style updated.', 'success')
    return _back()


@main.route('/settings/branding/logo/remove', methods=['POST'])
def branding_logo_remove():
    r = guard('settings.manage')
    if r:
        return r
    for k in LOGO_KEYS:
        set_setting(k, None)
    if get_setting('brand_mark') == 'custom':
        set_setting('brand_mark', 'letter')
    audit('LOGO_REMOVED', 'CompanySetting')
    db.session.commit()
    flash('Uploaded logo removed.', 'success')
    return _back()


@main.route('/settings/branding/bolga', methods=['POST'])
def branding_apply_bolga():
    """One click: Bolga Communications name, tagline, blue and logo mark."""
    r = guard('settings.manage')
    if r:
        return r
    set_setting('company_name', branding.BOLGA['name'])
    set_setting('tagline', branding.BOLGA['tagline'])
    set_setting('accent', branding.BOLGA['accent'])
    set_setting('brand_mark', 'bolga')
    audit('BRAND_PRESET_APPLIED', 'CompanySetting', None, 'Bolga Communications')
    db.session.commit()
    flash('Bolga Communications branding applied.', 'success')
    return _back()
