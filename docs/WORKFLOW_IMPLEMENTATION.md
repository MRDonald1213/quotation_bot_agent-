# Freight quotation workflow implementation

Approved scope implemented on 2026-11-08. The colored visual system, rounded controls and plain HTML/CSS/JavaScript stack are retained.

## Delivered

| Plan area | Implementation |
|---|---|
| Navigation and information architecture | Horizontal Dashboard, Quotations, Comparison, Archived and Settings; one quotation workspace; contextual Supplier Assistant; old duplicate routes redirect |
| Received quotation queue | Received documents and extracted quotations share the main list, with an explicit read/review action; unrelated documents are set aside; active/in-progress tabs, search, sorting and stage filtering |
| Universal format | Nullable supplier ID, price/currency/unit, transport/fuel, cargo/weight/volume/dimensions, contacts, validity, conditions and original source associations |
| Evidence separation | Source-grounded extracted values are separate from supplier database matches and operator destination corrections; parser-derived locations override model page claims |
| Intake | Restored bounded document upload, duplicate choices, local parsing, classification gates and extraction; supported PDF/XLSX/DOCX/TXT/EML/CSV formats |
| Supplier replies | Stable ordered question IDs, quotation-version context, formal/short/numbered/comma-separated answers, local-model interpretation for informal answers, explicit ambiguities and contextual confirmations |
| Merge and validation | Only relevant fields update; conflicting scalar/charge/service values are retained as candidates; human decisions require reasons; deterministic revalidation resolves answered gaps; new quotation versions invalidate approval |
| Drafts and sending | Drafts persist locally without Gmail; recipient/content editing, explicit content approval, typed SEND and final sending; edits invalidate approval; original sent question order/context retained |
| Delivery reliability | Conditional send claim, repeat-request idempotency and a locked uncertain-result state requiring a human Gmail Sent check; no automatic retry/send |
| Approval and import | Shared CLI/API readiness gate, independent approval and idempotent import, optional reviewed supplier CSV append, destination journal and serialized CSV writes |
| Archive | Separate rejected/invalid/archived dispositions; fixed reason category plus explanation; preserved read-only original sources, source download, correspondence and versions |
| Comparison and assistant | Shared deterministic eligibility with CLI/reports, explicit FX, compatible routes/scopes/pricing units, visible exclusions, price/speed highlights and explanations, price/time trade-offs, truthful limits on risk claims; supplier database lookup |
| Demo login | admin / 1234, server-side API protection, HttpOnly SameSite session, clear incorrect-credential error and logout |
| Frontend modules | Dedicated quotation, intake, draft, comparison, assistant, settings, connections and login scripts; old app.js reduced from roughly 2,450 lines to a shared shell/router |
| Documentation | Updated product brief, constraints, PRD, technical specification, data model, README and environment example |

## Verification

Final result: **105 tests passed** (64 existing FastAPI lifecycle deprecation warnings). Python compilation, all JavaScript syntax checks and `git diff --check` passed. Browser QA completed the full flow with **zero JavaScript errors** at 1920×1080 and 1280×900.

Screen scan result:

```json
{"absolute_paths": [], "personal_host_markers": [], "forbidden_claims": [], "unexpected_email_domains": []}
```

Screenshots are saved in `video/demo-data/workflow-v9/screenshots`; the machine-readable scan is `video/demo-data/workflow-v9/screen-scan.json`.


Run from `freight-agent/`:

```bash
python3 -m pytest -q
python3 -m compileall -q src scripts
node --check src/freight_agent/web/static/app.js
```

JavaScript syntax checks cover every page module. `git diff --check` validates whitespace.

The regression suite uses temporary SQLite databases/workspaces and automatic FakeLLM/FunctionModel substitution, plus a mocked Gmail transport. It covers the requested complete/missing/unlabeled quote scenarios, contextual multi-answer and confirmation handling, conflicts, completed replies, unknown currency/units, incompatible prices, cheapest/fastest highlighting DTOs, archive dispositions, explicit SEND, draft edits, send idempotency/reconciliation, additive v8 migration, classification gates, source locations, local draft persistence and source access.

For browser proof, start the isolated synthetic profile in one terminal:

```bash
python3 scripts/workflow_demo.py --reset
```

Then, with Playwright installed for development QA:

```bash
python3 scripts/workflow_browser_check.py
```

The demo and screenshots use only `video/demo-data/workflow-v9`. The script checks 1920×1080 and 1280×900, captures the full quotation → clarification → response → approval → import → comparison flow, verifies supplier database connection and lookup, and tests a real archive decision. It scans rendered screens for absolute paths, personal host markers, unexpected email domains and forbidden savings/vector-store claims. No production database is read or migrated by this profile.

## Operational boundaries

- Real Gmail OAuth/delivery and the accuracy/latency of the user's chosen Ollama model are not claimed as live-verified. Gmail tests use a mock; extraction/interpretation tests use FunctionModel. No real email was sent during implementation.
- Inference stays local. There is no cloud fallback or browser-to-Ollama connection.
- A bare dollar sign does not establish USD. Unknown currency, pricing basis, required cost or FX remains unknown/unrankable.
- Connected supplier CSV import appends an explicitly reviewed record and preserves prior rows. It does not write to an ERP/TMS or silently replace supplier facts.
- Scanned/image-only documents without readable text, legacy DOC/XLS and unsupported formats require conversion/manual review. They are not presented as successfully parsed.
- Risk ranking requires verified risk data and an agreed policy. The assistant explains this limitation instead of inventing a score.
- Demo credentials are not production authentication. Deployment requires real account/identity management, hashed credentials or an identity provider, transport/session protection and rate limiting. Disabling demo login does not secure a deployment.

## Compatibility

Canonical drafts reuse the existing local question-set/draft tables, so saving works while Gmail is disconnected. Email transport records are only needed for explicit approval/sending.

Existing API response keys remain available. Supplier action responses add separate source-only `extracted_fields` and `database_matches`. Approval now records `approved`; destination writing requires the separate import action. Legacy local drafts remain available for review/export. Legacy review/import entry points delegate to the shared workspace service. `/api/status` keeps its keys but masks database/workspace paths as `configured`.
