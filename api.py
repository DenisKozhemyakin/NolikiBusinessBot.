import os
import hmac
import hashlib
import secrets
import time
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel


DATABASE_URL = os.getenv("DATABASE_URL")
API_KEY = os.getenv("VIEWCONTROL_API_KEY")

# Пароль зберігаємо тільки на сервері у вигляді хешу
PASSWORD_HASH = os.getenv("VIEWCONTROL_PASSWORD_HASH")

# Токени зберігаються в пам'яті сервера
sessions = {}

SESSION_LIFETIME = 24 * 60 * 60

pool = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool

    if not DATABASE_URL or not API_KEY:
        raise RuntimeError(
            "DATABASE_URL або VIEWCONTROL_API_KEY не налаштовано"
        )

    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=3
    )

    try:
        yield
    finally:
        await pool.close()


app = FastAPI(
    title="ViewControl API",
    lifespan=lifespan
)


class LoginRequest(BaseModel):
    password: str


def check_password(password: str) -> bool:
    if not PASSWORD_HASH:
        return False

    entered_hash = hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()

    return hmac.compare_digest(
        entered_hash,
        PASSWORD_HASH
    )


def check_access(
    api_key: str | None,
    authorization: str | None
):
    # Залишаємо підтримку старого API-ключа
    if api_key and API_KEY and hmac.compare_digest(
        api_key, API_KEY
    ):
        return True

    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        expires = sessions.get(token)

        if expires and expires > time.time():
            return True

    raise HTTPException(
        status_code=401,
        detail="Unauthorized"
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/login")
async def login(data: LoginRequest):
    if not check_password(data.password):
        raise HTTPException(
            status_code=401,
            detail="Неправильний пароль"
        )

    token = secrets.token_urlsafe(32)

    sessions[token] = time.time() + SESSION_LIFETIME

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": SESSION_LIFETIME
    }


@app.get("/stats")
async def get_stats(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None)
):
    check_access(x_api_key, authorization)

    async with pool.acquire() as conn:
        users = await conn.fetchval(
            "SELECT COUNT(*) FROM bot_users"
        )

        events = await conn.fetch("""
            SELECT event_type, COUNT(*) AS total
            FROM bot_events
            GROUP BY event_type
        """)

    counts = {
        row["event_type"]: row["total"]
        for row in events
    }

    return {
        "users": users,
        "orders_started": counts.get("order_started", 0),
        "orders_completed": counts.get("order_completed", 0),
        "orders_cancelled": counts.get("order_cancelled", 0),
        "contacts_opened": counts.get("contacts_opened", 0)
    }
