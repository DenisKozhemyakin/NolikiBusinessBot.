import os
import hmac
import hashlib
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import asyncpg
import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel


# =====================================
# НАЛАШТУВАННЯ
# =====================================

DATABASE_URL = os.getenv("DATABASE_URL")
API_KEY = os.getenv("VIEWCONTROL_API_KEY")
PASSWORD_HASH = os.getenv("VIEWCONTROL_PASSWORD_HASH")

META_APP_ID = os.getenv("META_APP_ID")
META_APP_SECRET = os.getenv("META_APP_SECRET")
META_REDIRECT_URI = os.getenv("META_REDIRECT_URI")
META_ENCRYPTION_KEY = os.getenv("META_ENCRYPTION_KEY")

META_GRAPH_VERSION = "v24.0"

META_SCOPES = [
    "public_profile",
    "pages_show_list",
    "pages_read_engagement",
    "read_insights",
    "instagram_basic",
    "instagram_manage_insights",
]

SESSION_LIFETIME = 24 * 60 * 60
OAUTH_STATE_LIFETIME = 10 * 60

sessions = {}
oauth_states = {}

pool = None
fernet = None


# =====================================
# ЗАПУСК СЕРВЕРА
# =====================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, fernet

    if not DATABASE_URL or not API_KEY:
        raise RuntimeError(
            "DATABASE_URL або VIEWCONTROL_API_KEY не налаштовано"
        )

    if META_ENCRYPTION_KEY:
        fernet = Fernet(META_ENCRYPTION_KEY.encode())

    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=3
    )

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS meta_connections (
                id SERIAL PRIMARY KEY,
                meta_user_id TEXT NOT NULL UNIQUE,
                encrypted_token TEXT NOT NULL,
                token_expires_at TIMESTAMPTZ,
                connected_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

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


# =====================================
# АВТОРИЗАЦІЯ VIEWCONTROL
# =====================================

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


# =====================================
# TELEGRAM — ЗАЛИШАЄМО БЕЗ ЗМІН
# =====================================

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


# =====================================
# META — ДОПОМІЖНІ ФУНКЦІЇ
# =====================================

def require_meta_config():
    if not all([
        META_APP_ID,
        META_APP_SECRET,
        META_REDIRECT_URI,
        fernet
    ]):
        raise HTTPException(
            status_code=503,
            detail="Meta API не налаштовано"
        )


def get_meta_connection_status():
    return "configured" if all([
        META_APP_ID,
        META_APP_SECRET,
        META_REDIRECT_URI,
        fernet
    ]) else "not_configured"


# =====================================
# META — ПОЧАТОК АВТОРИЗАЦІЇ
# =====================================

@app.get("/meta/connect")
async def meta_connect(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None)
):
    check_access(x_api_key, authorization)
    require_meta_config()

    state = secrets.token_urlsafe(32)

    oauth_states[state] = time.time() + OAUTH_STATE_LIFETIME

    params = {
        "client_id": META_APP_ID,
        "redirect_uri": META_REDIRECT_URI,
        "state": state,
        "scope": ",".join(META_SCOPES),
        "response_type": "code"
    }

    auth_url = (
        f"https://www.facebook.com/{META_GRAPH_VERSION}/dialog/oauth?"
        + urlencode(params)
    )

    return {
        "authorization_url": auth_url,
        "expires_in": OAUTH_STATE_LIFETIME
    }


# =====================================
# META — CALLBACK
# =====================================

@app.get("/meta/callback")
async def meta_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None
):
    require_meta_config()

    if not state:
        raise HTTPException(
            status_code=400,
            detail="Відсутній OAuth state"
        )

    expires = oauth_states.pop(state, None)

    if not expires or expires < time.time():
        raise HTTPException(
            status_code=400,
            detail="Недійсний або прострочений OAuth state"
        )

    if error:
        return HTMLResponse(
            "<h2>Авторизацію Meta скасовано.</h2>",
            status_code=400
        )

    if not code:
        raise HTTPException(
            status_code=400,
            detail="Meta не повернула код авторизації"
        )

    async with httpx.AsyncClient(timeout=20) as client:

        token_response = await client.get(
            f"https://graph.facebook.com/{META_GRAPH_VERSION}/oauth/access_token",
            params={
                "client_id": META_APP_ID,
                "client_secret": META_APP_SECRET,
                "redirect_uri": META_REDIRECT_URI,
                "code": code
            }
        )

        if token_response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail="Не вдалося отримати токен Meta"
            )

        token_data = token_response.json()
        short_token = token_data.get("access_token")

        if not short_token:
            raise HTTPException(
                status_code=502,
                detail="Meta не повернула токен"
            )

        long_response = await client.get(
            f"https://graph.facebook.com/{META_GRAPH_VERSION}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": META_APP_ID,
                "client_secret": META_APP_SECRET,
                "fb_exchange_token": short_token
            }
        )

        if long_response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail="Не вдалося отримати довгостроковий токен Meta"
            )

        long_data = long_response.json()
        access_token = long_data.get("access_token")

        if not access_token:
            raise HTTPException(
                status_code=502,
                detail="Довгостроковий токен відсутній"
            )

        profile_response = await client.get(
            f"https://graph.facebook.com/{META_GRAPH_VERSION}/me",
            params={
                "fields": "id",
                "access_token": access_token
            }
        )

        if profile_response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail="Не вдалося отримати профіль Meta"
            )

        meta_user_id = profile_response.json().get("id")

        if not meta_user_id:
            raise HTTPException(
                status_code=502,
                detail="Meta не повернула ID користувача"
            )

    encrypted_token = fernet.encrypt(
        access_token.encode()
    ).decode()

    expires_in = long_data.get("expires_in")

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO meta_connections (
                meta_user_id,
                encrypted_token,
                token_expires_at
            )
            VALUES (
                $1,
                $2,
                CASE
                    WHEN $3::BIGINT IS NULL THEN NULL
                    ELSE NOW() + ($3::BIGINT * INTERVAL '1 second')
                END
            )
            ON CONFLICT (meta_user_id)
            DO UPDATE SET
                encrypted_token = EXCLUDED.encrypted_token,
                token_expires_at = EXCLUDED.token_expires_at,
                updated_at = NOW()
        """, meta_user_id, encrypted_token, expires_in)

    return HTMLResponse("""
        <!DOCTYPE html>
        <html lang="uk">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport"
                  content="width=device-width, initial-scale=1">
            <title>ViewControl</title>
        </head>
        <body style="
            background:#0B1020;
            color:white;
            font-family:Arial;
            text-align:center;
            padding:60px 20px;
        ">
            <h1>✅ Meta підключено!</h1>
            <p>Авторизацію Facebook успішно завершено.</p>
            <p>Тепер можна повернутися до ViewControl.</p>
        </body>
        </html>
    """)


# =====================================
# META — СТАТУС ПІДКЛЮЧЕННЯ
# =====================================

@app.get("/meta/status")
async def meta_status(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None)
):
    check_access(x_api_key, authorization)

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                meta_user_id,
                connected_at,
                token_expires_at
            FROM meta_connections
            ORDER BY connected_at DESC
        """)

    return {
        "configuration": get_meta_connection_status(),
        "connected": len(rows) > 0,
        "accounts": [
            {
                "meta_user_id": row["meta_user_id"],
                "connected_at": row["connected_at"].isoformat(),
                "token_expires_at": (
                    row["token_expires_at"].isoformat()
                    if row["token_expires_at"]
                    else None
                )
            }
            for row in rows
        ]
    }
