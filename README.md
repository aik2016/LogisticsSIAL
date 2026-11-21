# Logistics Control prototype

Private prototype for Sial's PO, inventory, invoice and Nupco workflows. See [SOURCE_NOTES.md](SOURCE_NOTES.md) for the supplied workbook mapping and data quality findings.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py create-admin
uvicorn app:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 on the **same computer**. Set `LOGISTICS_DB` to choose a database path. Set `COOKIE_SECURE=1` behind HTTPS. To enable email password reset, configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_USER`, `SMTP_PASSWORD`, and `APP_BASE_URL` as host environment variables. Do not commit those credentials. API documentation is at `/docs` after sign in.

## Admin first-use order

1. Create the admin account with `python manage.py create-admin` before starting the server.
2. Sign in and follow the **Admin setup** screen.
3. Preview/import the PO Masterlist, then the Sial PO report. Check Nupco comparisons and any conflicts.
4. Enter actual starting stock by batch; POs do not create stock.
5. Create staff accounts and assign only their required tools. The admin may revoke access or reset a staff password later.

## Working features

- Admin login, user creation, per-tool permissions and revocation, and admin password reset for users.
- PO headers and item lines with separate due dates, region, address, customer PO and value.
- Item pack sizes, family pallet defaults (RIM/RIB 9, CTND 12, RIMZP 14 boxes per pallet), batches, manufacturing and expiry dates, physical/booked/free inventory.
- Invoice booking by PO line and batch, status transitions, ASN details and returns.
- Booking reserves stock; dispatch retains the reservation; delivery deducts stock; returns restore it to the original batch.
- Overdue list, due reminders, issues with Sada ticket numbers, jobs, basic reports and CSV export.
- Preview and import for the supplied PO Masterlist, Sial PO Report, and five Nupco report layouts, with source snapshots and duplicate detection. Nupco evidence never auto-changes operational statuses.
- Review-only extraction of PO number, item quantities and due schedules from text-based Nupco PDFs; validated on the 24 supplied samples.
- English and Arabic navigation; many form labels remain English.

## Production work still required

Do not expose this prototype to the internet or enter real business data yet. SMTP configuration for email resets, login rate limiting, full audit coverage, secure document storage and extraction, Nupco report reconciliation, Odoo import mapping, scheduled notifications, database migrations/backups, tenant isolation, HTTPS hosting and security review remain. The reminders appear on the dashboard only. Invoice reference rules must be finalized from actual Odoo samples. Nupco workbooks can be stored as evidence, but detailed matching and automatic status proposals are still being built. PDF details are extracted for review but cannot yet be applied directly as a new PO; scanned PDFs need OCR.

GitHub can hold this source, but GitHub Pages cannot run the Python backend. Deploy to a Python-capable host with persistent database storage after the production work above.
