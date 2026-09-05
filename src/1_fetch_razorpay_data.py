import os
import razorpay
import polars as pl
from pathlib import Path
from dotenv import load_dotenv
import random
import time
import uuid

load_dotenv()

# 1. Initialize Client
client = razorpay.Client(auth=(os.getenv("RAZORPAY_KEY_ID"), os.getenv("RAZORPAY_KEY_SECRET")))

# 2. Generate 55 Orders
merchant_ledger_data = []
customers = ["Acme Corp", "Razorpay Software", "Globex", "Umbrella Inc", "Wayne Enterprises", "Stark Industries"]

print("Creating orders via Razorpay Test API...")

for i in range(1, 56):
    # Generate amounts with paise (e.g., 1543.50)
    amount_in_paise = random.randint(100000, 5000000) 
    gross_amount = amount_in_paise / 100 
    
    # Create order via API
    order = client.order.create({
        "amount": amount_in_paise,
        "currency": "INR",
        "receipt": f"receipt_{i}"
    })
    
    # Generate completely independent payment_id
    payment_id = f"pay_{uuid.uuid4().hex[:14]}"
    customer = random.choice(customers)
    
    # Domain-accurate status and fee logic
    rand_val = random.random()
    
    if rand_val < 0.75:
        # 75% Captured
        status = "captured"
        mdr_fee = round(gross_amount * 0.02, 2)
        gst_on_mdr = round(mdr_fee * 0.18, 2)
        refund_amount = 0.0
        
    elif rand_val < 0.85:
        # 10% Refunded (Mix of partial and FULL)
        status = "refunded"
        # MDR/GST are still calculated on original gross
        mdr_fee = round(gross_amount * 0.02, 2)
        gst_on_mdr = round(mdr_fee * 0.18, 2)
        
        # Guarantee some full refunds (1.0) and some partial (0.25, 0.5)
        refund_multiplier = random.choice([0.25, 0.5, 1.0, 1.0]) 
        refund_amount = round(gross_amount * refund_multiplier, 2)
        
    else:
        # 15% Failed (No settlement, no fees)
        status = "failed"
        mdr_fee = 0.0
        gst_on_mdr = 0.0
        refund_amount = 0.0
        
    merchant_ledger_data.append({
        "order_id": order['id'],
        "payment_id": payment_id,
        "customer_name": customer,
        "gross_amount": gross_amount,
        "mdr_fee": mdr_fee,
        "gst_on_mdr": gst_on_mdr,
        "refund_amount": refund_amount,
        "payment_date": f"2026-08-{random.randint(1, 20):02d}",
        "status": status
    })
    
    print(f"Created {i}/55: {order['id']} | Status: {status} | Refund: {refund_amount}")
    time.sleep(0.5) # Be polite to the API rate limits

# 3. Save to CSV using Polars
df = pl.DataFrame(merchant_ledger_data)
df.write_csv(Path(__file__).with_name("data") / "merchant_ledger.csv")
print("\n✅ Success! merchant_ledger.csv created with 55 records.")

# 4. Capture and print the Regex patterns for Tier 0
sample_order_id = merchant_ledger_data[0]['order_id']
sample_payment_id = merchant_ledger_data[0]['payment_id']

order_pattern = f"^{sample_order_id.split('_')[0]}_[a-zA-Z0-9]{{14}}$"
pay_pattern = f"^{sample_payment_id.split('_')[0]}_[a-zA-Z0-9]{{14}}$"

print(f"\n🔍 Captured Formats:")
print(f"Order ID: {sample_order_id} -> Regex: {order_pattern}")
print(f"Payment ID: {sample_payment_id} -> Regex: {pay_pattern}")