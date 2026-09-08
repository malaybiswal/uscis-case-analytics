-- Table for target I-485 cases
CREATE TABLE IF NOT EXISTS case_records (
    receipt_number VARCHAR(20) PRIMARY KEY,
    form_type VARCHAR(20),
    submission_filing_date DATE,
    current_status VARCHAR(100),
    current_status_date DATE,
    full_status_history JSONB,
    rfe_date DATE,
    rfe_response_date DATE,
    transfer_date DATE,
    approval_date DATE,
    card_produced_date DATE,
    scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Table for all non-I-485 cases
CREATE TABLE IF NOT EXISTS other_case_records (
    receipt_number VARCHAR(20) PRIMARY KEY,
    form_type VARCHAR(20),
    submission_filing_date DATE,
    current_status VARCHAR(100),
    current_status_date DATE,
    full_status_history JSONB,
    rfe_date DATE,
    rfe_response_date DATE,
    transfer_date DATE,
    approval_date DATE,
    card_produced_date DATE,
    scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);