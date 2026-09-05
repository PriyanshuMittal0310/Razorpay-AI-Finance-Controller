# AI Finance Controller — Technical Documentation

This directory contains comprehensive technical specifications, architectural designs, schema definitions, and implementation guides for the **Razorpay Buildathon Track 04 (AI Finance Controller)** submission.

All specifications herein are grounded directly in the actual repository implementation files (`3_reconciliation_pipeline.py`, `reasoning.py`, `qa_agent.py`, `forecaster.py`, `pipeline_timing.py`, `config.py`, and `app.py`).

---

## Documentation Index

| File | Primary Coverage | Intended Audience |
|---|---|---|
| [architecture.md](architecture.md) | End-to-end data pipeline flow, tier-by-tier mechanics (Tiers 0–5), exact numeric thresholds, the Tier 3 precision-trap safeguard, and defensive edge-case failure modes. | Evaluators, systems engineers, pipeline maintainers |
| [data-model.md](data-model.md) | Column-by-column reference for `ReconciliationRecord`, raw input schemas (`merchant_ledger.csv`, `bank_settlement.csv`), nullability invariants across record types, and exhaustive category enumerations. | Data engineers, financial analysts, audit reviewers |
| [design-decisions.md](design-decisions.md) | Architectural rationale behind deliberate constraints: omission of vector embeddings, bounded LLM usage, honest match-rate denominator math, regex confidence gating, and veto calibration. | Hackathon judges, technical leads, architects |
| [qa-agent.md](qa-agent.md) | Grounded text-to-SQL engine: schema context injection, read-only AST safety validation, two-stage generation, proactive/reactive rate limiting, and model configuration. | AI/LLM engineers, security auditors |
| [ui-guide.md](ui-guide.md) | Five-tab Streamlit dashboard walkthrough, design tokens (`PAPER`, `CARD`, `ACCENT`, etc.), dark-mode style isolation, and pill navigation implementation. | UI/UX engineers, frontend developers |
| [screenshots/](screenshots/) | Visual UI captures of all five demo interface tabs: Dashboard, Audit Trail, Cash Forecast, Settlement Q&A, and Datasets. | Evaluators, reviewers, all audiences |

---

## Suggested Reading Orders

### Path A: Comprehensive Overview (Evaluators & New Contributors)
For evaluators seeking to understand the end-to-end architecture, methodology, and design choices:

1. **[architecture.md](architecture.md)** — Start here to understand the 6-stage pipeline (Tier 0 through Tier 5), how deterministic filters precede AI reasoning, and how data flows from messy bank feeds to the master record.
2. **[design-decisions.md](design-decisions.md)** — Understand *why* the pipeline is built this way: the mathematical justification for avoiding embeddings, bounding subset-sum searches, and gating LLM outputs.
3. **[data-model.md](data-model.md)** — Review the unified schema contracts, strict nullability invariants between ledger and bank records, and the full categorical status domain.
4. **[ui-guide.md](ui-guide.md)** — Explore how the reconciliation engine, cash forecaster, and audit trail are surfaced visually in the Streamlit application.
5. **[qa-agent.md](qa-agent.md)** — Examine the implementation of the grounded natural-language-to-SQL interface.

### Path B: Targeted Troubleshooting & Debugging
For engineers maintaining or debugging specific components of the controller:

- **Reconciliation mismatch or orphan triage:**
  Read [architecture.md](architecture.md) (Tiers 1–5 deep dive) and cross-reference with [data-model.md](data-model.md) (*Field Nullability Matrix*).
- **Q&A Agent query generation errors or API rate-limiting (429s):**
  Read [qa-agent.md](qa-agent.md) for the proactive token bucket throttle and retry exponential backoff rules.
- **Cash Forecaster calculation or date bucketing adjustments:**
  Read [ui-guide.md](ui-guide.md) (§ Cash Forecast Tab) and [architecture.md](architecture.md) (§ Tier 5 Unsettled Receivables).
- **UI styling discrepancies or theme regressions:**
  Read [ui-guide.md](ui-guide.md) (§ Design Tokens & CSS Isolation).
