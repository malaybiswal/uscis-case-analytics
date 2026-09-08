import os
import re
import asyncio
from datetime import datetime, date
import asyncpg

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "postgres"),
    "database": os.getenv("POSTGRES_DB", "uscis_db"),
    "user": os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD", "postgres"),
    "port": int(os.getenv("DB_PORT", 5432)),
}

MALAY_DATE = date(2025, 10, 2)
MALAY_RECEIPT_SEQ = 2690002699

FINAL_STATUSES = [
    "card was produced",
    "card was mailed to me",
    "card was delivered",
    "new card is being produced",
    "case was approved",
    "approval notice was sent",
    "decision notice sent",
    "case was denied",
    "case closed"
]

def extract_sequence(receipt: str) -> int | None:
    if not receipt:
        return None
    digits = re.sub(r"\D", "", receipt)
    return int(digits) if digits else None

def parse_date(date_val) -> date | None:
    if not date_val:
        return None
    if isinstance(date_val, date) and not isinstance(date_val, datetime):
        return date_val
    if isinstance(date_val, datetime):
        return date_val.date()

    if isinstance(date_val, str):
        date_str = date_val.strip()
        if not date_str:
            return None

        if "T" in date_str:
            date_str = date_str.split("T")[0]

        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue

    return None

async def populate_gc_analysis():
    print("Connecting to database...")
    conn = await asyncpg.connect(**DB_CONFIG)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS gc_analysis (
            receipt_number VARCHAR(20) PRIMARY KEY,
            form_type VARCHAR(20),
            receipt_sequence BIGINT,
            filed_date DATE,
            status_date DATE,
            is_final BOOLEAN DEFAULT FALSE,
            approval_or_card_date DATE,
            days_pending_or_to_final INT,
            filed_after_malay BOOLEAN DEFAULT FALSE,
            receipt_after_malay BOOLEAN DEFAULT FALSE,
            has_rfe BOOLEAN DEFAULT FALSE,
            rfe_date DATE,
            rfe_response_date DATE,
            last_derived_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_gc_seq ON gc_analysis(receipt_sequence);
        CREATE INDEX IF NOT EXISTS idx_gc_filed_date ON gc_analysis(filed_date);
    """)

    print("Fetching raw records from case_records...")
    rows = await conn.fetch("""
        SELECT receipt_number, form_type, current_status, status_description,
               filed_date, status_date, submission_filing_date, current_status_date,
               rfe_date, rfe_response_date, full_status_history, status_history
        FROM case_records
    """)

    print(f"Processing {len(rows)} records into gc_analysis...")

    analysis_data = []
    for r in rows:
        receipt = r["receipt_number"]
        form = r["form_type"]
        status = (r["current_status"] or "").lower()
        desc = (r["status_description"] or "").lower()

        filed_dt = parse_date(r["filed_date"]) or parse_date(r["submission_filing_date"])
        status_dt = parse_date(r["status_date"]) or parse_date(r["current_status_date"])

        history = r["status_history"] or []

        seq = extract_sequence(receipt)
        
        status_combined = f"{status} {desc}"
        is_final = any(term in status_combined for term in FINAL_STATUSES)
        approval_date = status_dt if (is_final and isinstance(status_dt, date)) else None

        days_pending = None
        if isinstance(filed_dt, date):
            end_dt = status_dt if (is_final and isinstance(status_dt, date)) else date.today()
            days_pending = (end_dt - filed_dt).days

        filed_after = (filed_dt > MALAY_DATE) if isinstance(filed_dt, date) else False
        receipt_after = (seq > MALAY_RECEIPT_SEQ) if seq is not None else False

        has_rfe = "request for evidence" in status or any("request for evidence" in str(h).lower() for h in history)
        rfe_dt = parse_date(r["rfe_date"])
        rfe_resp_dt = parse_date(r["rfe_response_date"])

        analysis_data.append((
            receipt, form, seq, filed_dt, status_dt, is_final, approval_date, days_pending,
            filed_after, receipt_after, has_rfe, rfe_dt, rfe_resp_dt
        ))

    print("Upserting derived metrics into gc_analysis...")
    await conn.executemany("""
        INSERT INTO gc_analysis (
            receipt_number, form_type, receipt_sequence, filed_date, status_date, is_final,
            approval_or_card_date, days_pending_or_to_final,
            filed_after_malay, receipt_after_malay, has_rfe,
            rfe_date, rfe_response_date, last_derived_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, NOW())
        ON CONFLICT (receipt_number) DO UPDATE SET
            form_type = EXCLUDED.form_type,
            receipt_sequence = EXCLUDED.receipt_sequence,
            filed_date = EXCLUDED.filed_date,
            status_date = EXCLUDED.status_date,
            is_final = EXCLUDED.is_final,
            approval_or_card_date = EXCLUDED.approval_or_card_date,
            days_pending_or_to_final = EXCLUDED.days_pending_or_to_final,
            filed_after_malay = EXCLUDED.filed_after_malay,
            receipt_after_malay = EXCLUDED.receipt_after_malay,
            has_rfe = EXCLUDED.has_rfe,
            rfe_date = EXCLUDED.rfe_date,
            rfe_response_date = EXCLUDED.rfe_response_date,
            last_derived_at = NOW();
    """, analysis_data)

    print("Data processing complete. `gc_analysis` updated successfully.")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(populate_gc_analysis())