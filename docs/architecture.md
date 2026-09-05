# Architecture & Pipeline Specification

This document provides a comprehensive technical breakdown of the **AI Finance Controller** reconciliation engine, from raw data ingestion to downstream consumption.

---

## 1. System Data Flow

The pipeline operates on an **asymmetric, deterministic-first funnel**. Strict arithmetic and exact joins resolve the bulk of transactions at near-zero compute cost, escalating residual exceptions to fuzzy identity matching, DuckDB mathematical verification, and finally bounded LLM reasoning.

```mermaid
flowchart TD
    subgraph Ingestion["Raw Data Ingestion"]
        ML["merchant_ledger.csv"]
        BS["bank_settlement.csv"]
    end

    subgraph Preprocessing["Sanitization & Pre-processing"]
        DEDUP["Deduplicate bank rows on settlement_utr"]
        CAST["Enforce Polars column schemas (Float64, Date)"]
        EMPTY_CHK{"Empty batch check<br>(0 rows?)"}
        EMPTY_OUT["Write 0-row Parquet & Exit(0)"]
        FILTER_FAILED["Filter ledger status == 'failed'<br>Route to Tier 5 No Settlement Expected"]
    end

    subgraph Tier0["Tier 0: LLM Narrative Extraction"]
        T0_API["Batched Gemini 3.6 Flash Call<br>(Pydantic ExtractedBankData)"]
        T0_REGEX{"Razorpay Regex Gate<br>^pay_[a-zA-Z0-9]{14}$"}
        T0_PASS["Extracted Payment ID accepted"]
        T0_FAIL["ID nullified -> passes to Tier 2 fuzzy"]
    end

    subgraph Tier1["Tier 1: Exact ID Join (Polars)"]
        T1_JOIN["Inner Join on payment_id"]
        T1_MATH{"Amount Delta < ₹0.50?"}
        T1_VERIFIED["Tier 1: Exact (Verified)"]
        T1_EXC["Tier 1: Exact ID, Amount Mismatch (Exception)"]
    end

    subgraph Tier2["Tier 2: Fuzzy Entity + Date Window (RapidFuzz)"]
        T2_CROSS["Cross Join Unresolved Residual"]
        T2_GATES{"Score >= 75.0 &<br>Date Delta <= 3d &<br>Fee-Adjusted Delta <= ₹500"}
        T2_GREEDY["Greedy 1-to-1 Resolution<br>(sort: delta ASC, score DESC, date ASC)"]
        T2_PROP["Tier 2 Proposed Candidates<br>(Unverified)"]
    end

    subgraph Tier3["Tier 3: DuckDB Math Engine & Identity Veto"]
        T3_ACT1{"Action 1: Re-verify Tier 2<br>delta < ₹0.50?"}
        T3_ACT1_OK["Tier 3: Fuzzy+Math Verified"]
        T3_ACT1_FAIL["Release back to pending pools"]
        T3_ACT2["Action 2: Cross Join Pending Pools<br>Search expected_net == net_amount (±₹0.50)"]
        T3_VETO{"Identity Score >= 60.0?"}
        T3_ACT2_OK["Tier 3: Math-Only Verified<br>(Corporate Aliasing)"]
        T3_TRAP["Exception: Math Match - Identity Mismatch<br>(Precision Trap Safeguard)"]
    end

    subgraph Tier4["Tier 4: Bounded Subset-Sum & Reasoning LLM"]
        T4_PRE{"Candidate Pool <= 20 &<br>Combinations (size 2-3)<br>Sum == net_amount ±₹0.50"}
        T4_LLM["Reasoning LLM Ranking<br>(Gemini 3.6 Flash + BundleResolution schema)"]
        T4_CONF{"Top Confidence >= 0.75?"}
        T4_VERIFIED["Tier 4: AI Bundle Verified"]
        T4_AMBIG["Ambiguous Bundle / Manual Review"]
        T4_SKIP["Tier 4: Skipped - Pool Too Large"]
    end

    subgraph Tier5["Tier 5: Orphan Categorization"]
        T5_FAILED["Tier 5: No Settlement Expected<br>(Failed Gateway Payments)"]
        T5_UNSETTLED["Tier 5: Unsettled/Pending Receivable<br>(Active Ledger Unmatched)"]
        T5_BANK["Tier 5: Unexplained Bank Credit<br>(Bank Credit Unmatched)"]
    end

    subgraph Storage["Persistent Master Store"]
        PARQUET["data/master_reconciliation_records.parquet"]
        DUCKDB["data/reconciliation.duckdb"]
        TIMING["data/pipeline_timing.json"]
    end

    subgraph Consumers["Downstream Presentation & Q&A"]
        APP["Streamlit App (src/app.py)"]
        FORECASTER["Cash Forecaster (src/forecaster.py)"]
        QA["Settlement Q&A Agent (src/qa_agent.py)"]
    end

    ML --> CAST
    BS --> DEDUP --> CAST
    CAST --> EMPTY_CHK
    EMPTY_CHK -- Yes --> EMPTY_OUT
    EMPTY_CHK -- No --> FILTER_FAILED
    FILTER_FAILED -- Failed rows --> T5_FAILED
    FILTER_FAILED -- Active rows --> Tier1
    CAST --> Tier0
    T0_API --> T0_REGEX
    T0_REGEX -- Match --> T0_PASS --> Tier1
    T0_REGEX -- No match --> T0_FAIL --> Tier2

    Tier1 --> T1_JOIN --> T1_MATH
    T1_MATH -- Yes --> T1_VERIFIED
    T1_MATH -- No --> T1_EXC

    Tier1 -- Unresolved --> Tier2
    Tier2 --> T2_CROSS --> T2_GATES --> T2_GREEDY --> T2_PROP

    T2_PROP --> T3_ACT1
    T3_ACT1 -- Clear (±₹0.50) --> T3_ACT1_OK
    T3_ACT1 -- Fail --> T3_ACT1_FAIL --> T3_ACT2
    Tier2 -- Unresolved --> T3_ACT2

    T3_ACT2 --> T3_VETO
    T3_VETO -- Pass (>= 60) --> T3_ACT2_OK
    T3_VETO -- Fail (< 60) --> T3_TRAP

    T3_ACT2 -- Unresolved Bank/Ledger --> Tier4
    Tier4 --> T4_PRE
    T4_PRE -- Plausible subsets --> T4_LLM --> T4_CONF
    T4_CONF -- Confirmed --> T4_VERIFIED
    T4_CONF -- Low Conf --> T4_AMBIG
    T4_PRE -- Pool > 20 --> T4_SKIP
    T4_PRE -- No subset --> T5_BANK

    Tier4 -- Unresolved Ledger --> T5_UNSETTLED

    T1_VERIFIED & T1_EXC & T3_ACT1_OK & T3_ACT2_OK & T3_TRAP & T4_VERIFIED & T4_AMBIG & T4_SKIP & T5_FAILED & T5_UNSETTLED & T5_BANK --> PARQUET
    PARQUET --> DUCKDB
    PARQUET --> APP
    PARQUET --> FORECASTER
    DUCKDB --> QA
```

---

## 2. Tier-by-Tier Deep Dive

### Tier 0: LLM Narrative Extraction & Regex Gate
- **Purpose**: Extract clean structured tokens (`payment_id`, `entity_name`) from noisy, unstructured bank statement descriptions (e.g. `NEFT-AXIS-pay_4d4654f7ee6b41-CORP`, `UPI/RAZORPAY INDIA/439812/TECHFLOW`).
- **What It Does**:
  - Batches all bank statement descriptions into a single JSON payload.
  - Calls `TIER0_EXTRACTION_MODEL` (`gemini-3.6-flash`) using Google GenAI SDK with structured output enforcement via Pydantic schema `ExtractedBankData`.
  - Applies a strict confidence gate: validates every extracted payment ID against Razorpay's canonical format:
    ```python
    razorpay_id_regex = r"^pay_[a-zA-Z0-9]{14}$"
    ```
  - Contains a transient retry loop (up to 4 attempts with 5-second backoff) to insulate against upstream Gemini 503/429 spikes.
- **What It Deliberately Does NOT Do**:
  - It does **not** make matching decisions.
  - It does **not** trust unverified LLM output: any string failing the regex gate is converted to `None`, forcing the row to fall through to Tier 2 rather than poisoning the deterministic Tier 1 join.
- **Key Thresholds & Constants**:
  - `FAST_LLM_MODEL = TIER0_EXTRACTION_MODEL` (`"gemini-3.6-flash"`)
  - Regex pattern: `^pay_[a-zA-Z0-9]{14}$`

---

### Tier 1: Exact ID Join (Polars)
- **Purpose**: Vectorized deterministic matching on extracted payment IDs against the merchant payment ledger.
- **What It Does**:
  - Performs an inner join between `active_ledger` and `bank_tier0` on `payment_id == extracted_payment_id`.
  - Calculates expected net amount using the official fee settlement formula:
    $$\text{Expected Net} = \text{gross\_amount} - \text{mdr\_fee} - \text{gst\_on\_mdr} - \text{COALESCE}(\text{refund\_amount}, 0.0)$$
  - Calculates absolute amount delta:
    ```python
    amount_delta = (expected_net - net_amount).abs().round(2)
    ```
  - Categorizes records:
    - If `amount_delta < 0.50`: Promoted to `Tier 1: Exact` (Verified, 100% confidence).
    - If `amount_delta >= 0.50`: Flagged as `Tier 1: Exact ID, Amount Mismatch` (Exception, 100% confidence on ID match, but financial discrepancy).
- **What It Deliberately Does NOT Do**:
  - Does **not** allow loose amount tolerance.
  - Does **not** perform fuzzy string comparisons.
- **Key Thresholds & Constants**:
  - Exact match delta tolerance: `< 0.50` (50 paisa)

---

### Tier 2: Fuzzy Entity + Temporal Window (RapidFuzz)
- **Purpose**: Propose candidate matches for transactions where Tier 0 extracted no valid payment ID, but corporate counterparty identity and dates align.
- **What It Does**:
  - Generates a cross-join candidate pool between remaining active ledger rows and unresolved bank records.
  - Computes `identity_score` using RapidFuzz `token_set_ratio`:
    ```python
    fuzz.token_set_ratio(row["customer_name"] or "", row["extracted_entity_name"] or "")
    ```
  - Computes absolute date delta in days:
    $$\text{date\_delta} = |\text{settlement\_date} - \text{payment\_date}|$$
  - Computes candidate pair's fee-adjusted `expected_net` and `amount_delta`.
  - Applies 3 simultaneous gating filters:
    1. `identity_score >= 75.0`
    2. `date_delta <= 3` days
    3. `amount_delta <= 500.0` (loose bound to eliminate cross-invoice collisions)
  - Executes greedy 1-to-1 deduplication: sorts candidates by `amount_delta` ASC, `identity_score` DESC, `date_delta` ASC, and keeps the first unique assignment per settlement and payment ID.
- **What It Deliberately Does NOT Do**:
  - **Tier 2 does not verify.** It only *proposes* candidates. No Tier 2 record is written directly to the final verified ledger; every candidate is forwarded to Tier 3 for mathematical verification.
- **Key Thresholds & Constants**:
  - `TIER2_CANDIDATE_THRESHOLD = 75.0`
  - `TIER2_AMOUNT_DELTA_BOUND = 500.0`
  - Date window: `date_delta <= 3` days

---

### Tier 3: DuckDB Math Engine & Identity Veto
- **Purpose**: High-performance in-process SQL verification executing two independent actions:
  1. **Action 1 (Candidate Verification)**: Confirms Tier 2 proposed candidates against precise ±₹0.50 math tolerance.
  2. **Action 2 (Independent Math Match + Identity Veto)**: Searches unresolved pools for exact mathematical matches to catch corporate aliasing, while actively vetoing accidental coincidences.
- **What It Does**:
  - Action 1 runs in DuckDB:
    ```sql
    SELECT *, ROUND(ABS(expected_net - net_amount), 2) AS final_amount_delta
    FROM tier2_proposed
    ```
    Matches where `final_amount_delta < 0.50` become `Tier 3: Fuzzy+Math Verified`. Failed candidates are **released back** into the pending pool so neither side is stranded.
  - Action 2 performs a full cross-join between active pending ledger rows and pending bank credits, finding pairs where `amount_delta < 0.50`.
  - Every math match is scored with `fuzz.token_set_ratio`:
    - If `identity_score >= 60.0`: Promoted to `Tier 3: Math-Only Verified` (resolves corporate aliasing like parent/subsidiary name discrepancies).
    - If `identity_score < 60.0`: **Vetoed and trapped** as `Exception: Math Match - Identity Mismatch (Suspected Coincidence)`.
- **What It Deliberately Does NOT Do**:
  - Does **not** auto-verify an amount match without checking identity score.
  - Does **not** permanently discard Action 1 candidates if math fails; it releases them for downstream bundling.
- **Key Thresholds & Constants**:
  - `TIER3_MATH_TOLERANCE = 0.50`
  - `TIER3_IDENTITY_VETO_THRESHOLD = 60.0`

---

### Tier 4: Bounded Subset-Sum & Reasoning LLM (Bundle Resolution)
- **Purpose**: Detect and resolve many-to-one batch settlements (where a single bank credit covers multiple invoices).
- **What It Does**:
  - Implements a bounded subset-sum pre-filter:
    - Skips bundle search if candidate pool exceeds `TIER4_MAX_POOL_SIZE = 20` (protects against combinatorial explosion).
    - Checks combinations of size 2 and 3: `combinations(pool, 2)` and `combinations(pool, 3)`.
    - Enforces temporal window: invoice payment dates must fall within `TIER4_DATE_WINDOW_DAYS = 5` of settlement date.
    - Sums candidate invoice `expected_net` values within `TIER4_AMOUNT_TOLERANCE = 0.50`.
  - If one or more candidate subsets clear the arithmetic bound, escalates to `TIER4_REASONING_MODEL` (`gemini-3.6-flash`) with structured schema `BundleResolution`.
  - Verifies bundle only if `top_confidence >= 0.75`.
  - If confidence is below threshold or pool is skipped, labels record as `Tier 4: Skipped - Pool Too Large` or `Ambiguous Bundle - Manual Review Required` (`status: Manual Review`).
- **What It Deliberately Does NOT Do**:
  - Does **not** run unbounded subset-sum across the entire ledger.
  - Does **not** allow the LLM to invent invoice IDs; the LLM ranks only the deterministically pre-computed candidate subsets.
- **Key Thresholds & Constants**:
  - `TIER4_MAX_POOL_SIZE = 20`
  - `TIER4_MAX_SUBSET_SIZE = 3`
  - `TIER4_AMOUNT_TOLERANCE = 0.50`
  - `TIER4_DATE_WINDOW_DAYS = 5`
  - `TIER4_CONFIDENCE_THRESHOLD = 0.75`

---

### Tier 5: Orphan Categorization
- **Purpose**: Exhaustive accounting for every row that remains unresolved after Tiers 1–4.
- **What It Does**:
  - Categorizes all residual items into distinct buckets:
    1. **Failed Payments (`status: No Settlement Expected`)**: Records where `merchant_ledger.csv` has `status == "failed"`. Gated upstream and assigned `confidence: 1.0`.
    2. **Unsettled Receivables (`status: Exception`)**: Active ledger records with no corresponding bank credit (`resolved_at_tier: Tier 5: Unsettled/Pending Receivable`, `confidence: 0.0`).
    3. **Unexplained Bank Credits (`status: Exception`)**: Bank credits with no matching ledger entry (`resolved_at_tier: Tier 5: Unexplained Bank Credit`, `confidence: 0.0`).
- **What It Deliberately Does NOT Do**:
  - Leaves zero unmapped rows. Every record entering the pipeline is accounted for.

---

## 3. The Tier 3 "Precision Trap" Safeguard

In payment reconciliation, coincidental amount collisions are common. For example, two completely unrelated customers might both have transactions yielding an identical net amount of ₹39,821.35. A naïve math-only reconciliation system would blindly match them, corrupting financial reporting.

### Mechanics of the Safeguard
1. Action 2 identifies all ledger-bank pairs where $|\text{expected\_net} - \text{net\_amount}| < 0.50$.
2. It computes the RapidFuzz `token_set_ratio` between the ledger entity name and the bank extracted entity name.
3. If the score falls below `TIER3_IDENTITY_VETO_THRESHOLD` (`60.0`), the match is **vetoed**.
4. Instead of silent verification, the pair is flagged as:
   `resolved_at_tier = "Exception: Math Match - Identity Mismatch (Suspected Coincidence)"`
   `status = "Exception"`
   `confidence = identity_score / 100`

### Real Dataset Example
From the actual synthetic torture-test dataset:
- **Ledger Record**: `pay_5e1b7224aa7e4f`
  - Customer: `"Razorpay Software"`
  - Expected Net: `₹39,821.35`
- **Bank Record**: `IMPS617353423410`
  - Extracted Entity: `"Individual Sneha"`
  - Settled Net: `₹39,821.35`
  - Amount Delta: `₹0.00` (Exact paisa match)
- **Evaluation**:
  - `identity_score = fuzz.token_set_ratio("Razorpay Software", "Individual Sneha") = 0.0`
  - Score `0.0 < 60.0` threshold $\rightarrow$ **Veto triggered**.
  - Match is rejected from verified ledger and isolated in the audit trail for human review.

---

## 4. Failure-Mode & Edge-Case Behavior

The pipeline is hardened against dirty, incomplete, or adversarial data:

### 1. Empty Batch (0 Records)
- **Problem**: When an empty CSV is ingested, Polars cannot infer column datatypes, causing typed expressions like `gross_amount - mdr_fee` to fail with schema mismatch errors.
- **Implementation**:
  ```python
  if len(ledger_df) == 0 or len(bank_df) == 0:
      empty_master = pl.DataFrame(schema=RECONCILIATION_RECORD_SCHEMA)
      empty_master.write_parquet(out_path)
      sys.exit(0)
  ```
- **Result**: Gracefully writes a schema-valid, 0-row Parquet file. Downstream consumers (`app.py`, `qa_agent.py`) load an empty dataset cleanly without unhandled exceptions.

### 2. Duplicate Bank `settlement_utr`
- **Problem**: Duplicate bank statement rows or duplicate webhook events could claim multiple ledger payments, causing double-counted revenue.
- **Implementation**:
  ```python
  if bank_df["settlement_utr"].is_duplicated().any():
      n_before = len(bank_df)
      bank_df = bank_df.unique(subset=["settlement_utr"], keep="first")
      print(f"[Warning] Dropped {n_before - len(bank_df)} duplicate settlement_utr row(s)...")
  ```
- **Result**: Drops duplicate lines upstream, logging an audit warning and preserving single-settlement integrity.

### 3. Null or Blank `customer_name`
- **Problem**: RapidFuzz raises an error or produces invalid output if passed Python `None`.
- **Implementation**:
  ```python
  fuzz.token_set_ratio(
      row["customer_name"] or "",
      row["extracted_entity_name"] or ""
  )
  ```
- **Result**: Coalesces nulls to empty strings. Unextractable narratives safely receive a score of `0.0` and fall through to orphan categories without halting the pipeline.

### 4. LLM Extraction Total Failure / API 503 Outage
- **Problem**: Temporary Gemini rate limits (429) or service outages (503 UNAVAILABLE).
- **Implementation**:
  - 4-attempt retry loop with 5-second backoff in `3_reconciliation_pipeline.py`.
  - If all retries fail, it falls back to a clean degradation:
    ```python
    extracted_data = [{"payment_id": None, "entity_name": None} for _ in narratives]
    ```
- **Result**: Pipeline does not crash. With extracted IDs set to `None`, Tier 1 exact matches skip cleanly, and records fall through to Tier 2 and Tier 3 for mathematical and fuzzy resolution.
