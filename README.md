# USCIS Case Analytics & Scraper Engine

A scalable, containerized pipeline to scrape, parse, and analyze USCIS I-485 case data. Designed for local execution via Docker and parallelized cloud scaling via AWS ECS Fargate, SQS, and RDS PostgreSQL.

---

## Architecture Overview

* **Scraper & Parser (`uscis.py`, `scraper.py`)**: Asynchronous data retrieval and parsing of USCIS status updates into structured records.
* **Analytics Engine (`derive_analysis.py`)**: Processes raw case data to compute FIFO metrics, RFE indicators, card production flags, and relative queue placement.
* **UI & API Server (`ui_server.py`)**: FastAPI server providing web access and CSV data exports (`/api/export/analysis-csv`).
* **Database**: PostgreSQL storing raw (`case_records`) and derived analysis (`gc_analysis`) tables.

---

## Local Development (Docker)

### 1. Start Services
Bring up the PostgreSQL database and UI container:
```bash
docker-compose up -d

2. Run Analytics Engine
Derive metrics from processed case records:
docker cp derive_analysis.py uscis_ui:/app/derive_analysis.py
docker exec -it uscis_ui python derive_analysis.py

3. Export CSV Data
Fetch the generated analytics dataset:
curl -O http://localhost:8022/api/export/analysis-csv

AWS Cloud Deployment & Scaling
For large-scale data ingestion, deploy worker nodes to AWS ECS Fargate backed by an AWS RDS PostgreSQL instance and an AWS SQS queue.

1. Build and Push Image via AWS CloudShell
2. Open AWS CloudShell in the AWS Console.

Clone this repository:
git clone <YOUR_GITHUB_REPO_URL>
cd <REPO_NAME>

3. Authenticate Docker and push to Amazon ECR:
# Create ECR repository (if needed)
aws ecr create-repository --repository-name uscis-scraper --region us-east-1

# Authenticate & Build
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
docker build -t uscis-scraper .
docker tag uscis-scraper:latest <AWS_ACCOUNT_ID>[.dkr.ecr.us-east-1.amazonaws.com/uscis-scraper:latest](https://.dkr.ecr.us-east-1.amazonaws.com/uscis-scraper:latest)
docker push <AWS_ACCOUNT_ID>[.dkr.ecr.us-east-1.amazonaws.com/uscis-scraper:latest](https://.dkr.ecr.us-east-1.amazonaws.com/uscis-scraper:latest)

2. Database & Work Queue Setup
AWS RDS PostgreSQL: Provision an RDS instance and set database environment variables (DB_HOST, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD).

AWS SQS Queue: Create an SQS queue containing batches of receipt ranges to be processed by worker tasks.

3. Launch ECS Fargate Tasks
Spin up parallel ECS Task instances. Each container reads tasks off SQS, parses records asynchronously, and performs idempotent upserts into the RDS database via ON CONFLICT (receipt_number) DO UPDATE.
---

### Push README to GitHub

Add and push the file from your local Mac terminal:

```bash
git add README.md
git commit -m "Add README with local and AWS CloudShell setup instructions"
git push origin main
