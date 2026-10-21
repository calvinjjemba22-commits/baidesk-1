import json, re, os, uuid
from flask import current_app, session, has_request_context
from .extensions import db
from .models import ActivityLog, Notification, AuditLog, TicketHistory, User

ACTIVE_SLUGS = ['open', 'pending', 'in-progress', 'escalated']


def current_user():
    if not has_request_context():      # scripts / background jobs act as "System"
        return None
    uid = session.get('user_id')
    return db.session.get(User, uid) if uid else None

def log_activity(activity_type, description, customer_id=None, ticket_id=None, entity_type=None, entity_id=None, department_id=None, metadata=None):
    actor = current_user()
    row = ActivityLog(customer_id=customer_id, ticket_id=ticket_id, actor_user_id=actor.id if actor else None,
                      department_id=department_id, activity_type=activity_type, entity_type=entity_type,
                      entity_id=entity_id, description=description, metadata_json=json.dumps(metadata or {}))
    db.session.add(row)
    return row

def notify(user_id, type_, title, message, entity_type=None, entity_id=None):
    if user_id:
        db.session.add(Notification(user_id=user_id, type=type_, title=title, message=message, entity_type=entity_type, entity_id=entity_id))

def audit(action, entity_type=None, entity_id=None, details=None):
    actor = current_user()
    from flask import request
    db.session.add(AuditLog(user_id=actor.id if actor else None, action=action, entity_type=entity_type, entity_id=entity_id,
                            ip_address=request.remote_addr, user_agent=request.headers.get('User-Agent',''), details=details))

def normalize_phone(phone):
    return re.sub(r'[^0-9+]', '', phone or '')

def next_ticket_number():
    from .models import Ticket
    last = Ticket.query.order_by(Ticket.id.desc()).first()
    return f'TKT-{(last.id + 1 if last else 1):05d}'

def save_upload(file, user_id):
    if not file or not file.filename: return None
    allowed = {'png','jpg','jpeg','gif','webp','pdf','doc','docx','xls','xlsx','txt','csv'}
    ext = file.filename.rsplit('.',1)[-1].lower() if '.' in file.filename else ''
    if ext not in allowed: raise ValueError('Unsupported attachment type.')
    safe = f'{uuid.uuid4().hex}.{ext}'
    path = os.path.join(current_app.config['UPLOAD_FOLDER'], safe)
    os.makedirs(current_app.config['UPLOAD_FOLDER'], exist_ok=True)
    file.save(path)
    from .models import Attachment
    row = Attachment(filename=file.filename, stored_filename=safe, mime_type=file.mimetype, file_size=os.path.getsize(path), storage_path=path, uploaded_by=user_id)
    db.session.add(row); db.session.flush(); return row
