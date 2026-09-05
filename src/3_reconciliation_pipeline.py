import sys
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

import polars as pl
from pathlib import Path
from rapidfuzz import fuzz
import os
import time
from dotenv import load_dotenv
from pydantic import BaseModel
from google import genai
from google.genai import types
from pipeline_timing import Timer
from config import TIER0_EXTRACTION_MODEL

load_dotenv()

# Initialize performance timer
timer = Timer()

# --- CONFIG CONSTANTS (Empirically verified) ---
TIER2_CANDIDATE_THRESHOLD = 75.0
TIER2_AMOUNT_DELTA_BOUND = 500.0   # loose-but-real bound: rules out wrong-invoice/bundle collisions,
                                     # NOT the same as Tier 3's precise ₹0.50 tolerance
TIER3_IDENTITY_VETO_THRESHOLD = 60.0  # Will be used in Day 4

print("Loading data...")
data_dir = Path(__file__).with_name("data")
ledger_df = pl.read_csv(data_dir / "merchant_ledger.csv", try_parse_dates=True)
bank_df = pl.read_csv(data_dir / "bank_settlement.csv", try_parse_dates=True)

# ── Schema enforcement ─────────────────────────────────────────────────────────
# Polars cannot infer column types from an empty CSV (0 data rows), so every
# column comes back as Utf8 and arithmetic like gross_amount - mdr_fee fails.
# Explicit casts are a no-op on a normally-populated file and a type-correction
# on the empty-batch edge case.
ledger_df = ledger_df.with_columns([
    pl.col("gross_amount").cast(pl.Float64),
    pl.col("mdr_fee").cast(pl.Float64),
    pl.col("gst_on_mdr").cast(pl.Float64),
    pl.col("refund_amount").cast(pl.Float64),
    pl.col("payment_date").cast(pl.Date),
])
bank_df = bank_df.with_columns([
    pl.col("net_amount").cast(pl.Float64),
    pl.col("settlement_date").cast(pl.Date),
])

# Deduplicate bank rows on settlement_utr so a data-entry duplicate never
# claims two ledger rows (double-count guard). Keep the first occurrence.
if bank_df["settlement_utr"].is_duplicated().any():
    n_before = len(bank_df)
    bank_df = bank_df.unique(subset=["settlement_utr"], keep="first")
    print(f"[Warning] Dropped {n_before - len(bank_df)} duplicate settlement_utr row(s) from bank data.")

# ── Empty-batch guard ──────────────────────────────────────────────────────────
# If either input is empty there is nothing to reconcile. Rather than crashing
# deep in tier logic (cross-join schema inference, DuckDB registration), we
# write an empty parquet with the correct ReconciliationRecord schema so that
# app.py and qa_agent.py get a valid-but-empty table instead of a missing file.
if len(ledger_df) == 0 or len(bank_df) == 0:
    print("\n[Info] One or both inputs are empty. Nothing to reconcile.")
    print("Match rate: N/A (0 matchable records)")
    empty_master = pl.DataFrame(
        schema={
            "record_id": pl.Utf8,
            "record_type": pl.Utf8,
            "status": pl.Utf8,
            "resolved_at_tier": pl.Utf8,
            "resolution_method": pl.Utf8,
            "confidence": pl.Float64,
            "customer_name": pl.Utf8,
            "ledger_payment_id": pl.Utf8,
            "bank_settlement_utr": pl.Utf8,
            "gross_amount": pl.Float64,
            "net_amount": pl.Float64,
            "amount_delta": pl.Float64,
            "payment_date": pl.Utf8,
            "settlement_date": pl.Utf8,
            "notes": pl.Utf8,
        }
    )
    out_path = Path("data/master_reconciliation_records.parquet")
    out_path.parent.mkdir(exist_ok=True)
    empty_master.write_parquet(out_path)
    print(f"Wrote empty master record to {out_path} (schema-valid, 0 rows).")
    sys.exit(0)

# Helper to calculate expected net (used for delta checking)
def calculate_expected_net(df: pl.DataFrame) -> pl.Expr:
    return (
        pl.col("gross_amount") -
        pl.col("mdr_fee") -
        pl.col("gst_on_mdr") -
        pl.col("refund_amount").fill_null(0.0)  # 0.0 not 0 — avoids Int64/Float64 schema
                                                 # mismatch on empty-batch DataFrames
    )

# ==========================================
# PRE-PROCESSING: Filter Failed Payments
# ==========================================
failed_payments = ledger_df.filter(pl.col("status") == "failed")
active_ledger = ledger_df.filter(pl.col("status") != "failed")

print(f"\nTotal Active Ledger Rows: {len(active_ledger)}")
print(f"Filtered Failed Rows: {len(failed_payments)} (Routed to Tier 5)")

# ==========================================
# TIER 0: REAL LLM PRE-PROCESSING (Batched API Call)
# ==========================================
with timer.track("Tier 0: LLM Extraction"):
    import json
    from pydantic import BaseModel

    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    genai_client = genai.Client(api_key=GEMINI_API_KEY)
    FAST_LLM_MODEL = TIER0_EXTRACTION_MODEL

    # 1. Define the Pydantic schema for a single extracted record
    class ExtractedBankData(BaseModel):
        payment_id: str | None
        entity_name: str | None

    print("\n--- TIER 0: LLM Extraction (Batched) ---")

    narratives = bank_df["transaction_description"].to_list()
    narratives_json = json.dumps(narratives, indent=2)

    prompt = f"""Analyze the following list of bank settlement narratives. 
For each narrative, extract the Razorpay Payment ID (usually starts with 'pay_') and the Counterparty Entity Name.
If a field is not present in a narrative, use null for that field.

Return a JSON array of objects with the keys "payment_id" and "entity_name", matching the order of the input list.

Input Narratives:
{narratives_json}
"""

    try:
        response = None
        for attempt in range(4):
            try:
                response = genai_client.models.generate_content(
                    model=FAST_LLM_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        # Pass the schema as a list of the Pydantic model
                        response_schema=list[ExtractedBankData],
                    ),
                )
                break
            except Exception as e:
                if ("503" in str(e) or "429" in str(e) or "UNAVAILABLE" in str(e) or "RESOURCE_EXHAUSTED" in str(e)) and attempt < 3:
                    print(f"  [Notice] Gemini API transient error ({type(e).__name__}), retrying in 5s (attempt {attempt+2}/4)...")
                    time.sleep(5)
                    continue
                raise
        
        extracted_list = json.loads(response.text)
        
        if len(extracted_list) != len(narratives):
            print(f"  [Warning] LLM returned {len(extracted_list)} rows, but expected {len(narratives)}. Padding with nulls.")
            while len(extracted_list) < len(narratives):
                extracted_list.append({"payment_id": None, "entity_name": None})

        extracted_data = [
            {
                "payment_id": d.get("payment_id"),
                "entity_name": d.get("entity_name")
            }
            for d in extracted_list
        ]
        print(f"  Successfully extracted data for {len(extracted_data)} rows in a single API call.")

    except Exception as e:
        print(f"  [Error] Batch LLM extraction failed: {e}")
        extracted_data = [{"payment_id": None, "entity_name": None} for _ in narratives]

    # Add extracted data back to the bank DataFrame
    bank_tier0 = bank_df.with_columns([
        pl.Series("extracted_payment_id", [d["payment_id"] for d in extracted_data], dtype=pl.Utf8),
        pl.Series("extracted_entity_name", [d["entity_name"] for d in extracted_data], dtype=pl.Utf8)
    ])

    # THE CONFIDENCE GATE: Validate LLM output with Razorpay Regex
    razorpay_id_regex = r"^pay_[a-zA-Z0-9]{14}$"

    bank_tier0 = bank_tier0.with_columns(
        pl.when(pl.col("extracted_payment_id").str.contains(razorpay_id_regex, literal=False))
          .then(pl.col("extracted_payment_id"))
          .otherwise(None)
          .alias("extracted_payment_id")
    )

    valid_extractions = len(bank_tier0.filter(pl.col("extracted_payment_id").is_not_null()))
    print(f"LLM extracted valid IDs for {valid_extractions} rows (failed IDs dropped by regex gate).")

# ==========================================
# TIER 1: Exact Match
# ==========================================
with timer.track("Tier 1: Exact Match"):
    print("\n--- TIER 1: Exact Match ---")

    tier1_matches = active_ledger.join(
        bank_tier0.filter(pl.col("extracted_payment_id").is_not_null()),
        left_on="payment_id",
        right_on="extracted_payment_id",
        how="inner",
        suffix="_bank"
    )

    tier1_matches = tier1_matches.with_columns(
        calculate_expected_net(tier1_matches).alias("expected_net")
    ).with_columns(
        (pl.col("expected_net") - pl.col("net_amount")).abs().round(2).alias("amount_delta")
    )

    tier1_verified = tier1_matches.filter(pl.col("amount_delta") < 0.50)
    tier1_exceptions = tier1_matches.filter(pl.col("amount_delta") >= 0.50)

    matched_ledger_ids = set(tier1_matches["payment_id"].to_list())
    matched_bank_utrs = set(tier1_matches["settlement_utr"].to_list())

    pending_ledger = active_ledger.filter(~pl.col("payment_id").is_in(list(matched_ledger_ids)))
    pending_bank = bank_tier0.filter(~pl.col("settlement_utr").is_in(list(matched_bank_utrs)))

    print(f"Tier 1 Verified: {len(tier1_verified)}")
    print(f"Tier 1 Amount Mismatch Exceptions: {len(tier1_exceptions)}")
    print(f"Remaining Pending Ledger Rows: {len(pending_ledger)}")
    print(f"Remaining Pending Bank Rows: {len(pending_bank)}")

# ==========================================
# TIER 2: Fuzzy Entity + Temporal Match (with REAL fee-adjusted amount check)
# ==========================================
with timer.track("Tier 2: Fuzzy Match"):
    print("\n--- TIER 2: Fuzzy + Date + Fee-Adjusted Amount Match ---")

    cross_joined = pending_ledger.join(pending_bank, how="cross", suffix="_bank")

    tier2_candidates = cross_joined.with_columns([
        pl.struct(["customer_name", "extracted_entity_name"])
          .map_elements(
              lambda row: fuzz.token_set_ratio(
                  row["customer_name"] or "",        # None-safe: coalesce to empty string
                  row["extracted_entity_name"] or "" # so fuzz never receives a None argument
              ),
              return_dtype=pl.Float64
          )
          .alias("identity_score"),
        (pl.col("settlement_date") - pl.col("payment_date")).dt.total_days().abs().alias("date_delta")
    ])

    # CRITICAL FIX: compute this candidate PAIR's true fee-adjusted expected_net
    # (gross/mdr/gst/refund all come from the ledger side of the cross join),
    # then require the bank's net_amount to be within a loose-but-real tolerance
    # of THAT SPECIFIC ledger row's expected value — not just "less than gross".
    # This is what correctly rejects a many-to-one bundle collision: a bundle's
    # net_amount will be close to gross for SOME same-customer row by coincidence,
    # but will NOT be close to that row's fee-adjusted expected_net.
    tier2_candidates = tier2_candidates.with_columns(
        calculate_expected_net(tier2_candidates).alias("expected_net")
    ).with_columns(
        (pl.col("expected_net") - pl.col("net_amount")).abs().round(2).alias("amount_delta")
    )

    # Filter by identity threshold, date window, AND real fee-adjusted amount bound
    tier2_filtered = tier2_candidates.filter(
        (pl.col("identity_score") >= TIER2_CANDIDATE_THRESHOLD) &
        (pl.col("date_delta") <= 3) &
        (pl.col("amount_delta") <= TIER2_AMOUNT_DELTA_BOUND)
    )

    # GREEDY 1-TO-1 RESOLUTION: prefer the tightest amount match first, then best identity score, then lowest date delta
    tier2_proposed = tier2_filtered.sort(
        ["amount_delta", "identity_score", "date_delta"],
        descending=[False, True, False]
    )
    tier2_proposed = tier2_proposed.unique(subset=["settlement_utr"], keep="first")
    tier2_proposed = tier2_proposed.unique(subset=["payment_id"], keep="first")

    # Remove proposed candidates from the pending pool
    matched_ledger_ids_t2 = set(tier2_proposed["payment_id"].to_list())
    matched_bank_utrs_t2 = set(tier2_proposed["settlement_utr"].to_list())

    pending_ledger_t3 = pending_ledger.filter(~pl.col("payment_id").is_in(list(matched_ledger_ids_t2)))
    pending_bank_t3 = pending_bank.filter(~pl.col("settlement_utr").is_in(list(matched_bank_utrs_t2)))

    print(f"Tier 2 Proposed Candidates (1-to-1 Resolved): {len(tier2_proposed)}")
    if len(tier2_proposed) > 0:
        print("\nSample Tier 2 Proposed Matches:")
        print(tier2_proposed.select([
            "payment_id", "settlement_utr", "customer_name", "extracted_entity_name",
            "identity_score", "date_delta", "expected_net", "net_amount", "amount_delta"
        ]))

    print(f"\nRemaining Pending Ledger Rows for Tier 3: {len(pending_ledger_t3)}")
    print(f"Remaining Pending Bank Rows for Tier 3: {len(pending_bank_t3)}")
    print("(Note: BULK settlements will organically land here, to be solved by Tier 4 tomorrow)")

# ==========================================
# SANITY CHECK: confirm the known bundle row did NOT get consumed by Tier 2
# ==========================================
print("\n--- Sanity check: known BULK settlement UTRs ---")
bulk_utrs = bank_df.filter(pl.col("transaction_description").str.contains("BULK")).select("settlement_utr").to_series().to_list()
for u in bulk_utrs:
    status = "CONSUMED by T1" if u in matched_bank_utrs else ("CONSUMED by T2" if u in matched_bank_utrs_t2 else "correctly pending -> T3/T4")
    print(f"  {u}: {status}")

import duckdb

# --- CONFIG (carried over from Tier 1/2) ---
TIER3_MATH_TOLERANCE = 0.50            # precise tolerance — this IS the real check, unlike Tier 2's loose ₹500 bound
TIER3_IDENTITY_VETO_THRESHOLD = 60.0   # minimum fuzzy score required to accept a math-only match;
                                         # calibrated earlier in this project: genuine trap-case pairs
                                         # (e.g. "Umbrella Inc" vs "Individual Sneha") scored 16-37.5,
                                         # genuine fuzzy-name pairs scored 78-88 — 60 sits safely in the gap

# ==========================================
# TIER 3: Math & Identity-Veto Engine (DuckDB)
# ==========================================
with timer.track("Tier 3: Math-Only & Identity Veto"):
    print("\n--- TIER 3: Math & Identity-Veto Engine (DuckDB) ---")

    # ACTION 1: Re-verify Tier 2's proposed candidates precisely
    con = duckdb.connect()
    con.register("tier2_proposed", tier2_proposed)

    tier3_action1_result = con.execute("""
        SELECT *,
            ROUND(ABS(expected_net - net_amount), 2) AS final_amount_delta
        FROM tier2_proposed
    """).pl()

    tier3_action1_verified = tier3_action1_result.filter(
        pl.col("final_amount_delta") < TIER3_MATH_TOLERANCE
    ).with_columns([
        pl.lit("Tier 3: Fuzzy+Math Verified").alias("resolved_at_tier"),
        pl.lit("tier2_candidate_verified").alias("resolution_method")
    ])

    tier3_action1_released = tier3_action1_result.filter(
        pl.col("final_amount_delta") >= TIER3_MATH_TOLERANCE
    )

    print(f"Action 1 — Tier 2 candidates re-verified: {len(tier3_action1_verified)}")
    print(f"Action 1 — Tier 2 candidates REJECTED and released back to pool: {len(tier3_action1_released)}")

    # Release rejected candidates back into pending pools (both sides get another chance)
    released_ledger_ids = set(tier3_action1_released["payment_id"].to_list())
    released_bank_utrs = set(tier3_action1_released["settlement_utr"].to_list())

    pending_ledger_t3_active = pl.concat([
        pending_ledger_t3,
        active_ledger.filter(pl.col("payment_id").is_in(list(released_ledger_ids)))
    ])
    pending_bank_t3_active = pl.concat([
        pending_bank_t3,
        bank_tier0.filter(pl.col("settlement_utr").is_in(list(released_bank_utrs)))
    ])

    # ACTION 2: Independent math-only matching on whatever's still unresolved
    con.register("pending_ledger_t3", pending_ledger_t3_active)
    con.register("pending_bank_t3", pending_bank_t3_active)

    # Cross join remaining pools and compute both signals: math delta AND identity score
    action2_candidates = con.execute("""
        SELECT
            l.payment_id, l.customer_name, l.gross_amount, l.mdr_fee, l.gst_on_mdr, l.refund_amount,
            l.payment_date,
            b.settlement_utr, b.transaction_description, b.extracted_entity_name, b.net_amount, b.settlement_date,
            ROUND((l.gross_amount - l.mdr_fee - l.gst_on_mdr - COALESCE(l.refund_amount, 0)), 2) AS expected_net,
            ROUND(ABS((l.gross_amount - l.mdr_fee - l.gst_on_mdr - COALESCE(l.refund_amount, 0)) - b.net_amount), 2) AS amount_delta
        FROM pending_ledger_t3 l
        CROSS JOIN pending_bank_t3 b
    """).pl()

    # Keep only pairs where the math genuinely matches within tight tolerance
    action2_math_matches = action2_candidates.filter(
        pl.col("amount_delta") < TIER3_MATH_TOLERANCE
    )

    # Compute identity score for every math-matching pair — this is the veto check
    action2_scored = action2_math_matches.with_columns(
        pl.struct(["customer_name", "extracted_entity_name"])
          .map_elements(
              lambda row: fuzz.token_set_ratio(
                  row["customer_name"] or "",        # None-safe: coalesce to empty string
                  row["extracted_entity_name"] or "" # garbage narratives produce None here
              ),
              return_dtype=pl.Float64
          )
          .alias("identity_score")
    )

    # THE PRECISION TRAP SAFEGUARD:
    action2_verified_raw = action2_scored.filter(pl.col("identity_score") >= TIER3_IDENTITY_VETO_THRESHOLD)
    action2_trapped = action2_scored.filter(pl.col("identity_score") < TIER3_IDENTITY_VETO_THRESHOLD)

    # Greedy 1-to-1 resolution on the genuinely verified set (same pattern as Tier 2)
    action2_verified = action2_verified_raw.sort(
        ["amount_delta", "identity_score"], descending=[False, True]
    )
    action2_verified = action2_verified.unique(subset=["settlement_utr"], keep="first")
    action2_verified = action2_verified.unique(subset=["payment_id"], keep="first")
    action2_verified = action2_verified.with_columns([
        pl.lit("Tier 3: Math-Only Verified").alias("resolved_at_tier"),
        pl.lit("expected_net_match").alias("resolution_method")
    ])

    # Also resolve trap rows 1-to-1 so we report a clean, non-duplicated exception list
    action2_trapped_resolved = action2_trapped.sort(
        ["amount_delta"], descending=[False]
    )
    action2_trapped_resolved = action2_trapped_resolved.unique(subset=["settlement_utr"], keep="first")
    action2_trapped_resolved = action2_trapped_resolved.with_columns([
        pl.lit("Exception: Math Match - Identity Mismatch (Suspected Coincidence)").alias("resolved_at_tier"),
        pl.lit("math_matched_identity_vetoed").alias("resolution_method")
    ])

    print(f"\nAction 2 — Math-only verified (corporate aliasing caught): {len(action2_verified)}")
    print(f"Action 2 — Math matched but IDENTITY VETOED (precision traps caught): {len(action2_trapped_resolved)}")

    if len(action2_verified) > 0:
        print("\nSample Action 2 Verified Matches:")
        print(action2_verified.select([
            "payment_id", "settlement_utr", "customer_name", "extracted_entity_name",
            "identity_score", "expected_net", "net_amount", "amount_delta"
        ]))

    if len(action2_trapped_resolved) > 0:
        print("\nSample Action 2 REJECTED (Precision Trap) Matches:")
        print(action2_trapped_resolved.select([
            "payment_id", "settlement_utr", "customer_name", "extracted_entity_name",
            "identity_score", "expected_net", "net_amount", "amount_delta"
        ]))

    # Update pending pools for Tier 4
    matched_ledger_ids_t3 = set(tier3_action1_verified["payment_id"].to_list()) | set(action2_verified["payment_id"].to_list())
    matched_bank_utrs_t3 = set(tier3_action1_verified["settlement_utr"].to_list()) | set(action2_verified["settlement_utr"].to_list())
    trapped_bank_utrs = set(action2_trapped_resolved["settlement_utr"].to_list())

    pending_ledger_t4 = pending_ledger_t3_active.filter(
        ~pl.col("payment_id").is_in(list(matched_ledger_ids_t3))
    )
    pending_bank_t4 = pending_bank_t3_active.filter(
        ~pl.col("settlement_utr").is_in(list(matched_bank_utrs_t3) + list(trapped_bank_utrs))
    )

    print(f"\nRemaining Pending Ledger Rows for Tier 4: {len(pending_ledger_t4)}")
    print(f"Remaining Pending Bank Rows for Tier 4: {len(pending_bank_t4)}")

# ==========================================
# SANITY CHECK: dynamically discover precision-trap, aliasing, and BULK UTRs
# ==========================================
trap_utrs_known = (
    bank_df.filter(pl.col("transaction_description").str.contains("TIER3-IMPS"))
    .select("settlement_utr")
    .to_series()
    .to_list()
)
print(f"\n--- Sanity check: precision-trap UTRs discovered this run: {trap_utrs_known} ---")
for u in trap_utrs_known:
    status = "correctly TRAPPED (exception)" if u in trapped_bank_utrs else (
        "WRONGLY VERIFIED as a real match!" if u in matched_bank_utrs_t3 else "still pending -> check Tier 4/5"
    )
    print(f"  {u}: {status}")

aliasing_utrs_known = (
    bank_df.filter(pl.col("transaction_description").str.contains("TIER3B"))
    .select("settlement_utr")
    .to_series()
    .to_list()
)
print(f"\n--- Sanity check: corporate-aliasing UTRs discovered this run: {aliasing_utrs_known} ---")
for u in aliasing_utrs_known:
    status = (
        "correctly VERIFIED (Action 2 positive path working)" if u in matched_bank_utrs_t3 else
        "MISSING — check TIER3_IDENTITY_VETO_THRESHOLD isn't rejecting it, or slice overlap in generator"
    )
    print(f"  {u}: {status}")

bulk_utrs_known = (
    bank_df.filter(pl.col("transaction_description").str.contains("BULK"))
    .select("settlement_utr")
    .to_series()
    .to_list()
)
print(f"\n--- Sanity check: BULK UTRs discovered this run: {bulk_utrs_known} ---")
pending_bank_t4_utrs = pending_bank_t4["settlement_utr"].to_list()
for u in bulk_utrs_known:
    status = "correctly pending -> Tier 4" if u in pending_bank_t4_utrs else "MISSING — check where it went"
    print(f"  {u}: {status}")

# ==========================================
# TIER 4: Reasoning-based bundle resolution
# ==========================================
import runpy

with timer.track("Tier 4: AI Bundle Resolution"):
    runpy.run_path(
        Path(__file__).with_name("reasoning.py"),
        init_globals={
            "failed_payments": failed_payments,
            "pending_ledger_t4": pending_ledger_t4,
            "pending_bank_t4": pending_bank_t4,
            "tier1_verified": tier1_verified,
            "tier1_exceptions": tier1_exceptions,
            "tier2_proposed": tier2_proposed,
            "tier3_action1_verified": tier3_action1_verified,
            "action2_verified": action2_verified,
            "action2_trapped_resolved": action2_trapped_resolved,
            "timer": timer,
        },
    )

# Persist timing metrics once all stages including Tier 4 are fully recorded
master_df_path = Path("data/master_reconciliation_records.parquet")
if master_df_path.exists():
    master_records_count = len(pl.read_parquet(master_df_path))
else:
    master_records_count = len(ledger_df) + len(bank_df)

timer.save("data/pipeline_timing.json", total_records=master_records_count)
if os.path.exists("src/data"):
    timer.save("src/data/pipeline_timing.json", total_records=master_records_count)

