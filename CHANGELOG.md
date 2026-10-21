# Changelog

## 1.7.0 — Bolga branding, My day, queue board, conversation tickets
**Bolga Communications identity**
- One-click **Apply Bolga Communications branding** (Administration → General): name, tagline "Service Beyond Expectations", brand blue and the Bolga logo mark. The mark is rebuilt as a vector from the company sign.
- **Logo upload** (PNG, JPG, WebP, SVG up to 1 MB) to replace the built-in mark with official artwork; light logos can sit on a brand-coloured tile. SVGs containing scripts or remote links are refused and logos are served sandboxed. The logo is stored in the database, so it is included in backups.
- Brand now shows in the menu, sign-in page and browser tab (favicon). The sign-in page no longer advertises a default administrator password.

**My day** (the agents' home; agents land here after signing in)
- "Up next" card with why it matters, one-click Claim for waiting tickets, a workload ring against capacity, an Available/Away switch, today's numbers, and lists for *Needs you now*, *Customer replied*, *Promises to follow up* (mark Kept/Broken in place) and *Waiting on customers*.

**Live queue board** (team leads and managers)
- Team status strip, the waiting queue (most urgent first, with waiting time), and a card per agent with load against capacity, late / at-risk / paused counts and their most urgent tickets. **Drag a ticket onto an agent to assign it** (or use Best agent); drop it back to release it. Refreshes every 15 seconds. Team leads see their own team only; the same department rule as the ticket form applies to non-admins.

**Conversation-style ticket page**
- The original request is the first message; customer messages sit on the left and the agent's on the right, internal notes are visually distinct, calls / SMS / WhatsApp appear as cards, and status, priority and assignment changes appear inline. Date dividers, All / Messages / Notes / Activity filter, sticky header with deadline and Take it / Resolve, floating composer (Ctrl+Enter sends, auto-growing), opens at the newest message. The full audit history stays available.

## 1.6.0 — "Aurora" interface
**Speed and keyboard-first**
- **Command palette** (`Ctrl+K` / `⌘K`, or `/`, or click the search box): search tickets and customers as you type, jump to any page, run actions (create ticket, switch theme, mark yourself away). Remembers your recent items. Respects what each person is allowed to see.
- **Ticket preview drawer**: press `Space` on a highlighted ticket (or click the eye icon, or "Preview" in a dashboard row) to read the key facts and latest activity without leaving the queue.
- **Live dashboard**: figures refresh quietly every minute (paused while you are using a menu or drawer) with a "Live · updated" indicator.

**Look and feel**
- Glass top bar, softer depth, an ambient glow behind the page, refined cards and buttons, clearer keyboard focus rings, tabular numbers.
- Motion with purpose: staggered page entrance, numbers that count up, chart lines and rings that draw in, smooth page-to-page transitions. All motion switches off for people who prefer reduced motion.
- **Toasts** replace banner messages; they dismiss themselves and pause when hovered.
- Sidebar sections (Workspace / Insight / System) and a **collapsible menu** that remembers your choice; phones get the compact menu automatically.
- Friendly **empty states** instead of blank tables.
- Dark mode refined to match.

**White-label brand colour**
- Administration → General: pick one of eight presets or enter any colour. It flows through buttons, links, the logo mark, the active menu item and charts, in light and dark mode. Colours too pale for readable white text are rejected (WCAG AA contrast), and the value is re-validated every time it is used.

## 1.5.0 — Ready for a team on a Windows PC
**Running for real**
- Production server (`serve.py`, waitress) reachable from other computers on the office network; `start_baidesk.bat` now starts it, restarts it if it stops, and `run.py` no longer enables debug mode by default.
- **First run creates a private secret key** and a one-time administrator password (the old shipped defaults `Admin@123` and a public session secret are no longer used on new installs; the server refuses to start with the default secret).
- SQLite tuned for many people at once (WAL, 15 s write wait).
- Windows scripts: `open_firewall.bat`, `install_autostart.bat`, `backup_baidesk.bat`, `schedule_backup.bat`. New `WINDOWS_SETUP.md`.
- **Daily backups** from the command line (`flask --app app backup`) with automatic pruning.
- `tools/loadtest.py`: simulates many agents against a throwaway database.

**People and accounts**
- **Teams** with leads (Administration → Teams). Team leads see only their own team in Coaching and can only review their own team's tickets. A manager with no team, and admins, see everyone.
- **Staff account import** (CSV) with validate-only preview, teams, leads, departments, capacity and **one-time temporary passwords** shown once and downloadable.
- Passwords set by an administrator (new users, resets, imports) are temporary: the person must choose their own at first sign-in.
- **Login lockout**: 15 minutes after 5 wrong passwords for an address (or 30 from one computer); a successful sign-in clears the count.

## 1.4.1 — Auto-assign by department and need
- Auto-assign now matches on **department and the ticket's need (its category)**. Agents have a new *Handles these needs* setting (Administration → Users → edit). Order: agents who handle the category → generalists (no skills ticked) → anyone with free capacity; the least-loaded wins within each group. Away agents and agents at capacity are never chosen.
- The department is **inferred from the category** when none is picked (set a category's department under Departments & categories). With neither, any available agent can take the ticket. The reason for each automatic assignment is written to the ticket history.
- **The queue keeps moving**: tickets nobody could take are assigned automatically, most urgent first, when an agent marks themselves available, when someone resolves a ticket and frees capacity, and on the regular background check. A ticket someone deliberately set back to *Unassigned* is left alone (the "Assign to best available agent" button still works on it).
- Fix: background jobs and scripts can now write activity without an HTTP request.

## 1.4.0 — Workflow & operations
**SLA you can trust**
- Deadlines now respect **business hours and holidays** (previously "now + minutes", 24/7, even though the tables existed). New admin tabs: *Business hours* (per weekday) and *Holidays* (with a one-click Uganda fixed-date preset). Each SLA rule can opt out of business hours.
- The SLA clock **pauses while a ticket is Pending** (waiting on the customer) and resumes with the deadline pushed out by the paused time. Paused tickets are never counted overdue on the dashboard, queue or reports.
- First-response deadlines are stored and shown on the ticket. Priority changes recalculate deadlines.
- **SLA alerts**: assignees are notified 30 minutes before a deadline, managers and assignees when it is breached, once per ticket.

**Routing & capacity**
- Per-agent ticket capacity and an Away/Available toggle (account menu). Optional per-department capacity: the dashboard workload card now shows "52 / 65 · 13 left".
- **Auto-assign** new tickets to the least-loaded available agent in the department (switch in Administration → General), plus an "Assign to best available agent" button and a bulk action.

**Ticket workflow**
- Customer context panel on every ticket: phones, lifetime and last-30-day ticket counts, other open tickets, last contacts.
- One timeline for replies, internal notes and logged **calls / SMS / WhatsApp / walk-ins**; an outbound contact counts as the first response.
- **Customer replied** note type reopens resolved or pending tickets, restarts the clock and notifies the owner.
- Duplicate warning when the same customer already has a similar open ticket (14 days), with "Create anyway".
- **Resolve & next ticket**; keyboard flow: `j`/`k` move through the queue, `Enter` opens, `r` jumps to the reply box.
- **Saved views** (personal or shared with the team) beside the built-in ones.

**Operations**
- **Promises to pay**: record from a ticket, track due / upcoming / kept / broken, reminders on the due date, kept-rate. Shown on the customer page.
- **Coaching**: per-agent scorecard (resolved, first reply, resolution time, SLA met, reopened, promises kept) and a stable sample of unreviewed resolved tickets to score 1–5 with notes. Agents see only their own card.
- **What changed this week** on Reports: plain-language explanation of volume, backlog, first-reply speed, late tickets, unowned tickets and promises.

**Data**
- **Backup**: one-click zip of the database and uploads (consistent snapshot while running).
- **CSV import** for customers and tickets with a *validate only* dry run, row-level error reports, duplicate skipping, and downloadable templates.
- Existing databases are upgraded automatically (additive columns only).

## 1.3.0 — UI refresh
- New look matching the reference design: deep-navy sidebar with SVG icons and account menu, breadcrumb + centred search (Ctrl/⌘+K) + notification bell, round avatars, softer cards, bundled Inter font (works offline).
- Dashboard rebuilt: KPI cards with sparklines and "vs yesterday" trends, 7-day ticket volume chart with hover tooltip, SLA health ring, Needs attention table, Team workload, Recent activity. Backed by new `app/dashboard_data.py`.
- New users default to the light theme; existing users keep their saved theme.
- Fix: Tickets/Customers filter bars no longer stack one control per row.

## 1.2.0
**Reports & export**
- Reports page: date range (with quick presets), department filter, KPIs (created, resolved, open/overdue now, average first response, average resolution, SLA met), daily volume chart, and breakdowns by status, priority, department, agent and category.
- Export to CSV (Excel-ready, UTF-8): full summary, ticket details, or any single table. The ticket queue (current filters) and customer list can also be exported. Print / Save as PDF layout. Exports are audited and protected against spreadsheet formula injection.

**Ticket queue & workflow**
- Saved views (All, Open, Mine, Unassigned, Overdue, Escalated), sortable columns, pagination, bulk assign / status / priority.
- Edit ticket subject, description and category. Reply box with Public / Internal tabs and canned responses. Attachments on ticket creation and on interactions.
- Escalation "Return" hands the ticket back to the originating department; accepting moves it to In Progress.
- Dashboard work lists (unassigned, overdue, mine); KPI cards link to the matching queue.

**Admin & profile**
- Edit users (role, departments, active, password reset) with last-admin and self-lockout protection. Manage departments, categories, SLA targets, canned responses and company name. Audit log viewer. Profile page with password change.

**Fixes**
- SLA countdown was off by the viewer's UTC offset; it now uses real UTC, and stops for resolved tickets.
- All times display in `DISPLAY_TZ` (default Africa/Kampala), with relative labels such as "2h ago".
- Agents who escalated a ticket out of their department no longer land on a 403 page.
- Replies, notes and escalations now update the ticket's "updated" time.
- Theme applied before first paint (no flash) and saved per user. Skip link, focus styles, ARIA labels.
- Fixed horizontal page scroll on phones (wide tables now scroll inside their own container).
- Replaced deprecated `datetime.utcnow()`; added security headers; friendly 400/404/413/500 pages.

## 1.1.1 (bug-fix release)
- Fixed startup crash/SAWarning in `seed()`: the admin `User` is now added to the session before its roles/departments are assigned.
- Fixed the same ordering bug in the admin "Create user" route.
- Seeding is idempotent and safe if two workers start at once.
- Added CSRF protection to all POST forms.
- `.env` is now actually loaded (python-dotenv was installed but never called).
- Uploads are now permission-checked per ticket/customer instead of open to any signed-in user.
- Escalation actions follow a valid state flow (Pending -> Accepted -> Resolved); fixed "resolveed" wording.
- First-response time is recorded on the first public reply.
- Bad input returns 400/404 instead of a 500; session lifetime is bounded; debug mode is configurable.
- Replaced legacy `Query.get`; added SQLAlchemy to requirements; expanded test suite (SAWarnings are errors).
