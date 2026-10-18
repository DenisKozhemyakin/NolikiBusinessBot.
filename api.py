import os
import hmac
import hashlib
import secrets
import time
import re
import base64
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import asyncpg
import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, Header, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field


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
META_CONFIG_ID = os.getenv("META_CONFIG_ID")

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
OAUTH_STATE_LIFETIME = 30 * 60

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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS viewcontrol_sessions (
                token_hash TEXT PRIMARY KEY,
                expires_at TIMESTAMPTZ NOT NULL
            )
        """)
        # Separate customer identities. Existing admin sessions are untouched.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS viewcontrol_users (
                id BIGSERIAL PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS viewcontrol_user_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES viewcontrol_users(id) ON DELETE CASCADE,
                expires_at TIMESTAMPTZ NOT NULL
            )
        """)
        # Each platform connection belongs to one ViewControl user.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS viewcontrol_platform_connections (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES viewcontrol_users(id) ON DELETE CASCADE,
                platform TEXT NOT NULL,
                external_account_id TEXT NOT NULL,
                account_name TEXT,
                encrypted_token TEXT,
                connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, platform, external_account_id)
            )
        """)
        # OAuth state for individual ViewControl users; legacy OAuth stays separate.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS viewcontrol_user_meta_oauth_states (
                state_hash TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES viewcontrol_users(id) ON DELETE CASCADE,
                expires_at TIMESTAMPTZ NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS viewcontrol_oauth_states (
                state_hash TEXT PRIMARY KEY,
                expires_at TIMESTAMPTZ NOT NULL
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


async def check_access(
    api_key: str | None,
    authorization: str | None
):
    if api_key and API_KEY and hmac.compare_digest(api_key, API_KEY):
        return True

    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        if token:
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            async with pool.acquire() as conn:
                valid = await conn.fetchval("""
                    SELECT EXISTS (
                        SELECT 1 FROM viewcontrol_sessions
                        WHERE token_hash = $1 AND expires_at > NOW()
                    )
                """, token_hash)
            if valid:
                return True

    raise HTTPException(status_code=401, detail="Unauthorized")


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

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO viewcontrol_sessions (token_hash, expires_at)
            VALUES ($1, NOW() + ($2::BIGINT * INTERVAL '1 second'))
        """, token_hash, SESSION_LIFETIME)

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": SESSION_LIFETIME
    }


# =====================================
# USER ACCOUNTS — isolated from legacy admin access
# =====================================

class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=256)


def normalize_email(email: str) -> str:
    value = email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise HTTPException(status_code=422, detail="Некоректний email")
    return value


def hash_user_password(password: str, salt: bytes) -> str:
    # Standard-library scrypt: per-user random salt and expensive password hashing.
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return base64.b64encode(derived).decode("ascii")


async def issue_user_session(user_id: int) -> dict:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO viewcontrol_user_sessions (token_hash, user_id, expires_at)
            VALUES ($1, $2, NOW() + ($3::BIGINT * INTERVAL '1 second'))
        """, token_hash, user_id, SESSION_LIFETIME)
    return {"access_token": token, "token_type": "bearer", "expires_in": SESSION_LIFETIME}


async def current_user(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Потрібна авторизація")
    token = authorization[7:].strip()
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT u.id, u.email
            FROM viewcontrol_user_sessions s
            JOIN viewcontrol_users u ON u.id = s.user_id
            WHERE s.token_hash = $1 AND s.expires_at > NOW()
        """, token_hash)
    if row is None:
        raise HTTPException(status_code=401, detail="Сесія недійсна або завершилася")
    return {"id": row["id"], "email": row["email"]}


@app.post("/auth/register", status_code=201)
async def register_user(data: RegisterRequest):
    if not re.search(r"[A-Za-z]", data.password) or not re.search(r"[0-9]", data.password):
        raise HTTPException(status_code=422, detail="Пароль має містити латинські літери та цифри")
    email = normalize_email(data.email)
    salt = secrets.token_bytes(16)
    salt_encoded = base64.b64encode(salt).decode("ascii")
    password_hash = hash_user_password(data.password, salt)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO viewcontrol_users (email, password_salt, password_hash)
            VALUES ($1, $2, $3)
            ON CONFLICT (email) DO NOTHING
            RETURNING id
        """, email, salt_encoded, password_hash)
    if row is None:
        raise HTTPException(status_code=409, detail="Email уже зареєстрований")
    return await issue_user_session(row["id"])


@app.post("/auth/login")
async def login_user(data: RegisterRequest):
    email = normalize_email(data.email)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, password_salt, password_hash
            FROM viewcontrol_users WHERE email = $1
        """, email)
    # Keep error generic: don't reveal whether the email exists.
    if row is None:
        raise HTTPException(status_code=401, detail="Невірний email або пароль")
    salt = base64.b64decode(row["password_salt"])
    expected = hash_user_password(data.password, salt)
    if not hmac.compare_digest(expected, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Невірний email або пароль")
    return await issue_user_session(row["id"])


@app.get("/auth/me")
async def user_profile(authorization: str | None = Header(default=None)):
    return await current_user(authorization)


@app.post("/auth/logout")
async def logout_user(authorization: str | None = Header(default=None)):
    await current_user(authorization)
    token_hash = hashlib.sha256(authorization[7:].strip().encode("utf-8")).hexdigest()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM viewcontrol_user_sessions WHERE token_hash = $1", token_hash)
    return {"ok": True}


# =====================================
# TELEGRAM — ЗАЛИШАЄМО БЕЗ ЗМІН
# =====================================

@app.get("/stats")
async def get_stats(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None)
):
    await check_access(x_api_key, authorization)

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
        META_CONFIG_ID,
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
        META_CONFIG_ID,
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
    await check_access(x_api_key, authorization)
    require_meta_config()

    state = secrets.token_urlsafe(32)

    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO viewcontrol_oauth_states (state_hash, expires_at)
            VALUES ($1, NOW() + ($2::BIGINT * INTERVAL '1 second'))
        """, state_hash, OAUTH_STATE_LIFETIME)

    params = {
        "client_id": META_APP_ID,
        "redirect_uri": META_REDIRECT_URI,
        "state": state,
        "response_type": "code",
        "config_id": META_CONFIG_ID
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

    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    # User-owned OAuth flows are handled independently of the legacy admin flow.
    async with pool.acquire() as conn:
        personal_user_id = await conn.fetchval("""
            DELETE FROM viewcontrol_user_meta_oauth_states
            WHERE state_hash = $1 AND expires_at > NOW()
            RETURNING user_id
        """, state_hash)
    if personal_user_id is not None:
        return await complete_personal_meta_oauth(int(personal_user_id), code, error)

    async with pool.acquire() as conn:
        valid_state = await conn.fetchval("""
            DELETE FROM viewcontrol_oauth_states
            WHERE state_hash = $1 AND expires_at > NOW()
            RETURNING state_hash
        """, state_hash)

    if not valid_state:
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
    await check_access(x_api_key, authorization)

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

# =====================================
# META — FACEBOOK PAGES ТА СТАТИСТИКА
# =====================================

async def get_saved_meta_token() -> str:
    """Отримує збережений токен Meta без передачі його клієнту."""
    require_meta_config()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT encrypted_token, token_expires_at
            FROM meta_connections
            WHERE token_expires_at IS NULL OR token_expires_at > NOW()
            ORDER BY updated_at DESC
            LIMIT 1
        """)
    if not row:
        raise HTTPException(status_code=409, detail="Немає активного підключення Facebook. Підключіть Meta повторно.")
    try:
        return fernet.decrypt(row["encrypted_token"].encode()).decode()
    except Exception:
        raise HTTPException(status_code=503, detail="Не вдалося розшифрувати токен Meta. Перевірте ключ шифрування.")


async def meta_graph_get(client: httpx.AsyncClient, path: str, token: str, **params):
    """Безпечний запит до Graph API: не показує токен або сирі відповіді Meta."""
    params["access_token"] = token
    try:
        response = await client.get(
            f"https://graph.facebook.com/{META_GRAPH_VERSION}/{path.lstrip('/')}",
            params=params,
        )
        data = response.json()
    except (httpx.RequestError, ValueError):
        raise HTTPException(status_code=502, detail="Meta API тимчасово недоступний або повернув некоректну відповідь")
    if response.status_code >= 400 or (isinstance(data, dict) and "error" in data):
        err = data.get("error", {}) if isinstance(data, dict) else {}
        # Не повертаємо message/error_data: вони можуть містити чутливі відомості.
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Meta Graph API відхилив запит",
                "meta_status": response.status_code,
                "meta_error_code": err.get("code"),
                "meta_error_subcode": err.get("error_subcode"),
            },
        )
    return data


async def get_facebook_pages(client: httpx.AsyncClient):
    user_token = await get_saved_meta_token()
    # Meta може повертати список сторінок посторінково.
    pages = []
    path = "me/accounts"
    params = {"fields": "id,name,access_token,fan_count,followers_count", "limit": 100}
    for _ in range(5):
        data = await meta_graph_get(client, path, user_token, **params)
        pages.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
        # Не відкриваємо сторонні URL з відповіді: використовуємо лише cursor.
        cursor = data.get("paging", {}).get("cursors", {}).get("after")
        if not cursor:
            break
        path = "me/accounts"
        params = {"fields": "id,name,access_token,fan_count,followers_count", "limit": 100, "after": cursor}
    return pages


@app.get("/meta/pages")
async def meta_pages(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    await check_access(x_api_key, authorization)
    async with httpx.AsyncClient(timeout=20) as client:
        pages = await get_facebook_pages(client)
    # Ніколи не повертаємо Page access_token у відповідь застосунку.
    return {
        "count": len(pages),
        "pages": [
            {
                "id": page.get("id"),
                "name": page.get("name"),
                "followers_count": page.get("followers_count"),
                "fan_count": page.get("fan_count"),
            }
            for page in pages
        ],
    }


@app.get("/meta/facebook/stats")
async def meta_facebook_stats(
    page_id: str | None = Query(default=None, description="ID Facebook-сторінки з /meta/pages"),
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    await check_access(x_api_key, authorization)
    async with httpx.AsyncClient(timeout=20) as client:
        pages = await get_facebook_pages(client)
        if not pages:
            return {"connected": True, "page_found": False, "message": "Meta не повернула доступних Facebook-сторінок"}
        if page_id is not None:
            page = next((item for item in pages if str(item.get("id")) == page_id), None)
            if page is None:
                raise HTTPException(status_code=404, detail="Сторінку з таким ID не знайдено серед доступних")
        elif len(pages) > 1:
            raise HTTPException(status_code=409, detail="Доступно кілька сторінок. Передайте page_id з /meta/pages.")
        else:
            page = pages[0]
        page_token = page.get("access_token")
        if not page_token:
            raise HTTPException(status_code=502, detail="Meta не надала токен доступу до сторінки")

        # Базові показники сторінки. Не всі метрики Insights доступні
        # кожній сторінці або версії Graph API.
        result = {
            "connected": True,
            "page_found": True,
            "page": {
                "id": page.get("id"),
                "name": page.get("name"),
                "followers_count": page.get("followers_count"),
                "fan_count": page.get("fan_count"),
            },
            "recent_posts": [],
            "insights": {},
            "insights_note": "Охоплення й перегляди потребують окремої перевірки доступних метрик Meta Insights.",
        }
        # Помилка читання публікацій не повинна блокувати базові показники.
        result["posts_status"] = "ok"
        result["posts_error"] = None
        try:
            posts = await meta_graph_get(
                client,
                f"{page['id']}/posts",
                page_token,
                fields="id,message,created_time,permalink_url,reactions.summary(true).limit(0),comments.summary(true).limit(0)",
                limit=10,
            )
        except HTTPException as exc:
            # Не розкриваємо токени чи внутрішні повідомлення Meta.
            result["posts_status"] = "unavailable"
            result["posts_error"] = (
                exc.detail if isinstance(exc.detail, dict)
                else {"message": "Не вдалося отримати публікації Facebook"}
            )
        else:
            for post in posts.get("data", []):
                result["recent_posts"].append({
                    "id": post.get("id"),
                    "message": post.get("message"),
                    "created_time": post.get("created_time"),
                    "permalink_url": post.get("permalink_url"),
                    "reactions_count": post.get("reactions", {}).get("summary", {}).get("total_count"),
                    "comments_count": post.get("comments", {}).get("summary", {}).get("total_count"),
                })
        return result


# =====================================
# META — INSTAGRAM: ПЕРЕВІРКА ВСІХ FACEBOOK PAGES
# =====================================

@app.get("/meta/instagram/check")
async def meta_instagram_check(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    """Перевіряє всі доступні Facebook Pages на зв'язок з Instagram.

    Не повертає токени, сирі помилки Meta або приватні дані авторизації.
    Помилка однієї сторінки не перериває перевірку решти.
    """
    await check_access(x_api_key, authorization)

    async with httpx.AsyncClient(timeout=20) as client:
        pages = await get_facebook_pages(client)
        if not pages:
            return {
                "connected": False,
                "reason": "no_facebook_pages_available",
                "checked_pages": [],
                "instagram_accounts": [],
            }

        checked_pages = []
        instagram_accounts = []
        seen_instagram_ids = set()

        for page in pages:
            page_id = str(page.get("id", ""))
            page_name = page.get("name")
            page_token = page.get("access_token")
            page_result = {
                "facebook_page_id": page_id,
                "facebook_page_name": page_name,
            }

            if not page_token:
                page_result["status"] = "page_token_missing"
                checked_pages.append(page_result)
                continue

            try:
                link = await meta_graph_get(
                    client,
                    page_id,
                    page_token,
                    fields="id,name,instagram_business_account{id,username}",
                )
            except HTTPException as exc:
                page_result["status"] = "instagram_lookup_failed"
                page_result["error"] = (
                    exc.detail if isinstance(exc.detail, dict)
                    else {"message": "Помилка перевірки Instagram"}
                )
                checked_pages.append(page_result)
                continue

            ig = link.get("instagram_business_account") or {}
            ig_id = ig.get("id")
            if not ig_id:
                page_result["status"] = "instagram_account_not_returned"
                checked_pages.append(page_result)
                continue

            page_result["status"] = "instagram_found"
            checked_pages.append(page_result)

            if str(ig_id) in seen_instagram_ids:
                continue
            seen_instagram_ids.add(str(ig_id))

            account = {
                "facebook_page_id": page_id,
                "facebook_page_name": page_name,
                "instagram": {
                    "id": str(ig_id),
                    "username": ig.get("username"),
                    "followers_count": None,
                },
                "followers_status": "unavailable",
            }
            try:
                profile = await meta_graph_get(
                    client,
                    str(ig_id),
                    page_token,
                    fields="id,username,followers_count",
                )
                account["instagram"]["username"] = (
                    profile.get("username") or ig.get("username")
                )
                account["instagram"]["followers_count"] = profile.get(
                    "followers_count"
                )
                if profile.get("followers_count") is not None:
                    account["followers_status"] = "ok"
            except HTTPException as exc:
                account["followers_error"] = (
                    exc.detail if isinstance(exc.detail, dict)
                    else {"message": "Не вдалося отримати кількість підписників"}
                )
            instagram_accounts.append(account)

        result = {
            "connected": bool(instagram_accounts),
            "checked_pages": checked_pages,
            "instagram_accounts": instagram_accounts,
        }
        if not instagram_accounts:
            result["reason"] = "instagram_account_not_returned_on_accessible_pages"
        return result

# =====================================
# META — INSTAGRAM: ПРЯМА ПЕРЕВІРКА
# =====================================

# Instagram ID підтверджений через Graph API Explorer.
# Не залежить від того, чи повертає /me/accounts пов'язану Facebook Page.
INSTAGRAM_ACCOUNT_ID = os.getenv("META_INSTAGRAM_ACCOUNT_ID", "17841402272405774")


@app.get("/meta/instagram/stats")
async def meta_instagram_stats(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    """Публічні показники IG і добове охоплення; токени не видаються клієнту."""
    await check_access(x_api_key, authorization)
    user_token = await get_saved_meta_token()

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            profile = await meta_graph_get(
                client,
                INSTAGRAM_ACCOUNT_ID,
                user_token,
                fields="id,username,name,followers_count,media_count",
            )
        except HTTPException as exc:
            return {
                "connected": False,
                "instagram_id": INSTAGRAM_ACCOUNT_ID,
                "reason": "instagram_profile_unavailable",
                "error": exc.detail if isinstance(exc.detail, dict)
                         else {"message": "Не вдалося отримати профіль Instagram"},
            }

        result = {
            "connected": True,
            "instagram": {
                "id": profile.get("id"),
                "username": profile.get("username"),
                "name": profile.get("name"),
                "followers_count": profile.get("followers_count"),
                "media_count": profile.get("media_count"),
            },
            "reach": [],
            "reach_status": "unavailable",
        }
        try:
            insights = await meta_graph_get(
                client,
                f"{INSTAGRAM_ACCOUNT_ID}/insights",
                user_token,
                metric="reach",
                period="day",
            )
            for metric in insights.get("data", []):
                if metric.get("name") == "reach":
                    result["reach"] = [
                        {"value": point.get("value"), "end_time": point.get("end_time")}
                        for point in metric.get("values", [])
                    ]
                    result["reach_status"] = "ok"
                    break
        except HTTPException as exc:
            result["reach_error"] = (
                exc.detail if isinstance(exc.detail, dict)
                else {"message": "Не вдалося отримати охоплення Instagram"}
            )
        return result



# =====================================
# VIEWCONTROL — ПЕРСОНАЛЬНІ ПЛАТФОРМИ
# =====================================

@app.get("/platforms/my")
async def my_platforms(authorization: str | None = Header(default=None)):
    user = await current_user(authorization)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, platform, external_account_id, account_name, connected_at, updated_at
            FROM viewcontrol_platform_connections
            WHERE user_id = $1
            ORDER BY connected_at DESC, id DESC
        """, user["id"])
    return {
        "platforms": [
            {
                "id": row["id"],
                "platform": row["platform"],
                "external_account_id": row["external_account_id"],
                "account_name": row["account_name"],
                "connected_at": row["connected_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat(),
            }
            for row in rows
        ]
    }


# =====================================
# VIEWCONTROL — ПЕРСОНАЛЬНИЙ META OAUTH
# Existing /meta/callback receives the Meta redirect and dispatches
# personal OAuth state to complete_personal_meta_oauth.
# =====================================

@app.get("/user/meta/connect")
async def user_meta_connect(authorization: str | None = Header(default=None)):
    user = await current_user(authorization)
    require_meta_config()
    state = secrets.token_urlsafe(32)
    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO viewcontrol_user_meta_oauth_states (state_hash, user_id, expires_at)
            VALUES ($1, $2, NOW() + ($3::BIGINT * INTERVAL '1 second'))
        """, state_hash, user["id"], OAUTH_STATE_LIFETIME)
    auth_url = "https://www.facebook.com/" + META_GRAPH_VERSION + "/dialog/oauth?" + urlencode({
        "client_id": META_APP_ID,
        "redirect_uri": META_REDIRECT_URI,
        "state": state,
        "response_type": "code",
        "config_id": META_CONFIG_ID,
    })
    return {"authorization_url": auth_url, "expires_in": OAUTH_STATE_LIFETIME}


async def complete_personal_meta_oauth(user_id: int, code: str | None, error: str | None):
    if error:
        return HTMLResponse("<h2>Авторизацію Meta скасовано.</h2>", status_code=400)
    if not code:
        raise HTTPException(status_code=400, detail="Meta не повернула код")
    require_meta_config()

    async with httpx.AsyncClient(timeout=25) as client:
        short = await client.get(
            f"https://graph.facebook.com/{META_GRAPH_VERSION}/oauth/access_token",
            params={"client_id": META_APP_ID, "client_secret": META_APP_SECRET,
                    "redirect_uri": META_REDIRECT_URI, "code": code},
        )
        if short.status_code != 200 or not short.json().get("access_token"):
            raise HTTPException(status_code=502, detail="Meta не видала токен авторизації")
        exchanged = await client.get(
            f"https://graph.facebook.com/{META_GRAPH_VERSION}/oauth/access_token",
            params={"grant_type": "fb_exchange_token", "client_id": META_APP_ID,
                    "client_secret": META_APP_SECRET,
                    "fb_exchange_token": short.json()["access_token"]},
        )
        if exchanged.status_code != 200 or not exchanged.json().get("access_token"):
            raise HTTPException(status_code=502, detail="Meta не видала довгостроковий токен")
        user_token = exchanged.json()["access_token"]

        # Read only Facebook Pages actually granted to this Meta user.
        pages = []
        url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/me/accounts"
        params = {"fields": "id,name,access_token,instagram_business_account{id,username}",
                  "limit": 100, "access_token": user_token}
        for _ in range(10):
            response = await client.get(url, params=params)
            if response.status_code != 200:
                raise HTTPException(status_code=502, detail="Не вдалося отримати сторінки Facebook")
            payload = response.json()
            pages.extend(payload.get("data", []))
            next_url = payload.get("paging", {}).get("next")
            if not next_url:
                break
            url, params = next_url, None

    connections = []
    for page in pages:
        page_id = page.get("id")
        page_token = page.get("access_token")
        if not page_id or not page_token:
            continue
        connections.append(("facebook", str(page_id), page.get("name"),
                            fernet.encrypt(page_token.encode()).decode()))
        ig = page.get("instagram_business_account") or {}
        if ig.get("id"):
            connections.append(("instagram", str(ig["id"]), ig.get("username"),
                                fernet.encrypt(page_token.encode()).decode()))

    # Only the user identified by the single-use OAuth state owns these rows.
    async with pool.acquire() as conn:
        async with conn.transaction():
            for platform, external_id, account_name, encrypted in connections:
                await conn.execute("""
                    INSERT INTO viewcontrol_platform_connections
                        (user_id, platform, external_account_id, account_name, encrypted_token)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (user_id, platform, external_account_id)
                    DO UPDATE SET account_name = EXCLUDED.account_name,
                                  encrypted_token = EXCLUDED.encrypted_token,
                                  updated_at = NOW()
                """, user_id, platform, external_id, account_name, encrypted)

    return HTMLResponse("""<!doctype html><html lang="uk"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"></head>
    <body style="background:#0B1020;color:white;font-family:Arial;text-align:center;padding:55px 15px">
    <h2>Підключення Meta завершено</h2>
    <p>Повернись до ViewControl і онови список платформ.</p>
    <p>Якщо сторінки не з'явилися, перевір дозволи Meta для сторінок.</p>
    </body></html>""")


@app.get("/user/meta/status")
async def user_meta_status(authorization: str | None = Header(default=None)):
    user = await current_user(authorization)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT platform, external_account_id, account_name
            FROM viewcontrol_platform_connections
            WHERE user_id = $1 AND platform IN ('facebook', 'instagram')
            ORDER BY platform, account_name
        """, user["id"])
    return {"accounts": [dict(row) for row in rows]}
