import asyncio
import logging
import os
import random
import asyncpg
from playwright.async_api import async_playwright, Page, BrowserContext

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Fetch database credentials from environment variables
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "postgres"),
    "database": os.getenv("POSTGRES_DB", "uscis_db"),
    "user": os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD", "postgres"),
    "port": int(os.getenv("DB_PORT", 5432)),
}

def generate_receipt_numbers(prefix="WAC", fiscal_year="26", sequence_start=9000000, count=50):
    """
    Generates sequential receipt numbers (e.g., WAC269000000 to WAC269000049).
    """
    receipts = []
    for i in range(count):
        seq = str(sequence_start + i).zfill(8)
        receipts.append(f"{prefix}{fiscal_year}{seq}")
    return receipts

async def create_db_pool():
    """Initializes the database pool and syncs table schemas."""
    pool = await asyncpg.create_pool(**DB_CONFIG)

    # Automatically ensure all columns exist on startup
    async with pool.acquire() as conn:
        for table in ["case_records", "other_case_records"]:
            await conn.execute(f"""
                ALTER TABLE {table} 
                ADD COLUMN IF NOT EXISTS status_description TEXT,
                ADD COLUMN IF NOT EXISTS status_date TEXT,
                ADD COLUMN IF NOT EXISTS filed_date TEXT,
                ADD COLUMN IF NOT EXISTS processing_center TEXT,
                ADD COLUMN IF NOT EXISTS playbook_headline TEXT,
                ADD COLUMN IF NOT EXISTS playbook_meaning TEXT,
                ADD COLUMN IF NOT EXISTS status_history TEXT[],
                ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
            """)

    return pool

async def parse_and_save_case(page: Page, receipt_number: str, db_pool: asyncpg.Pool) -> bool:
    """
    Parses case details from MyCasesHub DOM and saves/upserts to PostgreSQL.
    Skips insertion if the receipt does not exist or fails to hydrate.
    """
    # 1. Wait for element attachment rather than strict visual layout stability
    await page.wait_for_selector("aside.case-info-panel", state="attached", timeout=8000)

    # 2. Wait for hydration (text content replacing "Loading...")
    try:
        await page.wait_for_function(
            """() => {
                const el = document.querySelector('aside.case-info-panel');
                return el && !el.innerText.includes('Loading case information');
            }""",
            timeout=6000
        )
    except Exception:
        logging.warning(f"[{receipt_number}] Timed out waiting for full hydration, attempting parse anyway.")

    # 3. Extract Key-Value Facts early to validate case existence
    facts = {}
    fact_items = await page.query_selector_all(".panel-facts-group .info-item")
    for item in fact_items:
        label_el = await item.query_selector(".info-label")
        value_el = await item.query_selector(".info-value")
        if label_el and value_el:
            key = (await label_el.inner_text()).strip().lower().replace(" ", "_")
            val = " ".join((await value_el.inner_text()).split())
            facts[key] = val

    form_type = facts.get("case_type", "Unknown")

    # 4. Validation Check: Ignore non-existent / empty cases
    badge_el = await page.query_selector("aside.case-info-panel .status-badge span")
    status_badge = (await badge_el.inner_text()).strip() if badge_el else ""

    title_el = await page.query_selector("aside.case-info-panel .status-title")
    status_title = (await title_el.inner_text()).strip() if title_el else ""

    current_status = f"{status_badge} - {status_title}".strip(" -") or "Unknown"

    # STRICT GUARD: Check for unknown forms, loading states, or unable-to-load states
    invalid_status_keywords = [
        "loading case information", 
        "unable to load case information", 
        "case record does not exist",
        "unknown"
    ]

    if form_type.lower() == "unknown" or any(keyword in current_status.lower() for keyword in invalid_status_keywords):
        logging.info(f"[{receipt_number}] Skipping DB insert: Invalid or unhydrated status ('{current_status}').")
        return False

    # 5. Extract Remaining Status Details
    desc_el = await page.query_selector("aside.case-info-panel .status-description")
    status_description = (await desc_el.inner_text()).strip() if desc_el else ""

    date_el = await page.query_selector("aside.case-info-panel .status-date")
    status_date = (await date_el.inner_text()).strip() if date_el else ""

    pb_headline_el = await page.query_selector(".case-info-playbook .playbook-headline")
    playbook_headline = (await pb_headline_el.inner_text()).strip() if pb_headline_el else ""

    pb_meaning_el = await page.query_selector(".case-info-playbook .playbook-meaning")
    playbook_meaning = (await pb_meaning_el.inner_text()).strip() if pb_meaning_el else ""

    filed_date = facts.get("filed_date", None)
    processing_center = facts.get("processing_center", None)

    # 6. Extract Timeline History
    history_items = await page.query_selector_all(".history-timeline .timeline-item")
    status_history = []
    for item in history_items:
        h_date_el = await item.query_selector(".timeline-date")
        h_title_el = await item.query_selector(".timeline-title")
        h_date = (await h_date_el.inner_text()).strip() if h_date_el else ""
        h_title = (await h_title_el.inner_text()).strip() if h_title_el else ""
        if h_title:
            status_history.append(f"{h_date}: {h_title}".strip(": "))

    # 7. Route & Upsert
    target_table = "case_records" if "I-485" in form_type else "other_case_records"

    async with db_pool.acquire() as conn:
        query = f"""
            INSERT INTO {target_table} (
                receipt_number, form_type, current_status, status_description, 
                status_date, filed_date, processing_center, playbook_headline, 
                playbook_meaning, status_history, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
            ON CONFLICT (receipt_number) 
            DO UPDATE SET
                form_type = EXCLUDED.form_type,
                current_status = EXCLUDED.current_status,
                status_description = EXCLUDED.status_description,
                status_date = EXCLUDED.status_date,
                filed_date = EXCLUDED.filed_date,
                processing_center = EXCLUDED.processing_center,
                playbook_headline = EXCLUDED.playbook_headline,
                playbook_meaning = EXCLUDED.playbook_meaning,
                status_history = EXCLUDED.status_history,
                updated_at = NOW();
        """
        await conn.execute(
            query,
            receipt_number,
            form_type,
            current_status,
            status_description,
            status_date,
            filed_date,
            processing_center,
            playbook_headline,
            playbook_meaning,
            status_history
        )

    logging.info(f"[{receipt_number}] Successfully saved ({form_type}) into {target_table}")
    return True

async def create_browser_context(browser) -> BrowserContext:
    """Helper to initialize browser context with anti-bot configurations."""
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        viewport={"width": 1920, "height": 1080}
    )
    await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    return context

async def process_single_receipt(
    context: BrowserContext, 
    receipt: str, 
    db_pool: asyncpg.Pool, 
    max_retries: int = 3
) -> bool:
    """Processes a single receipt with exponential backoff retries (e.g. 2s, 4s, 8s)."""
    url = f"https://mycaseshub.com/analysis/{receipt}"

    for attempt in range(1, max_retries + 1):
        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            
            if response and response.status >= 400:
                logging.warning(f"[{receipt}] Received HTTP {response.status} (Attempt {attempt}/{max_retries})")
                raise Exception(f"HTTP Status {response.status}")

            return await parse_and_save_case(page, receipt, db_pool)

        except Exception as e:
            backoff_delay = (2 ** attempt) + random.uniform(0.5, 1.5)
            if attempt < max_retries:
                logging.warning(
                    f"[{receipt}] Attempt {attempt}/{max_retries} failed: {e}. "
                    f"Retrying in {backoff_delay:.2f}s..."
                )
                await asyncio.sleep(backoff_delay)
            else:
                logging.error(f"[{receipt}] All {max_retries} attempts failed: {e}")
                return False
        finally:
            await page.close()

async def main():
    receipt_numbers = generate_receipt_numbers(
        prefix="WAC", 
        fiscal_year="26", 
        sequence_start=90041864, 
        count=30000
    )

    db_pool = await create_db_pool()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled", 
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage"
            ]
        )
        
        context = await create_browser_context(browser)

        for idx, receipt in enumerate(receipt_numbers, start=1):
            logging.info(f"[{idx}/{len(receipt_numbers)}] Processing {receipt} -> https://mycaseshub.com/analysis/{receipt}")
            
            await process_single_receipt(context, receipt, db_pool)

            # Recycle context every 100 iterations to maintain stability
            if idx % 100 == 0:
                logging.info("Recycling browser context to maintain stability...")
                await context.close()
                context = await create_browser_context(browser)

            delay = random.uniform(2.0, 4.0)
            await asyncio.sleep(delay)

        await context.close()
        await browser.close()
    
    await db_pool.close()

if __name__ == "__main__":
    asyncio.run(main())