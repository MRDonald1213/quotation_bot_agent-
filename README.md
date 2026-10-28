# AI Freight Agent — local CLI + web app (Workflows A, B and C)

Local-first command-line application that imports freight quotation documents/emails,
classifies them, extracts structured quotation data **with source evidence**, validates
it, persists it to SQLite, and reports data gaps that block fair carrier comparison.
It automatically creates carrier-specific clarification drafts after processing a quote,
merges supplier replies with evidence, quotation history, and recalculated costs, and
builds a deterministic cross-carrier comparison with an advisory recommendation.

Implements **Workflow A** of `Freight_AI_Agent_Technical_Specification_CLI_MVP.docx`
(§5): create shipment → import document → local text extraction + provenance →
PydanticAI classification → extraction agent → domain validation → quote + field
evidence persisted → gap analysis → CLI display of quote, confidence and gaps.

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running locally with a tool-capable model
  (e.g. `ollama pull qwen3.5`; check with `ollama list`). The model name is
  **configuration, never hard-coded**. No cloud LLM fallback exists: if Ollama is
  unreachable the app errors out with an actionable message.

## Installation

```bash
cd freight-agent
python3 -m pip install -e .
cp .env.example .env          # then set OLLAMA_MODEL=<your installed model>
freight init                  # creates data/workspace, SQLite schema (versioned)
freight health                # SQLite + Ollama + model + parser capability check
freight review                # local web app on http://127.0.0.1:8765/
```

`pip install -e .` also installs the web stack (FastAPI, Uvicorn, python-multipart); the
page itself needs no Node, no bundler and no internet access. Everything below works the
same from the CLI or the browser — the web app is a second front door over these services,
not a separate system.

## Sample workflow (Workflow A)

`freight intake add` classifies, extracts, validates and resolves the route in one
step, so the normal path is two commands per document. Wherever a command takes a
shipment or intake ID you may write `last` instead — it means the most recently
created record, so no ID ever has to be copied.

```bash
# 1. import a quotation file (.txt .eml .pdf .docx .xlsx .csv); the route is
#    matched to an existing shipment or proposed for approval automatically
freight intake add tests/fixtures/sample_quotes/quote_a_ocean_allline.txt
freight intake add ~/Downloads/Freight_Quotation.docx
freight intake add some_quote.eml --source email

# 2. approve a newly proposed route (shows origin/destination/cargo first)
freight shipment review                # pending proposals
freight shipment approve last -y
freight shipment edit last --origin "Rotterdam, Netherlands"   # correct a proposal

# 3. inspect / re-run
freight intake show last               # classification verdict + reason
freight intake process last [--dry-run] [--force]
freight gaps last                      # per-quote gaps, severity, questions
freight quote show <quote-id>          # quote + confidence + evidence
freight comparison last                # cross-carrier comparison + recommendation

# 4. utilities
freight config show
freight db backup <path>               # consistent backup via SQLite backup API
```

Duplicate imports are detected by SHA-256 and blocked; re-run with
`--on-duplicate skip|reprocess`. A low-confidence classification is retried once
automatically before a human review is requested.

## Local web interface (one app for workflows A, B and C)

```bash
freight review                        # serves and opens http://127.0.0.1:8765/
freight review --port 8801 --no-open  # different port, no browser launch
```

`freight review` runs the same application the CLI uses: a FastAPI backend that calls the
existing services (`services/intake`, `services/extraction`, `services/review`,
`services/shipments`, `services/clarification`, `services/comparison`) plus a plain
HTML/CSS/JS page served by the same process (no React, no build step, no npm). There is no
second data layer and no business logic in the routes or in the browser — every button maps
to a service the CLI already calls, so validation, versioning and the audit trail are
identical whichever way you work. Stop the server with Ctrl-C.

Pages: **Dashboard** (real counts from the database only), **Documents** (drag-and-drop
multi-upload, filters, duplicate handling), the split-pane **Document review workspace**
(document text on the left; extracted fields, evidence, confidence, gaps and clarification
questions on the right), **Review queue**, **Quotations & shipments**, **Comparison**,
**Clarification drafts** and **Settings / system status** (schema version, Ollama reachability,
configured model, size and extension limits, database and workspace paths).

Approved data is written to the database in a deliberately separate step:

1. **Approve review** — records the human decision on the extracted quotation. Nothing is
   committed to the operational tables yet; the quotation stays at `approved`.
2. **Import approved results** — refused until step 1 happened. The button first shows a
   preview listing exactly which records will be created or updated, then writes them. Clicking
   it again returns `already_imported` and creates nothing: the import is idempotent, and
   re-approving or rejecting after an import is refused. **Reject** keeps the quotation at
   `rejected` for the audit trail; nothing is silently discarded.

Safety bounds enforced by the server, not the page:

- bound to `127.0.0.1`; cross-origin state-changing requests are rejected (403);
- the browser talks only to this local backend — it never calls Ollama directly, and no cloud
  LLM provider is reachable from the UI; an `OLLAMA_BASE_URL` that is not a local address is
  refused on the Settings page rather than silently used;
- file extensions, `MAX_FILE_SIZE_MB` and parser limits are checked server-side; oversize or
  unsupported uploads are rejected per file, the rest of the batch still imports;
- document text is displayed as escaped data and is passed to the model only inside the
  `<UNTRUSTED_DOCUMENT>` boundary, so instruction-like text in a file cannot steer the agent;
  macros, scripts and embedded links are never executed;
- LLM work runs in a background job per document (the page polls `/api/jobs/{id}`), so a slow
  model never blocks the UI, and a failed job is reported instead of guessed;
- nothing is sent anywhere: drafts can be approved and exported to a local `.txt`/`.eml` file
  only, and no email, ERP write or other external request is automated;
- responses carry previews and evidence snippets, not full document bodies, and secrets are
  never logged.

First end-to-end path (the whole flow in one sitting):

1. `freight init` once, then `ollama serve` in another terminal (and `ollama pull <model>`,
   with `OLLAMA_MODEL=<model>` in `.env`).
2. `freight review` → **Documents** → drag `tests/fixtures/sample_quotes/quote_a_baltic_lines.txt`
   (or a real `.docx`/`.pdf`/`.xlsx`/`.eml`) in → **Run processing**. A second file of the same
   content is reported as a duplicate with skip/reprocess options.
3. When the job finishes, open the document: the **Review workspace** shows the source text next
   to the extracted fields, each with its verbatim evidence, confidence, the open gaps and the
   generated clarification questions. Accept or reject individual evidence rows, and waive a gap
   with a reason if the carrier will not answer it.
4. If the route is new, approve or correct the proposed shipment in the same pane.
5. **Approve review**, then **Import approved results to database** and confirm the preview.
6. **Quotations & shipments** → open the shipment → **Comparison**: totals, unknown costs and
   caveats come from the deterministic Python service (no browser arithmetic, no currency
   conversion without an explicit `FX_RATES_JSON` rate); export the report as markdown, JSON or
   CSV.
7. **Clarification drafts** lists the auto-generated drafts for the gaps that still matter;
   approve and export one — sending stays a human action outside this app.

### Troubleshooting

- `cannot start the review app on port 8765` — the port is taken; use `--port 8801`, or a
  previous `freight review` is still running (find it with `lsof -i 8801` and stop the old one
  with Ctrl-C in its terminal).
- Settings page shows Ollama unreachable, or a job fails with a model error — start it
  (`ollama serve`), confirm the model exists (`ollama list`) and that `OLLAMA_MODEL` matches it;
  `freight health` prints the exact check that failed. The app never falls back to a cloud model.
- Upload rejected as unsupported/oversize — the file type must be one of the extensions listed
  on the Settings page and the file must be under `MAX_FILE_SIZE_MB` in `.env`; raise the value
  and restart the app if the real documents genuinely need more.
- "Duplicate" banner on upload — the content hash already exists in the database; choose
  *skip* to reuse it or *reprocess* when the previous attempt was wrong (a new intake record is
  created and the old quotation version is kept).
- Data lives outside the code: SQLite at `data/freight.db`, imported copies and exports under
  `data/workspace/`. `freight init` migrates the schema in place (v4 adds the review/import
  state and baselines older rows as already imported); `freight db backup <path>` takes a
  consistent copy. `data/` and `.env` are git-ignored — never commit them.

## Clarification loop (Workflow B)

Run `freight init` once after upgrading: schema v2 adds clarification/reply tables and
baselines existing quotation history without deleting data.

Quote processing already creates questions and drafts for material gaps; supplier
reply import automatically extracts grounded updates, preserves unchanged facts,
recalculates costs and gaps, and creates any remaining follow-up drafts.

```bash
freight questions generate <shipment-id>  # also prepares drafts for existing quotes
freight questions list <shipment-id>
freight email list <shipment-id>
freight email show <draft-id>
freight email edit <draft-id>             # opens a local editor; approval is reset
freight email approve <draft-id>          # explicit human confirmation; never sends
freight email export <draft-id> --format eml --to carrier@example.com
freight reply import <shipment-id> reply.eml
freight reply import <shipment-id> reply.txt --quote <quote-id>
freight quote history <quote-id>
```

`questions generate` and `reply import/reprocess` support `--dry-run`; no database
updates, imported file copies, or exports are created. An ambiguous reply association
shows candidate quotations and requires selection (or `--quote` non-interactively).
Failed reply extraction leaves the previous quote intact and the reply available for
`freight reply reprocess <reply-id>`. Drafts for old quote versions cannot be exported;
editing any draft/question invalidates approval. Export creates a new local `.eml`
or `.txt` file only, with no attachments, never overwriting an existing file.

## Command behaviour and design rules (from the spec)

- **LLM output is untrusted** until Pydantic schema validation *and* a domain
  validator (currency codes, monetary values, dates, impossible values, evidence
  grounding) pass. Failures after bounded retries mark the item `needs_review` /
  `failed` — never invent a result.
- **Evidence**: every extracted field carries the source filename, page/sheet,
  verbatim snippet and confidence. Fields extracted without a verbatim-grounded
  snippet are flagged for review (`freight quote show`).
- **No LLM arithmetic**: `total_known_cost` is computed deterministically and only
  sums charges confirmed as additional, numeric, in the quote currency, and not
  already included in the base price. An unpriced mandatory charge blocks the
  total and the quote is `incomplete` — it is never treated as zero.
- **Unknown is None, not guessed**; no currency conversion without explicit FX data.
- **Prompt injection safety**: document text is delimited as
  `<UNTRUSTED_DOCUMENT>` data and the system prompts forbid following instructions
  inside it. A test fixture (`unrelated_newsletter.txt`) contains an injection
  attempt; classification is unaffected and, per spec, low-confidence/ambiguous
  items go to `needs_review` instead of being discarded.
- **Privacy**: all inference runs through the configured local Ollama endpoint
  only; `freight health` sends no document text anywhere. Logs mask email
  addresses even with `--verbose`. Imported files live in an explicit workspace
  directory the user can delete.
- **Human in the loop**: nothing is ever sent; `--force` overrides are audited;
  every status change is written to the `audit_events` table.
- Non-zero exit codes per failure class (2 config, 3 model, 4 LLM output,
  5 parsing, 6 duplicate, 7 validation, 8 not found).

## Repository layout

Per spec §14: `src/freight_agent/{cli,cli_commands,services,agents,parsers,domain,db}`,
plus `api/` (FastAPI app factory + background job runner) and `web/static/`
(`index.html`, `styles.css`, `api.js`, `app.js` — the plain page the backend serves; the
static files live inside the package, so `pip install -e .` and the wheel ship them with it).
Persistence: SQLAlchemy 2.x + SQLite (`PRAGMA foreign_keys=ON`), money stored as
text, timestamps ISO-8601 UTC, versioned schema in `schema_version`, indexes on
shipment IDs, intake hashes, carrier names, statuses, quote validity.

## Tests

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest -q              # unit + integration, no Ollama needed (FunctionModel double)
```

Coverage includes the LLM output contracts (schema validation, evidence grounding, prompt
injection boundaries, dry-run), the A/B/C regressions, and the web API: upload limits and
duplicate handling, job status, approve/import separation and idempotency, comparison rows
computed by the service, and the cross-origin rejection.

## Scope / limitations of this MVP

Workflows A (intake → quote), B (clarification loop) and C (cross-carrier comparison +
report export) are implemented, in the CLI and in the local web interface. ERP/TMS
integration and email sending remain out of scope — the app prepares and exports drafts,
a human sends them. Ambiguous intake classifications stop at `needs_review`;
`--force` records a human override. Replies without a unique explicit quotation
reference require a human selection; new reply facts without grounded evidence are
not used to overwrite confirmed fields. Charge matching uses exact normalized labels,
not fuzzy guesses. Unknown charges and currency mismatches remain unresolved.
Comparison never sums across currencies unless an explicit `FX_RATES_JSON` rate is
configured, never ranks expired/incomplete/scope-mismatched quotes, and the
recommendation is advisory — a human makes the final carrier decision.
