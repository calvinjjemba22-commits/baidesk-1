# BaiDesk Architecture

## Application layers
- Flask routes/controllers: HTTP, sessions, validation and permission gates.
- SQLAlchemy models: relational schema independent of SQLite-specific SQL.
- Services: authentication context, activity/audit logging, notifications, uploads, identifiers.
- HTML/CSS/JS frontend: responsive server-rendered UI with lightweight AJAX for notification/timeline refresh.

## Core immutable event model
`activity_log` is append-only. Customer timelines are projections of this event stream. Ticket-specific `ticket_history` records field-level changes; `audit_log` records security/admin events.

## PostgreSQL migration
Set `DATABASE_URL` to a PostgreSQL SQLAlchemy connection string and install a PostgreSQL driver. For production, use migrations (Alembic/Flask-Migrate), object storage for attachments and a background worker for SLA notifications.

## Security boundaries
Permissions are checked by Flask route guards. Passwords use Werkzeug password hashing. Session cookies are HTTP-only and SameSite=Lax. Attachments are restricted by extension and size. Internal comments are stored with a distinct type and should only be exposed through authenticated staff endpoints.
