# SoftClinch AMS Desk: SAP Help Desk (Python / Flask)

A self-hosted help desk for SAP Application Management Services. Clients raise SAP
issues, consultants work them against SLAs, and resolved tickets become knowledge
base articles.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py --demo               # loads demo data, then serves http://127.0.0.1:5000
```

Demo sign-ins (password `Demo@1234`):

| Email | Role |
|---|---|
| admin@demo.local | Admin (AMS delivery manager) |
| arjun@demo.local | Consultant, FI/CO |
| kavya@demo.local | Consultant, MM/SD/WM/QM |
| rahul@demo.local | Consultant, ABAP/Basis/Security/Integration/Fiori |
| ravi@nilgiri.demo | Client user, Nilgiri Textiles |

To start clean instead, run `python app.py` without `--demo`; the first visit asks you
to create the admin account.

## What it does

**Tickets built for SAP support**
- SAP module (FI, CO, MM, SD, PP, QM, PM, WM/EWM, PS, HCM, SuccessFactors, ABAP, Basis,
  Security/GRC, BW, Fiori, Integration), category (Incident, Service Request, Change
  Request, Problem, Authorization), system (PRD/QAS/DEV), client number, T-code,
  exact error message, business impact, users affected
- Numbering by category: `INC-000123`, `SR-…`, `CR-…`, `PRB-…`, `ACC-…`
- Transport request and SAP Note / SAP case fields, root cause and resolution
- Screenshot and log attachments
- Full audit history of every status, priority and assignee change

**Workflow**
- Statuses: New → Assigned → In Progress → Awaiting Customer / Awaiting SAP → In Testing → Resolved → Closed
- Auto-assignment to the least-loaded consultant who covers the ticket's module
- Internal notes hidden from clients; staff can reply and change status in one step
- A client reply on an Awaiting Customer ticket moves it back to In Progress
- Clients confirm a resolution (closes the ticket) or reopen it with a reason
- Resolved tickets cannot be saved without a written resolution

**SLA engine**
- First-response and resolution targets per priority (P1 to P4), editable by admins
- Resolution clock pauses while Awaiting Customer or Awaiting SAP
- Live states on every list: on track, at risk, breached, paused, met
- Priority changes recalculate the targets

**Knowledge base**
- Searchable by error number, T-code or keyword; filter by module
- Public (clients see it) or internal (AMS team only) articles
- One click from a resolved ticket to a pre-filled article
- Matching articles suggested on each ticket

**Reports and admin**
- Response and resolution SLA %, average net hours to resolve, open breaches and
  reopen counts, broken down by priority, module, client, consultant and category
- CSV export of any filtered ticket list
- Manage users (client / consultant / admin), client companies and SLA policy

## Roles

| | Client | Consultant | Admin |
|---|---|---|---|
| Raise tickets, reply, attach files | Own company | Any client | Any client |
| See tickets | Own company only | All | All |
| Internal notes, change status/priority/assignee | | ✓ | ✓ |
| Read KB | Public articles | All | All |
| Write KB articles | | ✓ | ✓ |
| Reports and CSV | CSV of own tickets | ✓ | ✓ |
| Users, companies, SLA policy | | | ✓ |

## Configuration

Environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `HELPDESK_NAME` | `SoftClinch AMS Desk` | Name shown in the app |
| `HELPDESK_TZ` | `Asia/Kolkata` | Time zone for displayed times |
| `HELPDESK_DB` | `./helpdesk.db` | SQLite database file |
| `HELPDESK_UPLOADS` | `./uploads` | Attachment folder |
| `HELPDESK_SECRET_KEY` | generated into `.secret_key` | Session signing key |
| `HELPDESK_AUTO_ASSIGN` | `1` | Set `0` to turn off auto-assignment |
| `HELPDESK_HOST` / `HELPDESK_PORT` | `127.0.0.1` / `5000` | Dev server address |

## Running in production

The built-in server is for development. On a Linux server:

```bash
pip install gunicorn
gunicorn -w 3 -b 0.0.0.0:8000 app:app
```

Put Nginx (or IIS/Apache) in front for HTTPS, set `HELPDESK_SECRET_KEY`, and back up
`helpdesk.db` and the `uploads/` folder. SQLite is fine for a team of tens of users;
move to PostgreSQL if you grow well beyond that.

## Files

```
app.py            all routes, SLA engine, access control
demo_data.py      sample companies, users, tickets, KB articles
templates/        Jinja2 pages
static/style.css  styling (light and dark mode)
```

## Ideas for next steps

- Email notifications on new tickets, replies and SLA warnings (smtplib or SendGrid)
- Business-hours SLA calendars per client (e.g. Mon–Sat 9:00–18:00 IST with holidays)
- Inbound email to ticket, and a WhatsApp channel for P1 alerts
- Monthly service report PDF per client
- Contract hours tracking for AMS retainer billing

# sap-support
