# AI Freight Agent — CLI MVP (Workflows A, B and C)

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
```

## Sample workflow (Workflow A)

```bash
# 1. create a shipment (interactive if flags omitted)
freight shipment create --reference SHP-2026-114 --name "Baku-Hamburg auto parts" \
  --origin "Baku, AZ" --destination "Hamburg, DE" \
  --cargo "12 pallets automotive parts, 8400 kg" \
  --delivery-scope door_to_door --currency EUR

# 2. import quotation files (.txt .eml .pdf .docx .xlsx .csv)
freight intake add <shipment-id> tests/fixtures/sample_quotes/quote_a_baltic_lines.txt
freight intake add <shipment-id> tests/fixtures/sample_quotes/quote_b_caspia.eml --source email

# 3. classify only, or full intake→quote processing (use --dry-run to preview)
freight intake classify <intake-id> [--dry-run]
freight intake process <intake-id> [--dry-run] [--force]

# 4. inspect results
freight intake list [--shipment-id ...] [--status ...]
freight intake show <intake-id>
freight quote list [--shipment-id ...]
freight quote show <quote-id>       # quote + confidence + evidence + gaps
freight quote reprocess <quote-id>  # re-extract; version bumps, no silent duplicate
freight gaps <shipment-id>          # per-quote gaps, severity, suggested questions

# 5. utilities
freight config show
freight db backup <path>            # consistent backup via SQLite backup API
```

Duplicate imports are detected by SHA-256 and blocked; re-run with
`--on-duplicate skip|reprocess`.

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

Per spec §14: `src/freight_agent/{cli,cli_commands,services,agents,parsers,domain,db}`.
Persistence: SQLAlchemy 2.x + SQLite (`PRAGMA foreign_keys=ON`), money stored as
text, timestamps ISO-8601 UTC, versioned schema in `schema_version`, indexes on
shipment IDs, intake hashes, carrier names, statuses, quote validity.

## Scope / limitations of this MVP

Workflows A (intake → quote), B (clarification loop) and C (cross-carrier comparison +
report export) are implemented. ERP/TMS integration, email sending, and web UI
remain out of scope. Ambiguous intake classifications stop at `needs_review`;
`--force` records a human override. Replies without a unique explicit quotation
reference require a human selection; new reply facts without grounded evidence are
not used to overwrite confirmed fields. Charge matching uses exact normalized labels,
not fuzzy guesses. Unknown charges and currency mismatches remain unresolved.
Comparison never sums across currencies unless an explicit `FX_RATES_JSON` rate is
configured, never ranks expired/incomplete/scope-mismatched quotes, and the
recommendation is advisory — a human makes the final carrier decision.
