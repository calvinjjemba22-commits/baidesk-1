from werkzeug.security import generate_password_hash, check_password_hash
from .extensions import db
from .timeutil import utcnow

user_roles = db.Table('user_roles',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'), primary_key=True),
    db.Column('role_id', db.Integer, db.ForeignKey('roles.id'), primary_key=True))
user_departments = db.Table('user_departments',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'), primary_key=True),
    db.Column('department_id', db.Integer, db.ForeignKey('departments.id'), primary_key=True))
role_permissions = db.Table('role_permissions',
    db.Column('role_id', db.Integer, db.ForeignKey('roles.id'), primary_key=True),
    db.Column('permission_id', db.Integer, db.ForeignKey('permissions.id'), primary_key=True))
team_members = db.Table('team_members',
    db.Column('team_id', db.Integer, db.ForeignKey('teams.id'), primary_key=True),
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'), primary_key=True, unique=True))   # one team per person
user_skills = db.Table('user_skills',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'), primary_key=True),
    db.Column('category_id', db.Integer, db.ForeignKey('categories.id'), primary_key=True))
customer_tag_links = db.Table('customer_tag_links',
    db.Column('customer_id', db.Integer, db.ForeignKey('customers.id'), primary_key=True),
    db.Column('tag_id', db.Integer, db.ForeignKey('tags.id'), primary_key=True))

class TimestampMixin:
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

class Role(db.Model):
    __tablename__ = 'roles'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    permissions = db.relationship('Permission', secondary=role_permissions, backref='roles')

class Permission(db.Model):
    __tablename__ = 'permissions'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(120), unique=True, nullable=False)

class Department(db.Model, TimestampMixin):
    __tablename__ = 'departments'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    description = db.Column(db.String(500))
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    capacity = db.Column(db.Integer)                                      # optional: max active tickets for the department

class User(db.Model, TimestampMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    theme = db.Column(db.String(20), default='light', nullable=False)
    capacity = db.Column(db.Integer, default=15, nullable=False)          # max active tickets for auto-routing
    is_available = db.Column(db.Boolean, default=True, nullable=False)    # 'away' agents are skipped by auto-routing
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)   # set for temporary passwords
    roles = db.relationship('Role', secondary=user_roles, backref='users')
    departments = db.relationship('Department', secondary=user_departments, backref='users')
    skills = db.relationship('Category', secondary=user_skills, backref='specialists')   # ticket categories this agent handles; none = generalist
    @property
    def team(self):
        return self.team_list[0] if self.team_list else None
    def set_password(self, password): self.password_hash = generate_password_hash(password)
    def check_password(self, password): return check_password_hash(self.password_hash, password)
    def has_permission(self, code):
        return any(code == p.code for r in self.roles for p in r.permissions) or any(r.name == 'Admin' for r in self.roles)
    def is_admin(self): return any(r.name == 'Admin' for r in self.roles)
    def is_manager(self): return any(r.name in ('Admin','Manager / Team Lead') for r in self.roles)

class Customer(db.Model, TimestampMixin):
    __tablename__ = 'customers'
    id = db.Column(db.Integer, primary_key=True)
    customer_number = db.Column(db.String(40), unique=True, nullable=False, index=True)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), default='')
    company = db.Column(db.String(180))
    email = db.Column(db.String(255), index=True)
    location = db.Column(db.String(255))
    notes = db.Column(db.Text)
    status = db.Column(db.String(30), default='Active', nullable=False)
    phones = db.relationship('CustomerPhone', backref='customer', cascade='all, delete-orphan')
    tags = db.relationship('Tag', secondary=customer_tag_links, backref='customers')

class CustomerPhone(db.Model):
    __tablename__ = 'customer_phones'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True)
    phone_number = db.Column(db.String(50), nullable=False, index=True)
    phone_type = db.Column(db.String(40), default='Mobile')
    is_primary = db.Column(db.Boolean, default=False)

class Tag(db.Model):
    __tablename__ = 'tags'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)

class TicketStatus(db.Model):
    __tablename__ = 'ticket_statuses'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False)
    color = db.Column(db.String(20), default='#64748b')
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)

class Priority(db.Model):
    __tablename__ = 'priorities'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    slug = db.Column(db.String(50), unique=True, nullable=False)
    color = db.Column(db.String(20), default='#64748b')
    sort_order = db.Column(db.Integer, default=0)

class Category(db.Model):
    __tablename__ = 'categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'))
    is_active = db.Column(db.Boolean, default=True)
    department = db.relationship('Department')

class Disposition(db.Model):
    __tablename__ = 'dispositions'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    channel = db.Column(db.String(50), default='Any')
    is_active = db.Column(db.Boolean, default=True)

class Ticket(db.Model, TimestampMixin):
    __tablename__ = 'tickets'
    id = db.Column(db.Integer, primary_key=True)
    ticket_number = db.Column(db.String(30), unique=True, nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True)
    subject = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'))
    priority_id = db.Column(db.Integer, db.ForeignKey('priorities.id'), nullable=False)
    status_id = db.Column(db.Integer, db.ForeignKey('ticket_statuses.id'), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'))
    assigned_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    due_at = db.Column(db.DateTime)
    first_response_at = db.Column(db.DateTime)
    resolved_at = db.Column(db.DateTime)
    closed_at = db.Column(db.DateTime)
    first_response_due_at = db.Column(db.DateTime)
    sla_paused_at = db.Column(db.DateTime)                                # set while waiting on the customer
    sla_paused_minutes = db.Column(db.Integer, default=0, nullable=False) # time already excluded from the SLA clock
    customer = db.relationship('Customer', backref='tickets')
    category = db.relationship('Category')
    priority = db.relationship('Priority')
    status = db.relationship('TicketStatus')
    department = db.relationship('Department')
    assigned_user = db.relationship('User', foreign_keys=[assigned_user_id])
    creator = db.relationship('User', foreign_keys=[created_by])

class TicketHistory(db.Model):
    __tablename__ = 'ticket_history'
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    action_type = db.Column(db.String(80), nullable=False)
    field_name = db.Column(db.String(80))
    old_value = db.Column(db.Text)
    new_value = db.Column(db.Text)
    reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    user = db.relationship('User')

class Interaction(db.Model):
    __tablename__ = 'interactions'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    channel = db.Column(db.String(50), nullable=False)
    direction = db.Column(db.String(30), nullable=False)
    disposition = db.Column(db.String(120))
    subject = db.Column(db.String(255))
    notes = db.Column(db.Text)
    interaction_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    customer = db.relationship('Customer', backref='interactions')
    user = db.relationship('User')

class TicketComment(db.Model):
    __tablename__ = 'ticket_comments'
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    comment_type = db.Column(db.String(20), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    user = db.relationship('User')

class CommentMention(db.Model):
    __tablename__ = 'comment_mentions'
    id = db.Column(db.Integer, primary_key=True)
    comment_id = db.Column(db.Integer, db.ForeignKey('ticket_comments.id'), nullable=False)
    mentioned_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

class Attachment(db.Model):
    __tablename__ = 'attachments'
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(120))
    file_size = db.Column(db.Integer)
    storage_path = db.Column(db.String(500), nullable=False)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

class AttachmentLink(db.Model):
    __tablename__ = 'attachment_links'
    id = db.Column(db.Integer, primary_key=True)
    attachment_id = db.Column(db.Integer, db.ForeignKey('attachments.id'), nullable=False)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id'))
    comment_id = db.Column(db.Integer, db.ForeignKey('ticket_comments.id'))
    interaction_id = db.Column(db.Integer, db.ForeignKey('interactions.id'))
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'))
    attachment = db.relationship('Attachment')
    ticket = db.relationship('Ticket')

class Escalation(db.Model):
    __tablename__ = 'escalations'
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id'), nullable=False, index=True)
    from_department_id = db.Column(db.Integer, db.ForeignKey('departments.id'))
    to_department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=False)
    initiated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), default='Pending', nullable=False)
    accepted_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    accepted_at = db.Column(db.DateTime)
    returned_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    returned_at = db.Column(db.DateTime)
    resolved_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

class DepartmentTime(db.Model):
    __tablename__ = 'department_time'
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id'), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=False)
    started_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    ended_at = db.Column(db.DateTime)
    duration_seconds = db.Column(db.Integer)

class ActivityLog(db.Model):
    __tablename__ = 'activity_log'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), index=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id'), index=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'))
    activity_type = db.Column(db.String(80), nullable=False, index=True)
    entity_type = db.Column(db.String(80))
    entity_id = db.Column(db.Integer)
    description = db.Column(db.Text, nullable=False)
    metadata_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    actor = db.relationship('User')

class Notification(db.Model):
    __tablename__ = 'notifications'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    type = db.Column(db.String(60), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=False)
    entity_type = db.Column(db.String(80))
    entity_id = db.Column(db.Integer)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    read_at = db.Column(db.DateTime)

class SLASettings(db.Model):
    __tablename__ = 'sla_rules'
    id = db.Column(db.Integer, primary_key=True)
    priority_id = db.Column(db.Integer, db.ForeignKey('priorities.id'), unique=True, nullable=False)
    first_response_minutes = db.Column(db.Integer, default=60)
    resolution_minutes = db.Column(db.Integer, default=1440)
    business_hours_only = db.Column(db.Boolean, default=True)
    active = db.Column(db.Boolean, default=True)

class BusinessHour(db.Model):
    __tablename__ = 'business_hours'
    id = db.Column(db.Integer, primary_key=True)
    day_of_week = db.Column(db.Integer, unique=True, nullable=False)
    is_working_day = db.Column(db.Boolean, default=True)
    start_time = db.Column(db.String(5), default='08:00')
    end_time = db.Column(db.String(5), default='17:00')

class Holiday(db.Model):
    __tablename__ = 'holidays'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    date = db.Column(db.Date, unique=True, nullable=False)

class CustomField(db.Model):
    __tablename__ = 'custom_fields'
    id = db.Column(db.Integer, primary_key=True)
    entity_type = db.Column(db.String(30), nullable=False)
    field_name = db.Column(db.String(100), nullable=False)
    label = db.Column(db.String(150), nullable=False)
    field_type = db.Column(db.String(30), default='text')
    is_required = db.Column(db.Boolean, default=False)
    options = db.Column(db.Text)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)

class CustomFieldValue(db.Model):
    __tablename__ = 'custom_field_values'
    id = db.Column(db.Integer, primary_key=True)
    custom_field_id = db.Column(db.Integer, db.ForeignKey('custom_fields.id'), nullable=False)
    entity_type = db.Column(db.String(30), nullable=False)
    entity_id = db.Column(db.Integer, nullable=False)
    value = db.Column(db.Text)

class CompanySetting(db.Model):
    __tablename__ = 'company_settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text)

class AuditLog(db.Model):
    __tablename__ = 'audit_log'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    action = db.Column(db.String(100), nullable=False)
    entity_type = db.Column(db.String(80))
    entity_id = db.Column(db.Integer)
    ip_address = db.Column(db.String(80))
    user_agent = db.Column(db.String(500))
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

class CannedResponse(db.Model):
    __tablename__ = 'canned_responses'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), unique=True, nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)


class SavedView(db.Model):
    """A named ticket-list filter. Shared views are visible to everyone."""
    __tablename__ = 'saved_views'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    name = db.Column(db.String(60), nullable=False)
    filters = db.Column(db.String(500), default='', nullable=False)   # URL query string of the saved filter
    is_shared = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    owner = db.relationship('User')

class PaymentPromise(db.Model):
    """'Customer promised to pay X on date Y' — the unit of work in collections."""
    __tablename__ = 'promises'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id'), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    amount = db.Column(db.Integer)
    promised_date = db.Column(db.Date, nullable=False, index=True)
    note = db.Column(db.String(300))
    status = db.Column(db.String(20), default='Pending', nullable=False, index=True)  # Pending | Kept | Broken | Cancelled
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    closed_at = db.Column(db.DateTime)
    customer = db.relationship('Customer', backref='promises')
    ticket = db.relationship('Ticket')
    owner = db.relationship('User')

class TicketReview(db.Model):
    """A manager's quality review of a resolved ticket (coaching)."""
    __tablename__ = 'ticket_reviews'
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id'), nullable=False, unique=True)
    agent_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    score = db.Column(db.Integer, nullable=False)          # 1-5
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    ticket = db.relationship('Ticket')
    reviewer = db.relationship('User', foreign_keys=[reviewer_id])

class AlertSent(db.Model):
    """Remembers which automatic alerts were already raised so nobody is notified twice."""
    __tablename__ = 'alerts_sent'
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(40), nullable=False)
    entity_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    __table_args__ = (db.UniqueConstraint('kind', 'entity_id', name='uq_alert_kind_entity'),)


class Team(db.Model):
    """A team lead and the agents they supervise. Team leads only see their own team in Coaching."""
    __tablename__ = 'teams'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    lead_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    lead = db.relationship('User', foreign_keys=[lead_id])
    members = db.relationship('User', secondary=team_members, backref='team_list', order_by='User.name')

class LoginAttempt(db.Model):
    """Failed sign-ins, used for the temporary lockout. Cleared for a person when they sign in successfully."""
    __tablename__ = 'login_attempts'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    ip = db.Column(db.String(64), index=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
