import polars as pl
import random
from datetime import datetime, timedelta
from pathlib import Path

# 1. Load the clean ledger
data_dir = Path(__file__).with_name("data")
ledger_df = pl.read_csv(data_dir / "merchant_ledger.csv", try_parse_dates=True)

# 2. Separate 'failed' payments
active_df = ledger_df.filter(pl.col("status") != "failed")
captured_only = active_df.filter(pl.col("status") == "captured")

# Helper to calculate exact expected net (forced to 2 decimals)
def get_expected_net(row):
    val = row["gross_amount"] - row["mdr_fee"] - row["gst_on_mdr"] - row["refund_amount"]
    return round(val, 2)

# Helper to simulate bank settlement dates (T+2)
def shift_date(date_val, days=2):
    if isinstance(date_val, datetime):
        return (date_val + timedelta(days=days)).strftime("%Y-%m-%d")
    date_obj = datetime.strptime(str(date_val), "%Y-%m-%d")
    return (date_obj + timedelta(days=days)).strftime("%Y-%m-%d")

bank_rows = []
used_payment_ids = set()

print("Generating mathematically sound bank settlement data...")

# --- TIER 4: AI Bundles (Strictly grouped by SAME customer) ---
customer_counts = captured_only.group_by("customer_name").len().sort("len", descending=True)
bundle_customers = customer_counts.head(2)["customer_name"].to_list()

for cust in bundle_customers:
    customer_rows = captured_only.filter(pl.col("customer_name") == cust).sort("payment_date").to_dicts()
    rows = next(
        (
            customer_rows[start:start + 3]
            for start in range(len(customer_rows) - 2)
            if (customer_rows[start + 2]["payment_date"] - customer_rows[start]["payment_date"]).days <= 5
        ),
        [],
    )
    if len(rows) < 3: continue

    total_net = round(sum([get_expected_net(r) for r in rows]), 2)
    first_date = min([r["payment_date"] for r in rows])
    
    bank_rows.append({
        "settlement_utr": f"SETL{str(first_date).replace('-','')}{random.randint(1000,9999)}",
        "transaction_description": f"TIER4-BULK-{cust}-SETTLEMENT",
        "net_amount": total_net,
        "settlement_date": shift_date(first_date)
    })
    used_payment_ids.update([r["payment_id"] for r in rows])

# Remove T4 rows from the pool
pool_for_t123 = active_df.filter(~pl.col("payment_id").is_in(list(used_payment_ids))).sample(fraction=1.0, shuffle=True)

# --- TIER 1: Exact Match (20 rows, first 3 broken by exactly 500.00) ---
t1_slice = pool_for_t123.slice(0, 20)
for i, row in enumerate(t1_slice.to_dicts()):
    net_amount = get_expected_net(row)
    if i < 3:
        net_amount = round(net_amount - 500.00, 2) # Explicit injection
        
    bank_rows.append({
        "settlement_utr": f"SETL{str(row['payment_date']).replace('-','')}{random.randint(1000,9999)}",
        "transaction_description": f"TIER1-NEFT-{row['payment_id']}-{row['customer_name']}",
        "net_amount": net_amount,
        "settlement_date": shift_date(row["payment_date"])
    })
used_payment_ids.update(t1_slice["payment_id"].to_list())

# --- TIER 2: Fuzzy Match (10 rows) ---
t2_slice = pool_for_t123.slice(20, 10)
for row in t2_slice.to_dicts():
    net_amount = get_expected_net(row)
    typo_name = row["customer_name"][:-2] + "xx" 
    bank_rows.append({
        "settlement_utr": f"SETL{str(row['payment_date']).replace('-','')}{random.randint(1000,9999)}",
        "transaction_description": f"TIER2-RTGS-{typo_name}-REF{random.randint(100,999)}",
        "net_amount": net_amount,
        "settlement_date": shift_date(row["payment_date"], 1)
    })
used_payment_ids.update(t2_slice["payment_id"].to_list())

# --- TIER 3: Math-Only Match (5 rows) ---
t3_slice = pool_for_t123.slice(30, 5)
for row in t3_slice.to_dicts():
    net_amount = get_expected_net(row)
    fake_name = f"Individual {random.choice(['Rahul', 'Priya', 'Amit', 'Sneha'])}"
    bank_rows.append({
        "settlement_utr": f"IMPS{random.randint(100000000000,999999999999)}",
        "transaction_description": f"TIER3-IMPS-{fake_name}-TRF",
        "net_amount": net_amount,
        "settlement_date": shift_date(row["payment_date"])
    })
used_payment_ids.update(t3_slice["payment_id"].to_list())

# --- TIER 3b: Corporate Aliasing Match (3 rows) ---
alias_map = {
    "Acme Corp": "Acme Trading Co",
    "Wayne Enterprises": "Wayne Ent",
    "Stark Industries": "Stark Ind Pvt Ltd",
    "Umbrella Inc": "Umbr Incorp",
    "Razorpay Software": "Razor Soft",
}

# Globex deliberately excluded: no alias found that lands reliably in the 60-74 band.
eligible_pool = pool_for_t123.filter(pl.col("customer_name").is_in(list(alias_map.keys())))
t3b_slice = eligible_pool.slice(35, 3)
for row in t3b_slice.to_dicts():
    net_amount = get_expected_net(row)
    alias_name = alias_map[row["customer_name"]]
    bank_rows.append({
        "settlement_utr": f"SETL{str(row['payment_date']).replace('-', '')}{random.randint(1000,9999)}",
        "transaction_description": f"TIER3B-NEFT-{alias_name}-INV",
        "net_amount": net_amount,
        "settlement_date": shift_date(row["payment_date"])
    })
used_payment_ids.update(t3b_slice["payment_id"].to_list())

# --- TIER 5: Bank Orphans (8 rows with random amounts) ---
for i in range(8):
    random_date = f"2026-08-{random.randint(1, 20):02d}"
    bank_rows.append({
        "settlement_utr": f"SETL{random_date.replace('-','')}{random.randint(1000,9999)}",
        "transaction_description": f"TIER5-ORPHAN-UNKNOWN-CORP-{random.randint(1000,9999)}",
        "net_amount": round(random.randint(1000, 50000) + random.random(), 2),
        "settlement_date": shift_date(random_date)
    })

# 4. Shuffle the bank rows
random.shuffle(bank_rows)

# 5. Save to CSV
bank_df = pl.DataFrame(bank_rows)
bank_df.write_csv(data_dir / "bank_settlement.csv")

# --- VERIFICATION STATS ---
total_ledger_rows = len(ledger_df)
failed_count = len(ledger_df.filter(pl.col("status") == "failed"))
active_unconsumed = len(active_df) - len(used_payment_ids)

print("\n✅ Success! bank_settlement.csv created.")
print(f"Total Bank Rows: {len(bank_df)}")
print(f"Total Ledger Rows Used in Bank File: {len(used_payment_ids)}")
print(f"--- TIER 5 LEDGER ORPHANS BREAKDOWN ---")
print(f"Failed Payments (No Settlement Expected): {failed_count}")
print(f"Active Unconsumed (Unsettled/Pending Receivable): {active_unconsumed}")
print(f"Total Ledger Orphans: {failed_count + active_unconsumed}")