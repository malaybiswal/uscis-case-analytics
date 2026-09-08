import os
import math
import csv
import io
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, StreamingResponse
import asyncpg

app = FastAPI(title="USCIS Tracker Dashboard")

# Database connection configuration
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "postgres"),
    "database": os.getenv("POSTGRES_DB", "uscis_db"),
    "user": os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD", "postgres"),
    "port": int(os.getenv("DB_PORT", 5432)),
}

pool = None

# Strip all non-digit characters and cast the remainder to BIGINT for numeric sorting
ALLOWED_SORT_COLUMNS = {
    "receipt_number": "NULLIF(regexp_replace(receipt_number, '\\D', '', 'g'), '')::BIGINT",
    "filed_date": "filed_date",
    "updated_at": "updated_at",
    "status_date": "status_date"
}

@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(**DB_CONFIG)

@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()

@app.get("/api/cases")
async def get_cases(
    table: str = Query("case_records", enum=["case_records", "other_case_records"]),
    page: int = Query(1, ge=1),
    limit: int = Query(50, enum=[50, 100, 150, 200]),
    form_type: str = Query(None),
    status: str = Query(None),
    filed_date: str = Query(None),
    search: str = Query(None),
    sort_by: str = Query("updated_at"),
    order: str = Query("desc", enum=["asc", "desc"])
):
    offset = (page - 1) * limit
    where_clauses = []
    params = []

    # Dynamic Filtering
    if form_type:
        params.append(form_type)
        where_clauses.append(f"form_type = ${len(params)}")
    
    if status:
        params.append(f"%{status}%")
        where_clauses.append(f"current_status ILIKE ${len(params)}")

    if filed_date:
        params.append(f"%{filed_date}%")
        where_clauses.append(f"filed_date ILIKE ${len(params)}")

    if search:
        params.append(f"%{search}%")
        where_clauses.append(f"(receipt_number ILIKE ${len(params)} OR status_description ILIKE ${len(params)})")

    where_str = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Validate sorting input against column whitelist
    sort_column = ALLOWED_SORT_COLUMNS.get(sort_by, "updated_at")
    sort_direction = "ASC" if order.lower() == "asc" else "DESC"

    async with pool.acquire() as conn:
        # Get total count for pagination math
        count_query = f"SELECT COUNT(*) FROM {table}{where_str}"
        total_records = await conn.fetchval(count_query, *params)

        # Get paginated data dynamically sorted
        data_query = f"""
            SELECT receipt_number, form_type, current_status, status_date, 
                   filed_date, processing_center, playbook_headline, updated_at
            FROM {table}{where_str}
            ORDER BY {sort_column} {sort_direction} NULLS LAST
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        rows = await conn.fetch(data_query, *params, limit, offset)

    # Get distinct options for filter dropdowns
    async with pool.acquire() as conn:
        form_types = await conn.fetch(f"SELECT DISTINCT form_type FROM {table} WHERE form_type IS NOT NULL")

    return {
        "data": [dict(r) for r in rows],
        "total": total_records,
        "page": page,
        "limit": limit,
        "total_pages": math.ceil(total_records / limit) if total_records else 1,
        "filter_options": {
            "form_types": [f["form_type"] for f in form_types]
        }
    }

@app.get("/api/export/analysis-csv")
async def export_analysis_csv():
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 
                r.receipt_number,
                a.receipt_sequence,
                r.form_type,
                r.current_status,
                a.filed_date,
                a.status_date,
                a.is_final,
                a.approval_or_card_date,
                a.days_pending_or_to_final,
                a.filed_after_malay,
                a.receipt_after_malay,
                a.has_rfe,
                a.rfe_date,
                a.rfe_response_date,
                r.updated_at AS scraped_at,
                r.status_history
            FROM case_records r
            JOIN gc_analysis a ON r.receipt_number = a.receipt_number
            WHERE REPLACE(LOWER(r.form_type), '-', '') LIKE '%i485%'
               OR r.form_type IS NULL
            ORDER BY a.receipt_sequence ASC
        """)

    output = io.StringIO()
    writer = csv.writer(output)
    
    if rows:
        writer.writerow(rows[0].keys())
        for row in rows:
            writer.writerow(row.values())

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]), 
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=uscis_i485_fifo_analysis.csv"}
    )

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>USCIS Case Status Dashboard</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
    </head>
    <body class="bg-slate-900 text-slate-100 p-6" x-data="dashboard()">
        <div class="max-w-7xl mx-auto space-y-6">
            
            <!-- Header -->
            <div class="flex justify-between items-center bg-slate-800 p-4 rounded-xl border border-slate-700">
                <div class="flex items-center gap-4">
                    <h1 class="text-2xl font-bold text-blue-400">USCIS Tracker Dashboard</h1>
                    <a href="/api/export/analysis-csv" target="_blank" class="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded text-xs font-semibold flex items-center gap-1.5 transition">
                        <span>↓ Export FIFO CSV</span>
                    </a>
                </div>
                <div class="text-sm text-slate-400">Total Cases Found: <span class="text-emerald-400 font-mono text-base" x-text="total"></span></div>
            </div>

            <!-- Filters -->
            <div class="bg-slate-800 p-4 rounded-xl border border-slate-700 grid grid-cols-1 md:grid-cols-5 gap-3">
                <div>
                    <label class="block text-xs text-slate-400 mb-1">Target Table</label>
                    <select x-model="table" @change="page=1; loadData()" class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-sm">
                        <option value="case_records">I-485 Records</option>
                        <option value="other_case_records">Other Form Records</option>
                    </select>
                </div>

                <div>
                    <label class="block text-xs text-slate-400 mb-1">Form Type</label>
                    <select x-model="formType" @change="page=1; loadData()" class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-sm">
                        <option value="">All Form Types</option>
                        <template x-for="f in formTypes">
                            <option :value="f" x-text="f"></option>
                        </template>
                    </select>
                </div>

                <div>
                    <label class="block text-xs text-slate-400 mb-1">Status Keyword</label>
                    <input type="text" x-model.debounce.400ms="status" @input="page=1; loadData()" placeholder="e.g. Approved, Received" class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-sm">
                </div>

                <div>
                    <label class="block text-xs text-slate-400 mb-1">Search Receipt/Text</label>
                    <input type="text" x-model.debounce.400ms="search" @input="page=1; loadData()" placeholder="WAC26..." class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-sm">
                </div>

                <div>
                    <label class="block text-xs text-slate-400 mb-1">Page Size</label>
                    <select x-model.number="limit" @change="page=1; loadData()" class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-sm">
                        <option value="50">50 per page</option>
                        <option value="100">100 per page</option>
                        <option value="150">150 per page</option>
                        <option value="200">200 per page</option>
                    </select>
                </div>
            </div>

            <!-- Data Table -->
            <div class="bg-slate-800 rounded-xl border border-slate-700 overflow-x-auto">
                <table class="w-full text-left text-sm">
                    <thead class="bg-slate-900/60 text-slate-400 uppercase text-xs border-b border-slate-700 select-none">
                        <tr>
                            <!-- Clickable Receipt Number Header -->
                            <th @click="toggleSort('receipt_number')" class="p-3 cursor-pointer hover:text-white transition">
                                <div class="flex items-center gap-1">
                                    <span>Receipt Number</span>
                                    <span x-show="sortBy === 'receipt_number'" x-text="order === 'asc' ? '▲' : '▼'" class="text-blue-400"></span>
                                </div>
                            </th>
                            <th class="p-3">Form</th>
                            <th class="p-3">Current Status</th>
                            <!-- Clickable Filed Date Header -->
                            <th @click="toggleSort('filed_date')" class="p-3 cursor-pointer hover:text-white transition">
                                <div class="flex items-center gap-1">
                                    <span>Filed Date</span>
                                    <span x-show="sortBy === 'filed_date'" x-text="order === 'asc' ? '▲' : '▼'" class="text-blue-400"></span>
                                </div>
                            </th>
                            <th class="p-3">Status Date</th>
                            <th class="p-3">Processing Center</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-700/50">
                        <template x-for="row in cases" :key="row.receipt_number">
                            <tr class="hover:bg-slate-700/30 transition">
                                <td class="p-3 font-mono font-medium text-blue-300" x-text="row.receipt_number"></td>
                                <td class="p-3"><span class="bg-slate-700 px-2 py-0.5 rounded text-xs" x-text="row.form_type"></span></td>
                                <td class="p-3 font-medium text-emerald-400" x-text="row.current_status"></td>
                                <td class="p-3 text-slate-400" x-text="row.filed_date || 'N/A'"></td>
                                <td class="p-3 text-slate-400" x-text="row.status_date || 'N/A'"></td>
                                <td class="p-3 text-slate-400" x-text="row.processing_center || 'N/A'"></td>
                            </tr>
                        </template>
                    </tbody>
                </table>
            </div>

            <!-- Pagination Controls -->
            <div class="flex justify-between items-center bg-slate-800 p-4 rounded-xl border border-slate-700">
                <div class="text-sm text-slate-400">
                    Page <span x-text="page"></span> of <span x-text="totalPages"></span>
                </div>
                <div class="flex gap-2">
                    <button @click="page--; loadData()" :disabled="page <= 1" class="px-3 py-1.5 bg-slate-700 disabled:opacity-40 rounded text-sm hover:bg-slate-600">Previous</button>
                    <button @click="page++; loadData()" :disabled="page >= totalPages" class="px-3 py-1.5 bg-blue-600 disabled:opacity-40 rounded text-sm hover:bg-blue-500">Next</button>
                </div>
            </div>

        </div>

        <script>
            function dashboard() {
                return {
                    table: 'case_records',
                    cases: [],
                    total: 0,
                    page: 1,
                    limit: 50,
                    totalPages: 1,
                    formType: '',
                    status: '',
                    search: '',
                    sortBy: 'updated_at',
                    order: 'desc',
                    formTypes: [],
                    toggleSort(column) {
                        if (this.sortBy === column) {
                            this.order = this.order === 'asc' ? 'desc' : 'asc';
                        } else {
                            this.sortBy = column;
                            this.order = 'asc';
                        }
                        this.page = 1;
                        this.loadData();
                    },
                    async loadData() {
                        const query = new URLSearchParams({
                            table: this.table,
                            page: this.page,
                            limit: this.limit,
                            sort_by: this.sortBy,
                            order: this.order,
                            ...(this.formType && { form_type: this.formType }),
                            ...(this.status && { status: this.status }),
                            ...(this.search && { search: this.search })
                        });
                        const res = await fetch('/api/cases?' + query);
                        const json = await res.json();
                        this.cases = json.data;
                        this.total = json.total;
                        this.totalPages = json.total_pages;
                        this.formTypes = json.filter_options.form_types;
                    },
                    init() { this.loadData(); }
                }
            }
        </script>
    </body>
    </html>
    """