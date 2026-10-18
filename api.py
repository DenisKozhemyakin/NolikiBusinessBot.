сьогодні 14:10
Шо з тобою на компʼютері стало
\Вставлений text(3).txt
Документ
\Вставлений text(4).txt
Документ
\Вставлений text(5).txt
Документ
Вставлено код(1).ts
TypeScript
\Вставлений text(6).txt
Документ
Вставлено код(2).ts
TypeScript
\Вставлений text(7).txt
Документ

Зрозумів! 👍

Отже, ти щойно сам прив'язав Instagram до Facebook-сторінки «Рисуем на заказ/Портреты, Картины».

Раніше наш сервер шукав Instagram через сторінку «Танюшка Чернявская», тому й отримував:

instagram_account_not_returned

Тепер перевіримо, чи бачить Meta нове підключення.

Крок 134.9 — повторна перевірка
Повернися до Swagger у браузері.
Відкрий GET /meta/instagram/check.
Натисни Execute (API-ключ, якщо залишився в полі, повторно вводити не потрібно).
Скопіюй Response body.

Надішли результат сюди.

Якщо перевірка знову покаже instagram_account_not_returned, з'ясуємо, чи запит перевіряє лише сторінку «Танюшка Чернявская». У такому разі змінимо його, щоб він перевіряв обидві Facebook-сторінки.

Поки нічого більше не змінюй.

Бачу результат. Тепер зрозуміло, чому перевірка не спрацювала.

Сервер повертає:

{
  "connected": false,
  "reason": "instagram_account_not_returned",
  "facebook_page_id": "113368974934371"
}

Проблема в тому, що сервер досі перевіряє стару Facebook-сторінку:

113368974934371 — «Танюшка Чернявская».
467094246750917 — «Рисуем на заказ/Портреты, Картины», яку ти щойно вибрав в Instagram.
Крок 135 — виправляємо перевірку Instagram

Зробимо правильніше: ViewControl автоматично перевірятиме обидві Facebook-сторінки, а не одну конкретну.

Так ми зможемо визначити, до якої сторінки прив'язаний Instagram, і отримати його дані.

Зараз зроби одну дію:

Надішли мені актуальний файл api.py із GitHub після останнього оновлення.

Я підготую готовий файл для заміни, щоб не зачепити вже працюючі Facebook і Telegram.

Після цього повторимо перевірку Instagram.

\Вставлений text(8).txt
Документ

Отримав актуальний api.py. Знайшов причину: перевірка Instagram була прив'язана лише до Facebook-сторінки «Танюшка Чернявская» (113368974934371).

✅ Крок 135 — виправляємо перевірку Instagram

Я підготував новий файл, у якому ViewControl:

Перевіряє обидві Facebook-сторінки автоматично.
Шукає прив'язаний Instagram-акаунт.
Отримує його ім'я та кількість підписників, якщо Meta дозволяє.
Не припиняє перевірку, якщо одна зі сторінок повертає помилку.
Зберігає наявні функції Facebook і Telegram.

📥 
Завантажити оновлений api.py

Файл перевірено на синтаксичні помилки Python. Роботу з Meta перевіримо після розгортання.

Що робимо зараз
Відкрий GitHub → NolikiBusinessBot → api.py.
Натисни ✏️ Edit.
Виділи весь код через Ctrl + A.
Встав код із завантаженого файлу.
Натисни Commit changes.
Дочекайся повідомлення «Успішне розгортання» в Railway.

Поки нічого не змінюй у VS Code.

Коли Railway завершить оновлення, напиши «Готово». Тоді перевіримо Instagram через Swagger і побачимо, до якої Facebook-сторінки він прив'язаний.

api_viewcontrol_instagram_all_pages.py
Код

Бібліотека
/
api_viewcontrol_instagram_all_pages.py
999
1
2
3
4
5
6
7
8
9
10
11
12
13
14
15
16
17
18
19
20
21
22
23
24
25
26
27
28
29
30
31
32
33
34
35
36
37
38
39
40
41
42
43
44
45
46
47
48
49
50
51
52
53
54
55
56
57
58
59
60
61
62
63
64
65
66
67
68
69
70
71
72
73
74
75
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
from fastapi import FastAPI, Header, HTTPException, Request, Query
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
OAUTH_STATE_LIFETIME = 10 * 60


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
