import sys
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

import os
import polars as pl
import duckdb
import json
import time
from itertools import combinations
from datetime import date
from dotenv import load_dotenv
try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None
from pydantic import BaseModel, Field
from typing import List, Optional, Literal

# --- CONFIG ---
load_dotenv()
from config import TIER4_REASONING_MODEL
GEMINI_MODEL = TIER4_REASONING_MODEL
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

TIER4_MAX_POOL_SIZE = 20     # skip bundle search if candidate pool exceeds this
TIER4_MAX_SUBSET_SIZE = 3    # only check 2-invoice and 3-invoice combinations
TIER4_AMOUNT_TOLERANCE = 0.50
TIER4_DATE_WINDOW_DAYS = 5   # slightly wider than Tier 2's 3d, since bundled settlements
                              # may span a few extra days across multiple invoice dates
TIER4_CONFIDENCE_THRESHOLD = 0.75

client = genai.Client(api_key=GEMINI_API_KEY) if genai and GEMINI_API_KEY else None
print(f"Using model: {GEMINI_MODEL}")

# ==========================================
# PYDANTIC SCHEMAS (as designed earlier in this project)
# ==========================================
class RankedSubset(BaseModel):
    invoice_ids: List[str]
    calculated_sum: float
    rank: int = Field(..., description="1 for best, 2 for second best, etc.")
    explanation: str

class BundleResolution(BaseModel):
    top_confidence: float = Field(..., description="Confidence score between 0.0 and 1.0 for the top ranked subset")
    ranked_subsets: List[RankedSubset]
    low_confidence_reason: Optional[str] = None

def call_gemini_with_retry(prompt, max_retries=3, base_delay=15):
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=BundleResolution,
                ),
            )
            return BundleResolution.model_validate_json(response.text)
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                if attempt < max_retries - 1:
                    print(f"    Rate limited, waiting {base_delay}s before retry {attempt+2}/{max_retries}...")
                    time.sleep(base_delay)
                    continue
            raise

print("\n--- TIER 4: Bounded Subset-Sum Pre-Filter + Reasoning LLM Bundle Resolution ---")

# ==========================================
# STEP 1: Deterministic subset-sum pre-filter (no LLM yet)
# ==========================================
# pending_ledger_t4 / pending_bank_t4 carried over from Tier 3.
# active_ledger is used to compute expected_net for candidate pool members.

def calculate_expected_net_row(row: dict) -> float:
    return round(
        row["gross_amount"] - row["mdr_fee"] - row["gst_on_mdr"] - (row.get("refund_amount") or 0),
        2
    )

pending_bank_t4: pl.DataFrame | None = globals().get("pending_bank_t4")
pending_ledger_t4: pl.DataFrame | None = globals().get("pending_ledger_t4")
failed_payments: pl.DataFrame | None = globals().get("failed_payments")
if pending_bank_t4 is None or pending_ledger_t4 is None or failed_payments is None:
    raise RuntimeError("reasoning.py must be run by the reconciliation pipeline")

bank_pending_dicts = pending_bank_t4.to_dicts()
ledger_pending_dicts = pending_ledger_t4.to_dicts()

for r in ledger_pending_dicts:
    r["expected_net"] = calculate_expected_net_row(r)

available_ledger_dicts = ledger_pending_dicts.copy()

bundle_results = []      # rows resolved by Tier 4 (verified or ambiguous-manual-review)
still_unresolved_bank = []  # bank rows with no subset found at all -> Tier 5

for bank_row in bank_pending_dicts:
    target = round(bank_row["net_amount"], 2)
    bank_date = bank_row["settlement_date"]

    # Narrow candidate pool: same customer name is NOT assumed here (real bundles
    # may reference a company name in the narrative, but we don't fully trust that
    # yet since Tier 0's extraction is a placeholder) — instead narrow by date window,
    # which is a safer, more general signal that doesn't depend on name-matching quality.
    candidate_pool = [
        r for r in available_ledger_dicts
        if abs((bank_date - r["payment_date"]).days) <= TIER4_DATE_WINDOW_DAYS
    ]

    if len(candidate_pool) > TIER4_MAX_POOL_SIZE:
        print(f"  {bank_row['settlement_utr']}: pool too large ({len(candidate_pool)} > {TIER4_MAX_POOL_SIZE}), skipping bundle search -> Manual Review")
        bundle_results.append({
            **bank_row,
            "resolved_at_tier": "Tier 4: Skipped - Pool Too Large",
            "resolution_method": "pool_bound_exceeded",
            "confidence": 0.0,
        })
        continue

    # Deterministic subset-sum search, bounded to size 2 and 3
    valid_subsets = []
    for size in range(2, TIER4_MAX_SUBSET_SIZE + 1):
        for combo in combinations(candidate_pool, size):
            s = round(sum(c["expected_net"] for c in combo), 2)
            if abs(s - target) < TIER4_AMOUNT_TOLERANCE:
                valid_subsets.append(combo)

    if len(valid_subsets) == 0:
        # No plausible bundle found at all -> this bank row is a genuine orphan
        still_unresolved_bank.append(bank_row)
        continue

    print(f"  {bank_row['settlement_utr']}: found {len(valid_subsets)} plausible subset(s), escalating to reasoning LLM")

    # ==========================================
    # STEP 2: Reasoning LLM call — ONLY for rows with at least one plausible subset
    # ==========================================
    subset_descriptions = []
    for i, combo in enumerate(valid_subsets):
        subset_descriptions.append({
            "option_id": i,
            "invoice_ids": [c["payment_id"] for c in combo],
            "customer_names": [c["customer_name"] for c in combo],
            "individual_amounts": [c["expected_net"] for c in combo],
            "calculated_sum": round(sum(c["expected_net"] for c in combo), 2),
            "payment_dates": [str(c["payment_date"]) for c in combo],
        })

    prompt = f"""You are a financial reconciliation assistant. A bank settlement of amount
{target} (settled on {bank_date}, narrative: "{bank_row['transaction_description']}") does not
match any single invoice, but the following combinations of ledger invoices sum to within
tolerance of this settlement amount:

{json.dumps(subset_descriptions, indent=2)}

Evaluate which combination (if any) most plausibly represents the true set of invoices bundled
into this single settlement. Prefer combinations where all invoices belong to the SAME customer
and have CLOSE payment dates, since real bundled settlements group one customer's invoices from
a similar period. If multiple options are similarly plausible, or none looks clearly correct,
reflect that with a lower confidence score.

Respond with a ranking of the given options (referencing them by option_id in your invoice_ids
field — actually use the real invoice_ids listed for that option), a top_confidence score, and
a brief explanation for the top choice. If you are not confident in any option, set
low_confidence_reason explaining why."""

    try:
        if client is None:
            raise RuntimeError("GEMINI_API_KEY is not set")
        resolution = call_gemini_with_retry(prompt)
    except Exception as e:
        print(f"    LLM call failed for {bank_row['settlement_utr']}: {e}")
        bundle_results.append({
            **bank_row,
            "resolved_at_tier": "Tier 4: LLM Call Failed",
            "resolution_method": "llm_error",
            "confidence": 0.0,
            "low_confidence_reason": str(e),
        })
        continue

    if resolution.top_confidence >= TIER4_CONFIDENCE_THRESHOLD and resolution.ranked_subsets:
        top = resolution.ranked_subsets[0]
        bundle_results.append({
            **bank_row,
            "resolved_at_tier": "Tier 4: AI Bundle Verified",
            "resolution_method": "subset_sum_llm",
            "confidence": resolution.top_confidence,
            "matched_invoice_ids": top.invoice_ids,
            "llm_explanation": top.explanation,
        })
        matched_ids = set(top.invoice_ids)
        available_ledger_dicts = [
            r for r in available_ledger_dicts
            if r["payment_id"] not in matched_ids
        ]
        print(f"    VERIFIED: {top.invoice_ids} (confidence={resolution.top_confidence:.2f})")
    else:
        bundle_results.append({
            **bank_row,
            "resolved_at_tier": "Ambiguous Bundle - Manual Review Required",
            "resolution_method": "subset_sum_llm_low_confidence",
            "confidence": resolution.top_confidence,
            "low_confidence_reason": resolution.low_confidence_reason or "LLM confidence below threshold",
        })
        print(f"    AMBIGUOUS (confidence={resolution.top_confidence:.2f}) -> Manual Review")

    time.sleep(4)  # crude free-tier RPM guard; adjust based on your model's actual RPM limit

# ==========================================
# SUMMARY
# ==========================================
print(f"\nTier 4 Verified Bundles: {sum(1 for r in bundle_results if r['resolved_at_tier']=='Tier 4: AI Bundle Verified')}")
print(f"Tier 4 Ambiguous (Manual Review): {sum(1 for r in bundle_results if 'Manual Review' in r['resolved_at_tier'] and r['resolution_method']!='pool_bound_exceeded')}")
print(f"Tier 4 Skipped (Pool Too Large): {sum(1 for r in bundle_results if r['resolution_method']=='pool_bound_exceeded')}")
print(f"Genuine Bank Orphans (no subset found at all) -> Tier 5: {len(still_unresolved_bank)}")

# ==========================================
# SANITY CHECK: dynamically discovered BULK UTRs should verify successfully
# ==========================================
print("\n--- Sanity check: known BULK UTRs ---")
bulk_utrs_known = (
    pending_bank_t4.filter(pl.col("transaction_description").str.contains("BULK"))
    .select("settlement_utr")
    .to_series()
    .to_list()
)
print(f"BULK UTRs discovered this run: {bulk_utrs_known}")
for u in bulk_utrs_known:
    match = next((r for r in bundle_results if r["settlement_utr"] == u), None)
    if match:
        print(f"  {u}: {match['resolved_at_tier']} (confidence={match.get('confidence')})")
    else:
        print(f"  {u}: NOT FOUND in bundle_results — check still_unresolved_bank")
        still_check = next((r for r in still_unresolved_bank if r["settlement_utr"] == u), None)
        print(f"    In unresolved orphans instead: {still_check is not None}")

import polars as pl
from datetime import datetime

# ==========================================
# TIER 5: Orphan Categorization ("The Honest Exception List")
# ==========================================
# By this point, every ledger row and every bank row has either been consumed by
# Tiers 1-4 (added to a matched/verified/exception bucket already) or is sitting
# in the final pending pools. Tier 5's only job is to take what's LEFT and label
# it clearly and specifically — never a bare "unresolved".

print("\n--- TIER 5: Orphan Categorization ---")

# ------------------------------------------
# LEDGER-SIDE: what's left in pending_ledger_t4 after Tier 4 ran
# (pending_ledger_t4 was NOT touched by Tier 4 — bundles only consume bank rows'
# matched invoice IDs, which should already be reflected if you removed them;
# see note below if you haven't wired that back yet)
# ------------------------------------------

# Bundle-consumed ledger IDs need to be removed here if Tier 4's code didn't already
# do this — collect them from bundle_results' matched_invoice_ids field.
bundle_consumed_ledger_ids = set()
for r in bundle_results:
    if r.get("resolved_at_tier") == "Tier 4: AI Bundle Verified":
        bundle_consumed_ledger_ids.update(r.get("matched_invoice_ids", []))

pending_ledger_final = pending_ledger_t4.filter(
    ~pl.col("payment_id").is_in(list(bundle_consumed_ledger_ids))
)

# failed_payments was set aside right at the start of the pipeline (Tier 0 pre-processing)
tier5_failed_no_settlement = failed_payments.with_columns([
    pl.lit("Tier 5: No Settlement Expected (Payment Failed)").alias("resolved_at_tier"),
    pl.lit("failed_status_excluded").alias("resolution_method"),
    pl.lit(1.0).alias("confidence"),  # 100% confident this correctly has no settlement
])

tier5_unsettled_receivable = pending_ledger_final.with_columns([
    pl.lit("Tier 5: Unsettled/Pending Receivable").alias("resolved_at_tier"),
    pl.lit("no_bank_match_found").alias("resolution_method"),
    pl.lit(0.0).alias("confidence"),  # 0.0 = no match confidence, this is an honest gap
])

print(f"Ledger orphans — Failed (no settlement expected): {len(tier5_failed_no_settlement)}")
print(f"Ledger orphans — Unsettled/Pending Receivable (should have settled, didn't): {len(tier5_unsettled_receivable)}")

# ------------------------------------------
# BANK-SIDE: what's left after Tier 4's subset-sum search found nothing
# (still_unresolved_bank, from the Tier 4 script)
# ------------------------------------------

tier5_bank_orphans = pl.DataFrame(still_unresolved_bank).with_columns([
    pl.lit("Tier 5: Unexplained Bank Credit").alias("resolved_at_tier"),
    pl.lit("no_ledger_match_found").alias("resolution_method"),
    pl.lit(0.0).alias("confidence"),
]) if len(still_unresolved_bank) > 0 else pl.DataFrame()

# Also fold in any Tier 4 rows that were explicitly ambiguous/skipped — these are
# NOT the same as "no subset found at all"; they found *something* plausible but
# couldn't confirm it. Keep them as their own category, don't merge into orphans.
tier5_ambiguous_bundles = pl.DataFrame([
    r for r in bundle_results
    if r.get("resolved_at_tier") in (
        "Ambiguous Bundle - Manual Review Required",
        "Tier 4: Skipped - Pool Too Large",
        "Tier 4: LLM Call Failed",
    )
]) if any(
    r.get("resolved_at_tier") in (
        "Ambiguous Bundle - Manual Review Required",
        "Tier 4: Skipped - Pool Too Large",
        "Tier 4: LLM Call Failed",
    ) for r in bundle_results
) else pl.DataFrame()

print(f"Bank orphans — Unexplained Bank Credit (no subset found at all): {len(tier5_bank_orphans)}")
print(f"Bank rows — Ambiguous/Skipped (found something, couldn't confirm): {len(tier5_ambiguous_bundles)}")

# ------------------------------------------
# SUMMARY: the full honest exception list, by category
# ------------------------------------------
print("\n" + "="*60)
print("FINAL EXCEPTION LIST — every unresolved row, categorized")
print("="*60)

total_unresolved = (
    len(tier5_failed_no_settlement)
    + len(tier5_unsettled_receivable)
    + len(tier5_bank_orphans)
    + len(tier5_ambiguous_bundles)
)
print(f"\nTotal unresolved rows across ledger + bank: {total_unresolved}")
print(f"  - No settlement expected (failed payment):  {len(tier5_failed_no_settlement)}")
print(f"  - Unsettled/pending receivable (owed money): {len(tier5_unsettled_receivable)}")
print(f"  - Unexplained bank credit (needs investigation): {len(tier5_bank_orphans)}")
print(f"  - Ambiguous/skipped bundles (needs manual review): {len(tier5_ambiguous_bundles)}")

if len(tier5_unsettled_receivable) > 0:
    print("\nUnsettled Receivables (chase these — ops team follow-up):")
    print(tier5_unsettled_receivable.select(["payment_id", "customer_name", "gross_amount", "payment_date"]))

if len(tier5_bank_orphans) > 0:
    print("\nUnexplained Bank Credits (investigate — could be bank error, unrecorded manual entry):")
    print(tier5_bank_orphans.select(["settlement_utr", "transaction_description", "net_amount", "settlement_date"]))

# ==========================================
# MASTER SCHEMA: ReconciliationRecord
# ==========================================
class ReconciliationRecord(BaseModel):
    record_id: str                  # payment_id (ledger-anchored) or settlement_utr (bank-anchored orphan)
    record_type: Literal["ledger", "bank"]  # which side this record's identity is anchored to
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
    notes: Optional[str] = None      # human-readable explanation for the audit trail


def build_master_records() -> list[ReconciliationRecord]:
    """
    Pulls together every tier's output into one normalized list.
    Assumes the following variables already exist in scope from the pipeline run:
    tier1_verified, tier1_exceptions, tier2_proposed, tier3_action1_verified,
    action2_verified, action2_trapped_resolved, bundle_results,
    tier5_failed_no_settlement, tier5_unsettled_receivable, tier5_bank_orphans
    """
    records: list[ReconciliationRecord] = []

    # --- Tier 1: Verified exact matches ---
    for r in tier1_verified.to_dicts():
        records.append(ReconciliationRecord(
            record_id=r["payment_id"],
            record_type="ledger",
            status="Verified",
            resolved_at_tier="Tier 1: Exact",
            resolution_method="exact_id_join",
            confidence=1.0,
            customer_name=r["customer_name"],
            ledger_payment_id=r["payment_id"],
            bank_settlement_utr=r["settlement_utr"],
            gross_amount=r["gross_amount"],
            net_amount=r["net_amount"],
            amount_delta=r["amount_delta"],
            payment_date=str(r["payment_date"]),
            settlement_date=str(r["settlement_date"]),
            notes="Exact payment_id match between ledger and bank narrative.",
        ))

    # --- Tier 1: Deliberate/genuine amount mismatches ---
    for r in tier1_exceptions.to_dicts():
        records.append(ReconciliationRecord(
            record_id=r["payment_id"],
            record_type="ledger",
            status="Exception",
            resolved_at_tier="Tier 1: Exact ID, Amount Mismatch",
            resolution_method="exact_id_amount_exceeded_tolerance",
            confidence=1.0,  # 100% confident the IDs match; the AMOUNT is the exception
            customer_name=r["customer_name"],
            ledger_payment_id=r["payment_id"],
            bank_settlement_utr=r["settlement_utr"],
            gross_amount=r["gross_amount"],
            net_amount=r["net_amount"],
            amount_delta=r["amount_delta"],
            payment_date=str(r["payment_date"]),
            settlement_date=str(r["settlement_date"]),
            notes=f"IDs match exactly but amount differs by {r['amount_delta']:.2f}, exceeding the ₹0.50 tolerance.",
        ))

    # --- Tier 3 Action 1: Re-verified Tier 2 candidates ---
    for r in tier3_action1_verified.to_dicts():
        records.append(ReconciliationRecord(
            record_id=r["payment_id"],
            record_type="ledger",
            status="Verified",
            resolved_at_tier="Tier 3: Fuzzy+Math Verified",
            resolution_method="tier2_candidate_verified",
            confidence=1.0,
            customer_name=r["customer_name"],
            ledger_payment_id=r["payment_id"],
            bank_settlement_utr=r["settlement_utr"],
            gross_amount=r["gross_amount"],
            net_amount=r["net_amount"],
            amount_delta=r.get("final_amount_delta", r.get("amount_delta")),
            notes="Fuzzy-matched candidate re-verified against precise fee-adjusted math (±₹0.50).",
        ))

    # --- Tier 3 Action 2: Math-only verified (corporate aliasing) ---
    for r in action2_verified.to_dicts():
        records.append(ReconciliationRecord(
            record_id=r["payment_id"],
            record_type="ledger",
            status="Verified",
            resolved_at_tier="Tier 3: Math-Only",
            resolution_method="expected_net_match",
            confidence=round(r["identity_score"] / 100, 4),
            customer_name=r["customer_name"],
            ledger_payment_id=r["payment_id"],
            bank_settlement_utr=r["settlement_utr"],
            gross_amount=r["gross_amount"],
            net_amount=r["net_amount"],
            amount_delta=r["amount_delta"],
            notes=f"No ID match; fee-adjusted math matched exactly. Identity score "
                  f"{r['identity_score']:.1f} cleared the veto threshold "
                  f"(bank narrative: '{r['extracted_entity_name']}').",
        ))

    # --- Tier 3 Action 2: REJECTED — precision trap ---
    for r in action2_trapped_resolved.to_dicts():
        records.append(ReconciliationRecord(
            record_id=r["settlement_utr"],
            record_type="bank",
            status="Exception",
            resolved_at_tier="Exception: Math Match - Identity Mismatch",
            resolution_method="math_matched_identity_vetoed",
            confidence=round(r["identity_score"] / 100, 4),
            customer_name=r.get("extracted_entity_name"),
            bank_settlement_utr=r["settlement_utr"],
            net_amount=r["net_amount"],
            amount_delta=r["amount_delta"],
            notes=f"Amount matched ledger customer '{r['customer_name']}' to the paisa, but identity "
                  f"score ({r['identity_score']:.1f}) is far below the veto threshold — flagged as a "
                  f"suspected coincidence, not auto-verified.",
        ))

    # --- Tier 4: AI-verified bundles ---
    for r in bundle_results:
        if r["resolved_at_tier"] == "Tier 4: AI Bundle Verified":
            records.append(ReconciliationRecord(
                record_id=r["settlement_utr"],
                record_type="bank",
                status="Verified",
                resolved_at_tier="Tier 4: AI Bundle",
                resolution_method="subset_sum_llm",
                confidence=r["confidence"],
                bank_settlement_utr=r["settlement_utr"],
                net_amount=r["net_amount"],
                notes=f"Settlement matched to {len(r.get('matched_invoice_ids', []))} bundled invoices "
                      f"({', '.join(r.get('matched_invoice_ids', []))}) via LLM confirmation. "
                      f"{r.get('llm_explanation', '')}",
            ))
        elif r["resolved_at_tier"] in (
            "Ambiguous Bundle - Manual Review Required",
            "Tier 4: Skipped - Pool Too Large",
            "Tier 4: LLM Call Failed",
        ):
            records.append(ReconciliationRecord(
                record_id=r["settlement_utr"],
                record_type="bank",
                status="Manual Review",
                resolved_at_tier=r["resolved_at_tier"],
                resolution_method=r["resolution_method"],
                confidence=r.get("confidence", 0.0),
                bank_settlement_utr=r["settlement_utr"],
                net_amount=r["net_amount"],
                notes=r.get("low_confidence_reason", "No confident bundle match found."),
            ))

    # --- Tier 5: Failed payments (no settlement expected) ---
    for r in tier5_failed_no_settlement.to_dicts():
        records.append(ReconciliationRecord(
            record_id=r["payment_id"],
            record_type="ledger",
            status="No Settlement Expected",
            resolved_at_tier="Tier 5: No Settlement Expected",
            resolution_method="failed_status_excluded",
            confidence=1.0,
            customer_name=r["customer_name"],
            ledger_payment_id=r["payment_id"],
            gross_amount=r["gross_amount"],
            payment_date=str(r["payment_date"]),
            notes="Payment failed at the gateway; no fee was ever charged and no settlement is expected.",
        ))

    # --- Tier 5: Genuine unsettled receivables ---
    for r in tier5_unsettled_receivable.to_dicts():
        records.append(ReconciliationRecord(
            record_id=r["payment_id"],
            record_type="ledger",
            status="Exception",
            resolved_at_tier="Tier 5: Unsettled/Pending Receivable",
            resolution_method="no_bank_match_found",
            confidence=0.0,
            customer_name=r["customer_name"],
            ledger_payment_id=r["payment_id"],
            gross_amount=r["gross_amount"],
            payment_date=str(r["payment_date"]),
            notes="Ledger shows a captured/refunded payment with no corresponding bank settlement found. "
                  "Needs follow-up with the ops/payments team.",
        ))

    # --- Tier 5: Unexplained bank credits ---
    for r in tier5_bank_orphans.to_dicts():
        records.append(ReconciliationRecord(
            record_id=r["settlement_utr"],
            record_type="bank",
            status="Exception",
            resolved_at_tier="Tier 5: Unexplained Bank Credit",
            resolution_method="no_ledger_match_found",
            confidence=0.0,
            bank_settlement_utr=r["settlement_utr"],
            net_amount=r["net_amount"],
            settlement_date=str(r["settlement_date"]),
            notes="Bank credit with no plausible ledger match at any tier — could be a bank error, "
                  "an unrecorded manual entry, or a payment outside this system's scope.",
        ))

    return records


# ==========================================
# BUILD AND EXPORT
# ==========================================
print("\n--- Building Master Reconciliation Record Table ---")
master_records = build_master_records()
print(f"Total records: {len(master_records)}")

# Convert to a Polars DataFrame for DuckDB/Streamlit consumption
master_df = pl.DataFrame([r.model_dump() for r in master_records])

# Sanity check: every original row should appear exactly once
status_counts = master_df.group_by("status").len().sort("len", descending=True)
print("\nStatus breakdown:")
print(status_counts)

tier_counts = master_df.group_by("resolved_at_tier").len().sort("len", descending=True)
print("\nResolution by tier:")
print(tier_counts)

# Persist for the UI / Q&A agent to consume
master_df.write_parquet("data/master_reconciliation_records.parquet")
if os.path.exists("src/data"):
    master_df.write_parquet("src/data/master_reconciliation_records.parquet")
print("\n✅ Saved data/master_reconciliation_records.parquet")
