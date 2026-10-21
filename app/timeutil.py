"""Time helpers. The database stores naive UTC; everything shown to people is converted
to the configured display timezone (DISPLAY_TZ, default Africa/Kampala)."""
from datetime import datetime, timezone, timedelta, date, time
from zoneinfo import ZoneInfo
from flask import current_app


def utcnow():
    """Naive UTC 'now' (replacement for the deprecated datetime.utcnow())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_tz():
    try:
        return ZoneInfo(current_app.config.get('DISPLAY_TZ', 'UTC'))
    except Exception:
        return timezone.utc


def to_local(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_tz())


def localdt(dt, fmt='%d %b %Y · %H:%M'):
    local = to_local(dt)
    return local.strftime(fmt) if local else '—'


def ago(dt):
    if not dt:
        return '—'
    secs = int((utcnow() - dt).total_seconds())
    if secs < 0:
        return localdt(dt)
    if secs < 60:
        return 'just now'
    if secs < 3600:
        return f'{secs // 60}m ago'
    if secs < 86400:
        return f'{secs // 3600}h ago'
    if secs < 7 * 86400:
        return f'{secs // 86400}d ago'
    return localdt(dt, '%d %b %Y')


def iso_utc(dt):
    """ISO string the browser parses as UTC (note the trailing Z)."""
    return dt.isoformat() + 'Z' if dt else ''


def local_date_to_utc(d):
    """Midnight at the start of local date `d`, as naive UTC."""
    return datetime.combine(d, time.min, tzinfo=get_tz()).astimezone(timezone.utc).replace(tzinfo=None)


def local_midnight_utc(offset_days=0):
    today = to_local(utcnow()).date()
    return local_date_to_utc(today + timedelta(days=offset_days))


def local_today():
    return to_local(utcnow()).date()


def parse_date(value):
    try:
        return date.fromisoformat((value or '').strip())
    except ValueError:
        return None
