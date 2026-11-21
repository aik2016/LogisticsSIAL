# Source mapping and checks — 21 November 2026

The supplied workbooks are operational source files. They are **not** included in this repository or its ZIP. Their contents must remain private.

## Intended grain

| Source | Grain | Intended use |
| --- | --- | --- |
| PO Masterlist.xlsx | One item / due-date row; marketplace PO number may be blank on continuation rows | Seed PO headers and item-line quantities/due dates |
| Sial PO Report - 13 Nov 2026.xlsx | One PO row, with multiple items and invoice references packed into cells | Snapshot of internal status, value, invoice and remarks; not a direct inventory movement |
| Outstanding POs.xlsx | Regional operation rows, sometimes continuation lines, plus invoiced/done tabs | Work queue and cross-check; requires sheet-specific parsing |
| SAP-PO-Report--2026-11-21-123626.xlsx | One SAP PO line | Compare ordered, GR and remaining quantities by PO and line |
| GR Report by Nupco.xlsx | One GR transaction / PO line | Match receipt evidence, including partial receipts |
| Invoicing report by nupco.xlsx | One uploaded Nupco invoice record | Upload status and Nupco invoice number |
| Invoices report by Nupco.xlsx | One invoice posting record | Approval/rejection and amount evidence |
| Statement of Account by Nupco.xlsx | One accounting document | Clearing and payment evidence; references must be matched carefully |

## Observed checks

- The masterlist has 670 item rows across 403 POs: 412 tender rows across 225 POs and 258 marketplace rows across 178 POs. 53 marketplace item rows have a blank PO cell that continues the preceding PO. The importer fills down only within that sheet.
- The Sial report has 395 PO rows: 163 marketplace, 126 tender and 106 outstanding. Its top rows contain summary figures and are not PO records.
- SAP has 405 distinct POs; all 403 masterlist POs appear there, and two SAP POs (`4600200703`, `4300006906`) are absent from the masterlist.
- At whole PO level, 221 of the 403 overlapping POs have matching ordered piece totals. Of the 182 differences, 168 are exactly +1 in SAP. Sample SAP rows show a separate 1-piece line with blank item category; these must be reviewed as possible service/charge lines before counting them as physical stock.
- SAP ordered quantity equals GR quantity plus remaining quantity for all 405 POs when grouped at PO level. This checks arithmetic consistency, not correctness of item identity.
- Real supplier invoice references in the files include numbers and `N...` forms. A single mandatory `M...` or `T...` rule would reject actual history; the prototype now allows nonblank references.
- Tender POs include `4300...` as well as `4600...` numbers.
- The uploaded PDF examples contain catalog codes, pack sizes, customer PO, address and per-item delivery schedules. PDF extraction is not yet automated; it needs a review screen because the documents have bilingual, layout-dependent text.

## Import behavior

The app supports preview and idempotent import of the **PO Masterlist** and **Sial PO Report** formats. Preview reports row counts and errors before any database changes. Reimporting identical bytes does nothing. An existing PO with lines causes a conflict rather than a silent overwrite. Sial statuses are kept in a separate source snapshot table; they do not auto-change inventory or invoice state. This protects stock from historical status text being mistaken for confirmed movements.

## Remaining reconciliation work

Build sheet-specific preview and matching for the Nupco SAP, GR, invoicing, invoices and statement reports. Use PO number plus PO line number for GR/SAP matching. Invoice payment matching must use Nupco document number and supplier invoice reference, with manual review for ambiguous references. Do not infer payment from a PO-level `Fully paid` text alone.

## Value correction

The masterlist's `Total Value` is not consistently a per-item value: it is repeated for some multi-item POs and blank for others. Across 395 POs that also occur in the Sial report, only 170 had matching values when masterlist rows were summed. The importer therefore keeps original masterlist row values as evidence and leaves the PO value unknown until a Sial whole-PO value is available. A direct PDF or Odoo source can later resolve remaining disagreements.

## PDF sample validation

The review-only PDF extractor was checked against all 24 supplied PDFs (22 additional tender examples plus the initial tender and marketplace PDFs). It identified 46 item/schedule blocks without extraction warnings. After grouping by PO, item code and due date, all 46 extracted quantity/date pairs matched the masterlist. This supports the supplied PDF layout only; future PDFs still require preview because Nupco may change its template or provide scanned documents.
