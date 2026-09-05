"""paste of the module for testing"""
from __future__ import annotations
import datetime
from datetime import date, timedelta
from typing import Optional, Dict, Any
import polars as pl


BUCKET_NEXT_7 = "Next 7 Days"
BUCKET_8_14 = "8-14 Days"
BUCKET_15_30 = "15-30 Days"
BUCKET_30_PLUS_OR_OVERDUE = "30+ Days / Overdue"

ALL_BUCKETS = [
    BUCKET_NEXT_7,
    BUCKET_8_14,
    BUCKET_15_30,
    BUCKET_30_PLUS_OR_OVERDUE,
]


def _to_date_series(series: pl.Series) -> pl.Series:
    if series.dtype in (pl.Utf8, pl.String):
        return series.str.to_date()
    elif series.dtype == pl.Date:
        return series
    elif series.dtype == pl.Datetime:
        return series.cast(pl.Date)
    return series.cast(pl.Date)


def compute_historical_lags(
    df: pl.DataFrame,
    min_customer_samples: int = 2,
    default_fallback_lag: int = 2,
) -> Dict[str, Any]:
    if len(df) == 0:
        return {
            "overall_median_lag": float(default_fallback_lag),
            "overall_mean_lag": float(default_fallback_lag),
            "customer_median_lags": {},
            "sample_count": 0,
        }

    verified = df.filter(
        (pl.col("status") == "Verified")
        & pl.col("payment_date").is_not_null()
        & pl.col("settlement_date").is_not_null()
    )

    if len(verified) == 0:
        return {
            "overall_median_lag": float(default_fallback_lag),
            "overall_mean_lag": float(default_fallback_lag),
            "customer_median_lags": {},
            "sample_count": 0,
        }

    verified = verified.with_columns(
        (
            _to_date_series(verified["settlement_date"])
            - _to_date_series(verified["payment_date"])
        )
        .dt.total_days()
        .alias("lag_days")
    )

    valid_lags = verified.filter(pl.col("lag_days").is_not_null() & (pl.col("lag_days") >= 0))
    if len(valid_lags) == 0:
        return {
            "overall_median_lag": float(default_fallback_lag),
            "overall_mean_lag": float(default_fallback_lag),
            "customer_median_lags": {},
            "sample_count": 0,
        }

    overall_median = float(valid_lags["lag_days"].median() or default_fallback_lag)
    overall_mean = float(valid_lags["lag_days"].mean() or default_fallback_lag)

    cust_lags: Dict[str, float] = {}
    if "customer_name" in valid_lags.columns:
        cust_counts = valid_lags.filter(pl.col("customer_name").is_not_null()).group_by("customer_name").agg([
            pl.len().alias("count"),
            pl.col("lag_days").median().alias("cust_median"),
        ])
        for row in cust_counts.to_dicts():
            if row["count"] >= min_customer_samples and row["cust_median"] is not None:
                cust_lags[row["customer_name"]] = float(row["cust_median"])

    return {
        "overall_median_lag": overall_median,
        "overall_mean_lag": overall_mean,
        "customer_median_lags": cust_lags,
        "sample_count": len(valid_lags),
    }


def forecast_unsettled_receivables(
    df: pl.DataFrame,
    as_of_date: Optional[date] = None,
    min_customer_samples: int = 2,
    default_fallback_lag: int = 2,
) -> pl.DataFrame:
    out_schema = {
        "record_id": pl.Utf8,
        "ledger_payment_id": pl.Utf8,
        "customer_name": pl.Utf8,
        "gross_amount": pl.Float64,
        "payment_date": pl.Date,
        "applied_lag_days": pl.Int64,
        "lag_source": pl.Utf8,
        "projected_settlement_date": pl.Date,
        "days_until_settlement": pl.Int64,
        "bucket": pl.Utf8,
        "is_overdue": pl.Boolean,
    }

    if len(df) == 0:
        return pl.DataFrame(schema=out_schema)

    ref_date = as_of_date or date.today()

    lag_info = compute_historical_lags(
        df,
        min_customer_samples=min_customer_samples,
        default_fallback_lag=default_fallback_lag,
    )
    overall_median = round(lag_info["overall_median_lag"])
    cust_lags = lag_info["customer_median_lags"]

    unsettled = df.filter(
        pl.col("resolved_at_tier") == "Tier 5: Unsettled/Pending Receivable"
    )

    if len(unsettled) == 0:
        return pl.DataFrame(schema=out_schema)

    unsettled_dicts = unsettled.to_dicts()
    rows = []

    for r in unsettled_dicts:
        raw_pdate = r.get("payment_date")
        if raw_pdate is None:
            continue

        if isinstance(raw_pdate, str):
            pdate = date.fromisoformat(raw_pdate[:10])
        elif isinstance(raw_pdate, (date, datetime.datetime)):
            pdate = raw_pdate if isinstance(raw_pdate, date) else raw_pdate.date()
        else:
            continue

        cname = r.get("customer_name")
        if cname and cname in cust_lags:
            applied_lag = round(cust_lags[cname])
            source = f"Customer Median ({cname})"
        else:
            applied_lag = overall_median
            source = "Overall Median"

        projected_date = pdate + timedelta(days=applied_lag)
        days_until = (projected_date - ref_date).days
        is_overdue = days_until < 0

        if days_until < 0 or days_until > 30:
            bucket = BUCKET_30_PLUS_OR_OVERDUE
        elif 0 <= days_until <= 7:
            bucket = BUCKET_NEXT_7
        elif 8 <= days_until <= 14:
            bucket = BUCKET_8_14
        else:
            bucket = BUCKET_15_30

        rows.append({
            "record_id": str(r.get("record_id") or r.get("ledger_payment_id") or ""),
            "ledger_payment_id": str(r.get("ledger_payment_id") or r.get("record_id") or ""),
            "customer_name": str(cname or "Unknown Entity"),
            "gross_amount": float(r.get("gross_amount") or 0.0),
            "payment_date": pdate,
            "applied_lag_days": int(applied_lag),
            "lag_source": str(source),
            "projected_settlement_date": projected_date,
            "days_until_settlement": int(days_until),
            "bucket": bucket,
            "is_overdue": bool(is_overdue),
        })

    if not rows:
        return pl.DataFrame(schema=out_schema)

    forecast_df = pl.DataFrame(rows)
    forecast_df = forecast_df.with_columns([
        pl.col("payment_date").cast(pl.Date),
        pl.col("projected_settlement_date").cast(pl.Date),
        pl.col("applied_lag_days").cast(pl.Int64),
        pl.col("days_until_settlement").cast(pl.Int64),
    ])
    return forecast_df


def aggregate_cash_forecast_by_bucket(forecast_df: pl.DataFrame) -> pl.DataFrame:
    bucket_order = ALL_BUCKETS

    if len(forecast_df) == 0:
        return pl.DataFrame({
            "bucket": bucket_order,
            "amount": [0.0] * len(bucket_order),
            "count": [0] * len(bucket_order),
        })

    agg = forecast_df.group_by("bucket").agg([
        pl.col("gross_amount").sum().alias("amount"),
        pl.len().alias("count"),
    ]).to_dicts()

    agg_map = {row["bucket"]: row for row in agg}

    out_rows = []
    for b in bucket_order:
        if b in agg_map:
            out_rows.append({
                "bucket": b,
                "amount": round(agg_map[b]["amount"], 2),
                "count": int(agg_map[b]["count"]),
            })
        else:
            out_rows.append({
                "bucket": b,
                "amount": 0.0,
                "count": 0,
            })

    return pl.DataFrame(out_rows)