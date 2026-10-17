import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Header, HTTPException

DATABASE_URL = os.getenv("DATABASE_URL")
API_KEY = os.getenv("VIEWCONTROL_API_KEY")

pool = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool

    if not DATABASE_URL or not API_KEY:
        raise RuntimeError("DATABASE_URL або VIEWCONTROL_API_KEY не налаштовано")

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)

    try:
        yield
    finally:
        await pool.close()


app = FastAPI(title="ViewControl API", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/stats")
async def get_stats(x_api_key: str | None = Header(default=None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    async with pool.acquire() as conn:
        users = await conn.fetchval(
            "SELECT COUNT(*) FROM bot_users"
        )

        events = await conn.fetch("""
            SELECT event_type, COUNT(*) AS total
            FROM bot_events
            GROUP BY event_type
        """)

    counts = {row["event_type"]: row["total"] for row in events}

    return {
        "users": users,
        "orders_started": counts.get("order_started", 0),
        "orders_completed": counts.get("order_completed", 0),
        "orders_cancelled": counts.get("order_cancelled", 0),
        "contacts_opened": counts.get("contacts_opened", 0),
    }
