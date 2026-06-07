addon = """

# ── Scan Jobs — background scanning ───────────────────────────────────────────

def create_scan_job(params: dict) -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO scan_jobs (created_at, status, params) VALUES (?, 'pending', ?)",
            (datetime.now().isoformat(), json.dumps(params))
        )
        return cursor.lastrowid


def get_scan_job(job_id: int) -> Optional[Dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM scan_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def get_latest_scan_job() -> Optional[Dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM scan_jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def update_scan_job(job_id: int, **kwargs):
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [job_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE scan_jobs SET {fields} WHERE id = ?", values)
"""

with open("src/database.py", "a", encoding="utf-8") as f:
    f.write(addon)
print("done")
