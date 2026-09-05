# Data Model & Schema Specification

This document provides the definitive schema contracts for all raw ingestion feeds and the unified `ReconciliationRecord` master schema.

---

## 1. Raw Ingestion Schemas

The pipeline reconciles two independent data sources: the merchant payment gateway ledger and the acquiring bank settlement statement.

### Merchant Ledger (`data/merchant_ledger.csv`)
Represents the source-of-truth transaction records from the payment gateway (Razorpay API).

| Column Name | Inferred Type | Enforced Type | Nullable | Description |
|---|---|---|---|---|
| `payment_id` | `Utf8` | `pl.Utf8` | No | Unique Razorpay payment identifier (e.g. `pay_4d4654f7ee6b41`). |
| `customer_name` | `Utf8` | `pl.Utf8` | Yes | Counterparty or enterprise customer name (e.g. `Wayne Enterprises`). |
| `customer_email` | `Utf8` | `pl.Utf8` | Yes | Contact email associated with the transaction. |
| `gross_amount` | `Float64` | `pl.Float64` | No | Total order transaction amount in INR charged to the customer. |
| `mdr_fee` | `Float64` | `pl.Float64` | No | Merchant Discount Rate (gateway processing fee in INR). |
| `gst_on_mdr` | `Float64` | `pl.Float64` | No | 18% Goods & Services Tax levied on the MDR fee. |
| `refund_amount` | `Float64` | `pl.Float64` | Yes | Total refunded amount in INR (coalesced to `0.0` in fee calculations). |
| `payment_date` | `Date` | `pl.Date` | No | Date payment was captured by the gateway (`YYYY-MM-DD`). |
| `status` | `Utf8` | `pl.Utf8` | No | Transaction state: `captured`, `refunded`, or `failed`. |

---

### Bank Settlement Statement (`data/bank_settlement.csv`)
Represents actual payout credits appearing on the merchant's corporate bank statement.

| Column Name | Inferred Type | Enforced Type | Nullable | Description |
|---|---|---|---|---|
| `settlement_utr` | `Utf8` | `pl.Utf8` | No | Unique Transaction Reference (UTR) assigned by NEFT/RTGS/IMPS (e.g. `SETL202608115151`). |
| `transaction_description` | `Utf8` | `pl.Utf8` | No | Raw bank narrative string with varying obfuscation, prefixes, and noise. |
| `net_amount` | `Float64` | `pl.Float64` | No | Actual credited funds received into the account in INR. |
| `settlement_date` | `Date` | `pl.Date` | No | Date the funds were credited to the bank account (`YYYY-MM-DD`). |

---

## 2. Master Schema: `ReconciliationRecord`

Defined as a Pydantic model in `src/reasoning.py` and exported to `data/master_reconciliation_records.parquet`:

```python
class ReconciliationRecord(BaseModel):
    record_id: str
    record_type: Literal["ledger", "bank"]
    status: Literal["Verified", "Exception", "Manual Review", "No Settlement Expected"]
    resolved_at_tier: str
    resolution_method: str
    confidence: float
    customer_name: Optional[str] = None
    ledger_payment_id: Optional[str] = None
    bank_settlement_utr: Optional[str] = None
    gross_amount: Optional[float] = None
    net_amount: Optional[float] = None
    amount_delta: Optional[float] = None
    payment_date: Optional[str] = None
    settlement_date: Optional[str] = None
    notes: Optional[str] = None
```

### Column Reference

| Field | Type | Description |
|---|---|---|
| `record_id` | `str` | Primary key. Populated with `payment_id` for ledger-anchored rows and `settlement_utr` for bank-anchored rows. |
| `record_type` | `Literal["ledger", "bank"]` | Identifies which side of the ledger this row represents. |
| `status` | `Literal[...]` | Primary operational state (4 valid values). |
| `resolved_at_tier` | `str` | Exact tier where the record reached resolution. |
| `resolution_method` | `str` | Algorithmic method identifier. |
| `confidence` | `float` | Metric from `0.0` (unresolved orphan) to `1.0` (exact verification). |
| `customer_name` | `str \| None` | Counterparty name (ledger customer or bank extracted entity). |
| `ledger_payment_id` | `str \| None` | Razorpay payment ID (null for bank-only credits). |
| `bank_settlement_utr` | `str \| None` | Bank settlement UTR (null for ledger-only orphans). |
| `gross_amount` | `float \| None` | Ledger gross transaction amount in INR. |
| `net_amount` | `float \| None` | Bank settled net amount in INR. |
| `amount_delta` | `float \| None` | Absolute difference between expected net and bank net ($|\text{expected} - \text{actual}|$). |
| `payment_date` | `str \| None` | Date payment was captured (`YYYY-MM-DD`). |
| `settlement_date` | `str \| None` | Date bank settlement cleared (`YYYY-MM-DD`). |
| `notes` | `str \| None` | Human-readable explanation for audit logs and exception triage. |

---

## 3. Strict Field Nullability Invariants

To prevent regressions (such as an earlier defect where Tier 3 verified rows omitted `gross_amount`), the pipeline enforces explicit nullability contracts depending on `record_type`:

| Record Type | Scenario / Tier | `gross_amount` | `net_amount` | `amount_delta` | `ledger_payment_id` | `bank_settlement_utr` |
|---|---|:---:|:---:|:---:|:---:|:---:|
| **`ledger`** | **Tier 1 Verified** | **Populated** | **Populated** | **Populated** | **Populated** | **Populated** |
| **`ledger`** | **Tier 1 Amount Mismatch** | **Populated** | **Populated** | **Populated** | **Populated** | **Populated** |
| **`ledger`** | **Tier 3 Action 1 Verified** | **Populated** | **Populated** | **Populated** | **Populated** | **Populated** |
| **`ledger`** | **Tier 3 Action 2 Verified** | **Populated** | **Populated** | **Populated** | **Populated** | **Populated** |
| **`ledger`** | **Tier 5: Failed Payment** | **Populated** | *NULL* | *NULL* | **Populated** | *NULL* |
| **`ledger`** | **Tier 5: Unsettled Receivable** | **Populated** | *NULL* | *NULL* | **Populated** | *NULL* |
| **`bank`** | **Tier 3 Precision Trap** | *NULL* | **Populated** | **Populated** | *NULL* | **Populated** |
| **`bank`** | **Tier 4 AI Bundle Verified** | *NULL*\* | **Populated** | *NULL* | *NULL*\* | **Populated** |
| **`bank`** | **Tier 4 Skipped / Ambiguous** | *NULL* | **Populated** | *NULL* | *NULL* | **Populated** |
| **`bank`** | **Tier 5: Unexplained Credit** | *NULL* | **Populated** | *NULL* | *NULL* | **Populated** |

*\*Note for Tier 4 Bundles: Because a bundled settlement spans multiple ledger invoices (e.g. 3 payment IDs to 1 UTR), the primary bank-anchored row stores the total `net_amount`, while the constituent payment IDs and explanations are embedded inside `notes`.*

---

## 4. Exhaustive Categorical Domain

Below is the complete, closed-world list of all valid categories across the codebase:

### Valid Values for `status`
1. `Verified` — Cleared either deterministically or via confirmed AI bundle resolution.
2. `Exception` — An unresolved mismatch, precision trap, or missing counterparty item requiring operational triage.
3. `Manual Review` — Bounded bundle searches that exceeded pool size limits or yielded low confidence scores.
4. `No Settlement Expected` — Ingested gateway transactions whose initial state was `failed`; excluded from the matchable denominator.

### Valid Values for `resolved_at_tier`
| Category String | Status | Resolution Method Code | Emitted By |
|---|---|---|---|
| `Tier 1: Exact` | `Verified` | `exact_id_join` | Tier 1 Exact Match |
| `Tier 1: Exact ID, Amount Mismatch` | `Exception` | `exact_id_amount_exceeded_tolerance` | Tier 1 Exact Match |
| `Tier 3: Fuzzy+Math Verified` | `Verified` | `tier2_candidate_verified` | Tier 3 Action 1 |
| `Tier 3: Math-Only` | `Verified` | `expected_net_match` | Tier 3 Action 2 |
| `Exception: Math Match - Identity Mismatch` | `Exception` | `math_matched_identity_vetoed` | Tier 3 Action 2 (Precision Trap) |
| `Tier 4: AI Bundle` | `Verified` | `subset_sum_llm` | Tier 4 Bundle Resolution |
| `Tier 4: Skipped - Pool Too Large` | `Manual Review` | `pool_exceeded_bound` | Tier 4 Pre-filter (Pool > 20) |
| `Ambiguous Bundle - Manual Review Required` | `Manual Review` | `low_confidence_bundle` | Tier 4 LLM Ranking (Conf < 0.75) |
| `Tier 4: LLM Call Failed` | `Manual Review` | `llm_error_fallback` | Tier 4 Exception Fallback |
| `Tier 5: No Settlement Expected` | `No Settlement Expected` | `failed_status_excluded` | Tier 5 Orphan Categorization |
| `Tier 5: Unsettled/Pending Receivable` | `Exception` | `no_bank_match_found` | Tier 5 Orphan Categorization |
| `Tier 5: Unexplained Bank Credit` | `Exception` | `no_ledger_match_found` | Tier 5 Orphan Categorization |
