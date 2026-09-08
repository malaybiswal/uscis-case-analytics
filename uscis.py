import os
import time
import random
import logging
import requests
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
from playwright.sync_api import sync_playwright

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# Database Configuration
DB_CONFIG = {
    "dbname": os.environ.get("POSTGRES_DB", "uscis_db"),
    "user": os.environ.get("POSTGRES_USER", "postgres"),
    "password": os.environ.get("POSTGRES_PASSWORD", "postgres"),
    "host": os.environ.get("POSTGRES_HOST", "localhost"),
    "port": os.environ.get("POSTGRES_PORT", "5432"),
}

# Initialize Database Connection Pool
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, **DB_CONFIG)
    logging.info("PostgreSQL connection pool initialized successfully.")
except Exception as e:
    logging.error(f"Failed to connect to PostgreSQL: {e}")
    raise SystemExit(e)


@contextmanager
def get_db_cursor():
    """Context manager to borrow and return database connections safely."""
    conn = db_pool.getconn()
    try:
        cursor = conn.cursor()
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        db_pool.putconn(conn)


def get_uscis_auth_token():
    """Uses Playwright's API context to fetch the token directly without rendering HTML pages."""
    auth_url = "https://egov.uscis.gov/csol-api/ui-auth"
    logging.info("Starting Playwright context to retrieve auth token...")
    
    try:
        with sync_playwright() as p:
            # Create a request context with custom browser headers
            request_context = p.request.new_context(
                extra_http_headers={
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                    "Origin": "https://egov.uscis.gov",
                    "Referer": "https://egov.uscis.gov/"
                }
            )
            
            logging.info(f"Requesting token directly from {auth_url}...")
            # Set a 60-second timeout specifically for slow network hops
            response = request_context.get(auth_url, timeout=60000)
            
            if response.status == 200:
                data = response.json()
                token = data.get("JwtResponse", {}).get("accessToken")
                request_context.dispose()
                
                if token:
                    logging.info("Auth token acquired successfully.")
                    return token
                else:
                    logging.error("Response missing JwtResponse.accessToken field.")
                    return None
            else:
                logging.error(f"Auth token request failed with status code: {response.status}")
                request_context.dispose()
                return None

    except Exception as e:
        logging.error(f"Playwright API request failed: {e}")
        return None

def fetch_uscis_case_status(receipt_number, auth_token):
    """
    Queries the backend API for case status.
    Returns parsed dictionary on success, or None on error/missing data.
    """
    endpoint = f"https://egov.uscis.gov/csol-api/case-statuses/{receipt_number}"
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    }

    try:
        response = requests.get(endpoint, headers=headers, timeout=12)
        
        if response.status_code == 429:
            logging.warning(f"Rate limited (429) on receipt {receipt_number}. Skipping.")
            return None

        response.raise_for_status()
        json_data = response.json()

        # Check if response actually contains valid case details
        case_response = json_data.get("CaseStatusResponse", {})
        details_eng = case_response.get("detailsEng", {})

        action_text = details_eng.get("actionCodeText")
        action_desc = details_eng.get("actionCodeDesc")

        # Validate that payload is NOT an empty or error state
        if not action_text or "Unable to load case information" in action_text:
            logging.warning(f"Received empty or non-valid data payload for {receipt_number}")
            return None

        return {
            "receipt_number": receipt_number,
            "current_status": action_text,
            "status_description": action_desc,
            "form_type": case_response.get("formType"),
            "submission_filing_date": case_response.get("filingDate")
        }

    except requests.exceptions.RequestException as req_err:
        logging.error(f"HTTP request error fetching {receipt_number}: {req_err}")
        return None
    except Exception as parse_err:
        logging.error(f"Error parsing JSON payload for {receipt_number}: {parse_err}")
        return None


def upsert_case_record(record):
    """Upserts valid case status into PostgreSQL."""
    query = """
        INSERT INTO case_records (
            receipt_number, 
            current_status, 
            status_description, 
            form_type, 
            submission_filing_date, 
            updated_at
        )
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT (receipt_number) DO UPDATE 
        SET 
            current_status = EXCLUDED.current_status,
            status_description = EXCLUDED.status_description,
            form_type = EXCLUDED.form_type,
            submission_filing_date = EXCLUDED.submission_filing_date,
            updated_at = NOW();
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute(query, (
                record["receipt_number"],
                record["current_status"],
                record["status_description"],
                record["form_type"],
                record["submission_filing_date"]
            ))
        logging.info(f"Successfully saved {record['receipt_number']} -> Status: {record['current_status']}")
    except Exception as db_err:
        logging.error(f"Database write error for {record['receipt_number']}: {db_err}")


def process_receipt_range(receipt_prefix, start_num, end_num):
    """Iterates through receipt range and fetches status safely."""
    auth_token = get_uscis_auth_token()
    if not auth_token:
        logging.critical("Could not acquire initial auth token. Aborting execution.")
        return

    for i in range(start_num, end_num + 1):
        receipt_number = f"{receipt_prefix}{i}"
        logging.info(f"Processing receipt: {receipt_number}")

        # Fetch case status
        case_data = fetch_uscis_case_status(receipt_number, auth_token)

        # STRICT GUARD: Only write to PostgreSQL if valid case_data was returned
        if case_data:
            upsert_case_record(case_data)
        else:
            logging.info(f"Skipping database write for {receipt_number} due to fetch error or missing data.")

        # Sleep with random jitter between 5 and 12 seconds to prevent IP bans
        sleep_duration = random.uniform(5.0, 12.0)
        time.sleep(sleep_duration)


if __name__ == "__main__":
    # Example execution: Scan receipt range WAC2609000178 to WAC2609000195
    PREFIX = "WAC"
    START = 2609001136
    END = 2609002136

    try:
        process_receipt_range(PREFIX, START, END)
    finally:
        # Close DB pool on execution finish
        db_pool.closeall()
        logging.info("Database connection pool closed.")