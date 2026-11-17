# API бронирования помещений (FastAPI + PostgreSQL).
#
# Swagger: http://127.0.0.1:8000/docs
# Запуск:  python init_db.py  (один раз), затем  uvicorn app:app --reload
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
import secrets

import bcrypt
import psycopg2
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from psycopg2.errors import UniqueViolation
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, Field, field_validator

import db

# Жизненный цикл брони: BOOKED (забронирована, слот занят) →
# CLOSED (закрыта — период закончился, ставится автоматически при чтении).
# CANCELLED — отменена (слот свободен).
BOOKED = "BOOKED"
CLOSED = "CLOSED"
CANCELLED = "CANCELLED"

# Роли пользователей. USER регистрируется сам; ADMIN создаётся init_db.py и
# может отменять брони любых пользователей (слот освобождается так же).
ROLE_USER = "USER"
ROLE_ADMIN = "ADMIN"

# Сессии администратора: токен живёт SESSION_HOURS часов, после logout удаляется.
SESSION_HOURS = 12

# Сведения о сборке — для попапа «i» на фронте и для баг-репортов
# («на какой версии найден дефект»). Единственный источник версии в коде:
# отсюда же берётся version= у FastAPI (Swagger).
# ⚠ BUILD_DATE обновлять вручную при каждом релизе (ISO, YYYY-MM-DD);
#   человекочитаемая дата для попапа (ДД.ММ.ГГГГ) считается из неё.
VERSION = "1.0.0"
BUILD_DATE = "2026-11-01"
BUILD_DATE_LABEL = datetime.strptime(BUILD_DATE, "%Y-%m-%d").strftime("%d.%m.%Y")
PRODUCT_NAME = "Система бронирования помещений"
SUPPORT = {
    "name": "Техническая поддержка",
    "email": "support@booking-system.ru",
}


def get_periods():
    # Справочник периодов брони — таблица "Время" (сеансы 08:00-09:00, 09:00-10:00, ...).
    # Источник истины — БД: фронт рисует сетку по этому списку.
    rows = query(
        'SELECT "ID_Время"      AS id, '
        '       "ВремяНачала"   AS start_time, '
        '       "ВремяОкончания" AS end_time '
        'FROM "Время" ORDER BY "ВремяНачала", "ID_Время"'
    )
    for row in rows:
        row["duration_minutes"] = _minutes(row["end_time"]) - _minutes(row["start_time"])
    return rows


def working_hours_block():
    # Границы рабочего дня и шаг слота — по справочнику периодов.
    periods = get_periods()
    if not periods:
        return {"start_time": None, "end_time": None, "slot_minutes": None}
    first, last = periods[0], periods[-1]
    slot = _minutes(first["end_time"]) - _minutes(first["start_time"])
    return {
        "start_time": first["start_time"].strftime("%H:%M"),
        "end_time": last["end_time"].strftime("%H:%M"),
        "slot_minutes": slot,
    }


def _minutes(t):
    # datetime.time нельзя вычитать — переводим в минуты от полуночи
    return t.hour * 60 + t.minute


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Локальный встроенный PostgreSQL поднимаем только если нет DATABASE_URL.
    if db.is_embedded():
        db.get_server()
    yield


app = FastAPI(
    title="API бронирования помещений",
    description=(
        "Система бронирования общих помещений. "
        "Время — в ISO 8601 (дата YYYY-MM-DD, интервалы вида "
        "2026-10-26T09:00:00+03:00). Бронь создаётся сразу в статусе BOOKED "
        "(забронирована, слот занят) и после окончания периода автоматически "
        "переходит в CLOSED (закрыта). Отмена — CANCELLED (слот освобождается). "
        "Отменить бронь можно только ДО начала её периода: "
        "если период уже начался (или прошёл), возвращается 409. "
        "Накладки невозможны: в POST /bookings строка ресурса блокируется "
        "(SELECT ... FOR UPDATE) до конца транзакции, плюс на (помещение, дата, "
        "период) стоит уникальный индекс для неотменённых броней. "
        "Несколько периодов подряд (time_ids или starts_at+ends_at) создаются "
        "атомарно — все или ни один. Свободное время на дату — GET /availability."
    ),
    version=VERSION,
    lifespan=lifespan,
)

# CORS: пока разрешены все origin — локальная разработка (авторизации нет, cookies
# не используются). При выносе на реальный домен сузить список до адресов фронта.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class BookingIn(BaseModel):
    user_id: int = Field(..., description="ID пользователя")
    resource_id: int = Field(..., description="ID ресурса (помещения)")
    # Время можно задать двумя способами:
    #   1) booking_date + time_id/time_ids (периоды из GET /times);
    #   2) starts_at + ends_at в ISO 8601 — сервер сам раскладывает интервал на периоды.
    booking_date: date | None = Field(
        None, description="Дата брони, YYYY-MM-DD (обязательна вместе с time_id/time_ids)"
    )
    time_id: int | None = Field(
        None, description="ID периода брони из GET /times (один период)"
    )
    time_ids: list[int] | None = Field(
        None,
        description="ID периодов из GET /times (несколько, например 9:00 и 10:00) — "
                    "создаются все сразу и атомарно",
    )
    starts_at: datetime | None = Field(
        None,
        description="Начало брони в ISO 8601, например 2026-10-26T09:00:00+03:00 "
                    "(альтернатива time_id/time_ids; должен совпадать с сеткой периодов)",
    )
    ends_at: datetime | None = Field(
        None,
        description="Конец брони в ISO 8601, например 2026-10-26T11:00:00+03:00 "
                    "(указывается вместе со starts_at)",
    )
    allow_overlap_own: bool = Field(
        False,
        description="Подтверждение «да, бронирую второе помещение на это же время». "
                    "Если у пользователя уже есть бронь на ту же дату и периоды в "
                    "другом помещении, без этого флага вернётся 409 с описанием "
                    "конфликта (conflict: own_overlap).",
    )


class UserIn(BaseModel):
    # Те же правила, что валидирует фронт (validateAuthFields):
    # имя — только буквы (кириллица/латиница), пробел и дефис, не короче 2 символов;
    # телефон — ровно 10 цифр после +7 (8… приводится к +7…).
    name: str = Field(..., min_length=2, max_length=100,
                      pattern=r'^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё \-]*$',
                      description="Имя (буквы, пробел, дефис; от 2 символов)")
    phone: str = Field(..., description="Телефон в формате +7XXXXXXXXXX")

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not re.search(r'[A-Za-zА-Яа-яЁё]', v):
            raise ValueError("имя должно содержать буквы")
        return v

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, v: str) -> str:
        digits = re.sub(r'\D', '', v)
        if len(digits) == 11 and digits[0] in ('7', '8'):
            digits = digits[1:]
        if not re.fullmatch(r'[0-9]{10}', digits):
            raise ValueError("телефон должен быть в формате +7XXXXXXXXXX")
        return '+7' + digits


class AdminLoginIn(BaseModel):
    # Вход администратора: имя (ФИО из формы админ-панели) + пароль.
    # Телефон тоже принимается — для совместимости со Swagger и старым клиентом.
    fio: str | None = Field(None, description="Имя администратора (например admin)")
    phone: str | None = Field(None, description="Телефон администратора (+7XXXXXXXXXX)")
    password: str = Field(..., min_length=1, description="Пароль")

    @field_validator("password")
    @classmethod
    def _check_identity(cls, v: str, info):
        # Хотя бы одно из полей «кто ты» должно быть заполнено.
        data = info.data
        if not (data.get("fio") or data.get("phone")):
            raise ValueError("укажите имя (fio) или телефон (phone)")
        return v


def query(sql, params=()):
    conn = db.connect()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def user_role(cur, user_id: int) -> str | None:
    # Роль пользователя или None, если такого пользователя нет.
    # Авторизации в проекте нет: роль берётся по переданному user_id.
    cur.execute('SELECT "Роль" FROM "Пользователь" WHERE "ID_Пользователь" = %s',
                (user_id,))
    row = cur.fetchone()
    return None if row is None else row["Роль"]


def session_user(cur, token: str | None):
    # Пользователь по токену сессии администратора.
    # Возвращает {id, name, role} или None (токена нет / истёк / отозван).
    if not token:
        return None
    cur.execute(
        """
        SELECT u."ID_Пользователь" AS id, u."Имя" AS name, u."Роль" AS role
        FROM "Сессия" s
        JOIN "Пользователь" u ON u."ID_Пользователь" = s."ID_Пользователь"
        WHERE s."Токен" = %s AND s."Истекает" > NOW()
        """,
        (token,),
    )
    return cur.fetchone()


def require_admin(cur, token: str | None):
    # Для админских действий: без действительного токена администратора — 403.
    user = session_user(cur, token)
    if user is None or user["role"] != ROLE_ADMIN:
        raise HTTPException(403, "нужен вход администратора")
    return user


def require_admin_or_none(cur, token: str | None):
    # То же, но без исключения: None, если токена/роли нет.
    user = session_user(cur, token)
    return user if user is not None and user["role"] == ROLE_ADMIN else None


def close_finished_bookings():
    # Переводит BOOKED → CLOSED для броней, чей период уже закончился
    # (дата + время окончания < текущего момента). Вызывается при чтении
    # списка броней — отдельный планировщик не нужен.
    # Возвращает число закрытых записей (0 — если закрывать было нечего).
    # При обрыве соединения (пулер Supabase) — одна повторная попытка.
    for attempt in (1, 2):
        try:
            return _close_finished_bookings()
        except psycopg2.OperationalError:
            if attempt == 2:
                return 0
            continue


def _close_finished_bookings():
    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            'SELECT "ID_Статус" FROM "Статус" WHERE "Название" = %s', (CLOSED,))
        row = cur.fetchone()
        if row is None:
            return 0
        closed_id = row[0]
        cur.execute('SELECT "ID_Статус" FROM "Статус" WHERE "Название" = %s', (BOOKED,))
        row = cur.fetchone()
        if row is None:
            return 0
        booked_id = row[0]
        cur.execute(
            'UPDATE "Бронь" b SET "ID_Статус" = %s '
            'FROM "Время" v '
            'WHERE b."ID_Время" = v."ID_Время" AND b."ID_Статус" = %s '
            '  AND (b."Дата" + v."ВремяОкончания") < NOW()',
            (closed_id, booked_id),
        )
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        # Ранние return внутри try не доходят до commit/rollback — закрываем
        # транзакцию явно, иначе соединение остаётся в «idle in transaction»
        # и держит блокировки (на Supabase это съедает лимит).
        try:
            if conn.info.transaction_status != 0:
                conn.rollback()
        except Exception:
            pass
        conn.close()


def status_id(cur, name):
    cur.execute('SELECT "ID_Статус" FROM "Статус" WHERE "Название" = %s', (name,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(500, f"статус {name} не найден — запустите init_db.py")
    return row["ID_Статус"]


def _period_label(p):
    # "09:00-10:00" — для текста ошибки
    return f'{p["start_time"].strftime("%H:%M")}-{p["end_time"].strftime("%H:%M")}'


def _hhmmss(t):
    return t.strftime("%H:%M:%S")


def _iso_local(d, t):
    # Дата + время слота → строка ISO 8601 с часовым поясом сервера,
    # например "2026-10-26T09:00:00+03:00"
    return datetime.combine(d, t).astimezone().isoformat()


def _local_naive(value):
    # Пришедшее по API время приводим к локальному поясу сервера и убираем tzinfo,
    # чтобы сравнивать с временами из справочника периодов.
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.replace(tzinfo=None)


def _period_started(booking_date, start_time):
    # Прошедшим считаем период, который уже НАЧАЛСЯ: бронь 08:00-09:00 нельзя
    # отменить/подтвердить уже в 08:00:00 (и в 08:01, пока она идёт) — иначе
    # можно было бы «перенести»/отменить бронь, которой уже пользуются.
    # Время локальное для сервера — то же, что и в проверке при создании брони.
    now = datetime.now()
    return booking_date < now.date() or (
        booking_date == now.date() and start_time <= now.time()
    )


def _booking_label(booking):
    # "26.10.2026 08:00-09:00" — для текста ошибки
    return f'{booking["date"].strftime("%d.%m.%Y")} {_period_label(booking)}'


def _requested_period_ids(body):
    # Принимаем оба формата: time_id (один период) и time_ids (несколько).
    # Дубли убираем, порядок не важен — дальше всё сортируется по времени.
    ids = []
    if body.time_id is not None:
        ids.append(body.time_id)
    for tid in body.time_ids or []:
        if tid not in ids:
            ids.append(tid)
    return ids


def _resolve_periods(cur, body):
    # Возвращает (дата брони, [выбранные периоды]) — либо по time_id/time_ids,
    # либо по интервалу starts_at/ends_at в ISO 8601 (должен совпадать с сеткой).
    ids = _requested_period_ids(body)

    if ids:
        if body.booking_date is None:
            raise HTTPException(
                400, "вместе с time_id/time_ids нужна booking_date (YYYY-MM-DD)"
            )
        cur.execute(
            'SELECT "ID_Время" AS id, '
            '       "ВремяНачала" AS start_time, '
            '       "ВремяОкончания" AS end_time '
            'FROM "Время" WHERE "ID_Время" = ANY(%s) '
            'ORDER BY "ВремяНачала", "ID_Время"',
            (ids,),
        )
        periods = cur.fetchall()
        found = {p["id"] for p in periods}
        missing = [tid for tid in ids if tid not in found]
        if missing:
            raise HTTPException(404, f"период не найден: {missing} (см. GET /times)")
        return body.booking_date, periods

    # --- вариант 2: интервал в ISO 8601 ---
    if body.starts_at is None and body.ends_at is None:
        raise HTTPException(
            400, "укажите период: time_id/time_ids или пару starts_at + ends_at"
        )
    if body.starts_at is None or body.ends_at is None:
        raise HTTPException(400, "нужны оба поля: starts_at и ends_at")

    start = _local_naive(body.starts_at)
    end = _local_naive(body.ends_at)
    if start >= end:
        raise HTTPException(400, "ends_at должен быть позже starts_at")
    if start.date() != end.date():
        raise HTTPException(400, "starts_at и ends_at должны быть в один день")
    if body.booking_date is not None and body.booking_date != start.date():
        raise HTTPException(400, "booking_date не совпадает с датой в starts_at")
    if start.second or end.second or start.microsecond or end.microsecond:
        raise HTTPException(400, "время должно совпадать с сеткой периодов (см. GET /times)")

    start_min = _minutes(start)
    end_min = _minutes(end)
    cur.execute(
        'SELECT "ID_Время" AS id, '
        '       "ВремяНачала" AS start_time, '
        '       "ВремяОкончания" AS end_time '
        'FROM "Время" ORDER BY "ВремяНачала", "ID_Время"'
    )
    periods = [
        p for p in cur.fetchall()
        if _minutes(p["start_time"]) >= start_min and _minutes(p["end_time"]) <= end_min
    ]
    if (
        not periods
        or _minutes(periods[0]["start_time"]) != start_min
        or _minutes(periods[-1]["end_time"]) != end_min
    ):
        raise HTTPException(
            400, "интервал должен совпадать с сеткой периодов (см. GET /times)"
        )
    # периоды внутри интервала обязаны идти подряд
    for prev, nxt in zip(periods, periods[1:]):
        if prev["end_time"] != nxt["start_time"]:
            raise HTTPException(400, "в интервале есть разрыв — не хватает периода")

    return start.date(), periods


# ---------------------------------------------------------------- чтение

# ------------------------------------------------------------------ фронт
# Отдаём страницы из docs/frontend тем же сервером, что и API: тогда одной
# ссылки достаточно и для сайта, и для запросов (удобно для туннеля).
# Если папки нет — работаем как чистый API (GET / ведёт на Swagger).
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/", include_in_schema=False)
def root():
    index = FRONTEND_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)
    return RedirectResponse("/docs")


@app.get("/admin.html", include_in_schema=False)
def admin_page():
    page = FRONTEND_DIR / "admin.html"
    if page.is_file():
        return FileResponse(page)
    raise HTTPException(404, "admin.html не найден")


if (FRONTEND_DIR / "src").is_dir():
    app.mount("/src", StaticFiles(directory=str(FRONTEND_DIR / "src")), name="static")


@app.get("/version", summary="Версия продукта, дата сборки и контакты поддержки")
@app.get("/api/version", include_in_schema=False)
def get_version():
    # Для иконки «i» на фронте и для баг-репортов: на какой сборке найден
    # дефект и куда обращаться. Два пути на одну функцию: /version — по стилю
    # контракта (базовый URL без /api), /api/version — как в задаче и тест-кейсе.
    # БД не используется намеренно: ответ не зависит от данных и не упадёт
    # на пустой базе (нет запросов к таблицам).
    return {
        "product": PRODUCT_NAME,
        "version": VERSION,
        "build_date": BUILD_DATE,
        "build_date_label": BUILD_DATE_LABEL,
        "support": SUPPORT,
    }


@app.get("/settings", summary="Настройки расписания (рабочие часы, слоты)")
def get_settings():
    # Фронт берёт отсюда границы рабочего дня и шаг слота, чтобы построить
    # расписание дня/недели/месяца и список свободного времени.
    # now — ISO 8601 с часовым поясом сервера.
    periods = get_periods()
    return {
        "working_hours": working_hours_block(),
        "periods_count": len(periods),
        "now": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


@app.get("/times", summary="Справочник периодов брони (сеансы)")
def list_times():
    # Периоды из таблицы "Время" — полная сетка: фронт рисует по ним расписание
    # (включая занятые ячейки). Список свободного времени — GET /availability.
    return get_periods()


@app.get("/availability", summary="Свободные периоды помещения на дату")
def list_availability(resource_id: int, booking_date: date):
    # Готовый список для выпадающего меню времени: занятые и прошедшие периоды
    # в него НЕ попадают — пользователю нечего выбирать «вслепую».
    # Занятость считается по пересечению периодов, отменённые брони не мешают.
    # Для сетки расписания (где нужны и занятые ячейки) остаётся GET /bookings.
    if not query('SELECT 1 FROM "Ресурс" WHERE "ID_Ресурс" = %s', (resource_id,)):
        raise HTTPException(404, "ресурс не найден")

    rows = query("""
        SELECT p."ID_Время"       AS id,
               p."ВремяНачала"    AS start_time,
               p."ВремяОкончания" AS end_time
        FROM "Время" p
        WHERE NOT EXISTS (
            SELECT 1
            FROM "Бронь" b
            JOIN "Время"  v ON b."ID_Время"  = v."ID_Время"
            JOIN "Статус" s ON b."ID_Статус" = s."ID_Статус"
            WHERE b."ID_Ресурс" = %s
              AND b."Дата" = %s
              AND s."Название" <> %s
              AND v."ВремяНачала"    < p."ВремяОкончания"
              AND v."ВремяОкончания" > p."ВремяНачала"
        )
        ORDER BY p."ВремяНачала", p."ID_Время"
    """, (resource_id, booking_date, CANCELLED))

    # Прошедшее время тоже не предлагаем (его всё равно нельзя забронировать).
    now = datetime.now()
    return [
        row for row in rows
        if booking_date > now.date()
        or (booking_date == now.date() and row["start_time"] >= now.time())
    ]


@app.get("/resources", summary="Список помещений")
def list_resources():
    rows = query(
        'SELECT "ID_Ресурс"    AS id, '
        '       "Название"     AS name, '
        '       "Площадь"      AS area, '
        '       "Вместимость"  AS capacity, '
        '       "Оборудование" AS equipment '
        'FROM "Ресурс" ORDER BY "ID_Ресурс"'
    )
    for row in rows:
        row["working_hours"] = working_hours_block()
    return rows


@app.get("/users", summary="Список пользователей")
def list_users(phone: str | None = None, name: str | None = None):
    # фильтры необязательные: для «входа» фронт ищет аккаунт по телефону и/или имени
    sql = (
        'SELECT "ID_Пользователь" AS id, "Имя" AS name, "Телефон" AS phone, '
        '\"Роль\" AS role '
        'FROM "Пользователь"'
    )
    where = []
    params = []
    if phone is not None:
        where.append('"Телефон" = %s')
        params.append(phone)
    if name is not None:
        where.append('"Имя" = %s')
        params.append(name)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += ' ORDER BY "ID_Пользователь"'
    return query(sql, tuple(params))


@app.get("/bookings", summary="Список броней")
def list_bookings(
    user_id: int | None = None,
    booking_date: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    resource_id: int | None = None,
    search: str | None = None,
    sort: str = "start",
    token: str | None = None,
):
    # все параметры необязательные: без них — все брони (только с админским токеном);
    # ?user_id=1 — «мои брони», ?booking_date=... — расписание дня,
    # ?date_from=...&date_to=... — диапазон (вкладка «неделя» / «месяц»),
    # ?resource_id=2 — одно помещение (можно вместе с date_from/date_to),
    # ?search=иван — подстрока имени заказчика (регистр не важен),
    # ?sort=start|start_desc — порядок по времени начала (по умолчанию — по возрастанию),
    # ?token=... — токен администратора: снимает ограничение «только свои», открывает
    # поиск по заказчику и просмотр всех броней. Без токена: обязателен user_id,
    # search запрещён (403), отдаются только брони этого пользователя.
    # Перед выдачей закрываем прошедшие брони (BOOKED → CLOSED): статус
    # пересчитывается в момент чтения, фоновой задачи не требуется.
    close_finished_bookings()
    conn = db.connect()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        is_admin = require_admin_or_none(cur, token) is not None
    finally:
        conn.close()

    if not is_admin:
        if user_id is None:
            raise HTTPException(403, "нужен вход администратора или user_id")
        if search is not None:
            raise HTTPException(403, "поиск по заказчику доступен администратору")

    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(400, "date_from должен быть не позже date_to")

    sort_sql = {
        "start": 'b."Дата", v."ВремяНачала"',
        "start_desc": 'b."Дата" DESC, v."ВремяНачала" DESC',
    }
    if sort not in sort_sql:
        raise HTTPException(400, "sort должен быть start или start_desc")

    sql = """
        SELECT b."ID_Бронь"        AS id,
               b."ID_Пользователь" AS user_id,
               u."Имя"             AS user,
               r."Название"        AS resource,
               s."Название"        AS status,
               b."ID_Время"        AS time_id,
               v."ВремяНачала"     AS start_time,
               v."ВремяОкончания"  AS end_time,
               b."Дата"            AS date,
               b."Создана"         AS created_at
        FROM "Бронь" b
        JOIN "Пользователь" u ON b."ID_Пользователь" = u."ID_Пользователь"
        JOIN "Ресурс"       r ON b."ID_Ресурс"       = r."ID_Ресурс"
        JOIN "Статус"       s ON b."ID_Статус"       = s."ID_Статус"
        JOIN "Время"        v ON b."ID_Время"        = v."ID_Время"
    """
    where = []
    params = []
    if user_id is not None:
        where.append('b."ID_Пользователь" = %s')
        params.append(user_id)
    if booking_date is not None:
        where.append('b."Дата" = %s')
        params.append(booking_date)
    if date_from is not None:
        where.append('b."Дата" >= %s')
        params.append(date_from)
    if date_to is not None:
        where.append('b."Дата" <= %s')
        params.append(date_to)
    if resource_id is not None:
        where.append('b."ID_Ресурс" = %s')
        params.append(resource_id)
    if search is not None and search.strip():
        where.append('u."Имя" ILIKE %s')
        params.append(f"%{search.strip()}%")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f' ORDER BY {sort_sql[sort]}, b."ID_Бронь"'
    rows = query(sql, tuple(params))
    for row in rows:
        # Плюс к date/start_time/end_time — полные временные метки ISO 8601
        row["starts_at"] = _iso_local(row["date"], row["start_time"])
        row["ends_at"] = _iso_local(row["date"], row["end_time"])
    return rows


@app.get("/summary", summary="Сводка занятых часов по ресурсам за период (только администратор)")
def summary(
    date_from: date,
    date_to: date,
    resource_id: int | None = None,
    token: str | None = None,
):
    # Сумма часов броней по каждому помещению за период (обе даты включительно).
    # Час = длительность периода из справочника "Время" (сейчас 60 мин).
    # Учитываются BOOKED и CLOSED (состоявшиеся брони); CANCELLED не входит.
    # ?resource_id=2 — сводка по одному помещению.
    # ?token=... — обязателен токен администратора (POST /auth/admin-login).
    close_finished_bookings()
    conn = db.connect()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        require_admin(cur, token)
    finally:
        conn.close()
    # Помещения без броней в период (0 часов) в ответ не попадают.
    if date_from > date_to:
        raise HTTPException(400, "date_from должен быть не позже date_to")

    sql = """
        SELECT r."ID_Ресурс" AS resource_id,
               r."Название"  AS resource,
               COALESCE(SUM(
                   EXTRACT(EPOCH FROM (v."ВремяОкончания" - v."ВремяНачала")) / 3600.0
               ), 0)        AS hours,
               COUNT(*)     AS bookings_count
        FROM "Бронь" b
        JOIN "Ресурс" r ON b."ID_Ресурс" = r."ID_Ресурс"
        JOIN "Время"  v ON b."ID_Время"  = v."ID_Время"
        JOIN "Статус" s ON b."ID_Статус" = s."ID_Статус"
        WHERE b."Дата" BETWEEN %s AND %s
          AND s."Название" <> %s
    """
    params = [date_from, date_to, CANCELLED]
    if resource_id is not None:
        sql += ' AND b."ID_Ресурс" = %s'
        params.append(resource_id)
    sql += ' GROUP BY r."ID_Ресурс", r."Название" ORDER BY r."ID_Ресурс"'

    rows = query(sql, tuple(params))
    for row in rows:
        row["hours"] = float(row["hours"])
    return [row for row in rows if row["hours"] > 0]


# ---------------------------------------------------------------- запись

@app.post("/users", status_code=201, summary="Зарегистрироваться")
def create_user(body: UserIn):
    conn = db.connect()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT 1 FROM "Пользователь" WHERE "Телефон" = %s', (body.phone,))
        if cur.fetchone() is not None:
            raise HTTPException(409, "аккаунт с таким телефоном уже есть")
        cur.execute(
            'INSERT INTO "Пользователь" ("Имя","Телефон","Роль") VALUES (%s,%s,%s) '
            'RETURNING "ID_Пользователь"',
            (body.name, body.phone, ROLE_USER),
        )
        new_id = cur.fetchone()["ID_Пользователь"]
        conn.commit()
    except UniqueViolation:
        # гонка двух регистраций на один телефон: уникальный индекс не дал дубля
        conn.rollback()
        raise HTTPException(409, "аккаунт с таким телефоном уже есть")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"id": new_id, "name": body.name, "phone": body.phone, "role": ROLE_USER}


@app.post("/auth/admin-login", summary="Вход администратора (ФИО + пароль)")
def admin_login(body: AdminLoginIn):
    # Пароль проверяется по bcrypt-хэшу. Успешный вход создаёт токен сессии,
    # который принимают админские действия (GET /bookings без user_id,
    # GET /summary, отмена чужой брони). Срок жизни — SESSION_HOURS.
    # «Кто ты» — имя (fio из формы админ-панели) или телефон (phone, Swagger):
    # имя ищется без учёта регистра, телефон — точно.
    conn = db.connect()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        base_sql = (
            'SELECT "ID_Пользователь", "Имя", "Телефон", "Роль" AS role, '
            '       "ПарольHash" AS pw FROM "Пользователь" '
        )
        if body.fio and body.fio.strip():
            cur.execute(base_sql + 'WHERE lower("Имя") = lower(%s) '
                                   'ORDER BY "ID_Пользователь" LIMIT 1',
                        (body.fio.strip(),))
        else:
            cur.execute(base_sql + 'WHERE "Телефон" = %s', (body.phone,))
        user = cur.fetchone()
        # Одинаковый ответ на «нет такого» и «неверный пароль» — не подсказываем,
        # какие аккаунты существуют.
        if (user is None or user["role"] != ROLE_ADMIN or not user["pw"]
                or not bcrypt.checkpw(body.password.encode("utf-8"),
                                      user["pw"].encode("utf-8"))):
            raise HTTPException(401, "неверное имя/телефон или пароль")

        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)
        cur.execute(
            'INSERT INTO "Сессия" ("Токен","ID_Пользователь","Истекает") '
            'VALUES (%s,%s,%s)',
            (token, user["ID_Пользователь"], expires),
        )
        # попутно подчищаем просроченные сессии
        cur.execute('DELETE FROM "Сессия" WHERE "Истекает" <= NOW()')
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "token": token,
        "expires_at": expires.isoformat(),
        "user": {"id": user["ID_Пользователь"], "name": user["Имя"],
                 "role": user["role"]},
    }


@app.post("/auth/admin-logout", summary="Выход администратора (отзыв токена)")
def admin_logout(token: str):
    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute('DELETE FROM "Сессия" WHERE "Токен" = %s RETURNING "Токен"',
                    (token,))
        removed = cur.fetchone() is not None
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"logged_out": removed}


@app.post("/bookings", status_code=201, summary="Создать бронь (атомарно, можно несколько периодов)")
def create_booking(body: BookingIn):
    # Одна бронь = один период. Несколько периодов подряд (например 09:00-10:00
    # и 10:00-11:00) приходят как time_ids или как интервал starts_at/ends_at
    # в ISO 8601 и создаются ВСЕ ИЛИ НИ ОДИН: та же транзакция и та же блокировка
    # строки ресурса, что и для одного слота. Новая бронь сразу получает статус
    # BOOKED — подтверждение не требуется.
    conn = db.connect()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Периоды брони берутся из справочника "Время" (см. GET /times):
        # произвольное время забронировать нельзя.
        booking_date, periods = _resolve_periods(cur, body)
        requested = [p["id"] for p in periods]

        # Прошедшее время — 400 целиком: частичная бронь сломала бы ожидание
        # «выделил с 9 до 11 — получил с 9 до 11».
        now = datetime.now()
        past = [
            p for p in periods
            if booking_date < now.date()
            or (booking_date == now.date() and p["start_time"] < now.time())
        ]
        if past:
            raise HTTPException(
                400,
                "нельзя бронировать прошедшее время: "
                + ", ".join(_period_label(p) for p in past),
            )

        # SELECT ... FOR UPDATE блокирует строку ресурса до конца транзакции:
        # параллельные POST по одному помещению выстраиваются в очередь за ней.
        # Дальше каждый оператор — со свежим снимком (READ COMMITTED), т.е. после
        # ожидания блокировки проверка накладки видит уже вставленные брони.
        cur.execute(
            'SELECT "ID_Ресурс" FROM "Ресурс" WHERE "ID_Ресурс" = %s FOR UPDATE',
            (body.resource_id,),
        )
        if cur.fetchone() is None:
            raise HTTPException(404, "ресурс не найден")

        cur.execute(
            'SELECT 1 FROM "Пользователь" WHERE "ID_Пользователь" = %s',
            (body.user_id,),
        )
        if cur.fetchone() is None:
            raise HTTPException(404, "пользователь не найден")

        cancelled_id = status_id(cur, CANCELLED)

        # Проверка «у меня уже есть бронь на это время в ДРУГОМ помещении».
        # Это не ошибка — пользователь может посидеть в двух комнатах, но обычно
        # это опечатка, поэтому без явного allow_overlap_own возвращаем 409
        # с описанием конфликта: фронт показывает «у вас уже есть бронь, уверены?»,
        # а при подтверждении повторяет запрос с allow_overlap_own=true.
        if not body.allow_overlap_own:
            cur.execute("""
                SELECT r."ID_Ресурс"        AS resource_id,
                       r."Название"         AS resource,
                       v."ID_Время"         AS time_id,
                       v."ВремяНачала"      AS start_time,
                       v."ВремяОкончания"   AS end_time
                FROM "Бронь" b
                JOIN "Ресурс" r ON b."ID_Ресурс" = r."ID_Ресурс"
                JOIN "Время"  v ON b."ID_Время"  = v."ID_Время"
                WHERE b."ID_Пользователь" = %s
                  AND b."Дата" = %s
                  AND b."ID_Ресурс" <> %s
                  AND b."ID_Время" = ANY(%s)
                  AND b."ID_Статус" <> %s
                ORDER BY v."ВремяНачала", r."Название"
            """, (body.user_id, booking_date, body.resource_id, requested, cancelled_id))
            own = cur.fetchall()
            if own:
                conflicts = [
                    {
                        "resource": c["resource"],
                        "resource_id": c["resource_id"],
                        "time_id": c["time_id"],
                        "start_time": _hhmmss(c["start_time"]),
                        "end_time": _hhmmss(c["end_time"]),
                    }
                    for c in own
                ]
                names = ", ".join(
                    f'{c["resource"]} ({c["start_time"]}-{c["end_time"]})'
                    for c in conflicts
                )
                raise HTTPException(
                    409,
                    {
                        "detail": "у вас уже есть бронь на это время: " + names
                                  + ". Чтобы забронировать ещё и это помещение, "
                                    "повторите запрос с allow_overlap_own=true",
                        "conflict": "own_overlap",
                        "conflicts": conflicts,
                    },
                )

        # Накладка: пересечение по (ресурс, дата) среди НЕ отменённых —
        # проверяем каждый выбранный период против всех существующих броней.
        # new.start < old.end AND new.end > old.start
        cur.execute("""
            SELECT DISTINCT v."ID_Время"       AS id,
                            v."ВремяНачала"    AS start_time,
                            v."ВремяОкончания" AS end_time
            FROM "Бронь" b
            JOIN "Время" v ON b."ID_Время" = v."ID_Время"
            WHERE b."ID_Ресурс" = %s
              AND b."Дата" = %s
              AND b."ID_Статус" <> %s
              AND EXISTS (
                  SELECT 1 FROM "Время" r
                  WHERE r."ID_Время" = ANY(%s)
                    AND v."ВремяНачала"    < r."ВремяОкончания"
                    AND v."ВремяОкончания" > r."ВремяНачала"
              )
            ORDER BY v."ВремяНачала"
        """, (body.resource_id, booking_date, cancelled_id, requested))
        busy = cur.fetchall()
        if busy:
            raise HTTPException(
                409,
                "накладка: уже занято " + ", ".join(_period_label(p) for p in busy),
            )

        booked_id = status_id(cur, BOOKED)
        # INSERT ... SELECT вставляет все выбранные периоды одним оператором.
        cur.execute(
            'INSERT INTO "Бронь" '
            '("ID_Пользователь","ID_Ресурс","ID_Статус","ID_Время","Дата") '
            'SELECT %s, %s, %s, v."ID_Время", %s FROM "Время" v '
            'WHERE v."ID_Время" = ANY(%s) '
            'RETURNING "ID_Бронь", "ID_Время", "Создана"',
            (body.user_id, body.resource_id, booked_id, booking_date, requested),
        )
        rows = cur.fetchall()
        new_ids = {row["ID_Время"]: row["ID_Бронь"] for row in rows}
        created_at = rows[0]["Создана"]

        conn.commit()
    except UniqueViolation:
        # страховка второй линии: уникальный индекс на (помещение, дата, период)
        # среди НЕ отменённых броней (проверка выше + блокировка уже не пропустили)
        conn.rollback()
        raise HTTPException(409, "накладка: это время уже занято")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Ответ: и старые поля (id/time_id/start_time/end_time), и пакет целиком,
    # и полные метки ISO 8601 (starts_at/ends_at). created_at — время создания
    # брони по часам сервера (колонка "Создана", DEFAULT NOW() в БД).
    created = [
        {
            "id": new_ids[p["id"]],
            "time_id": p["id"],
            "start_time": _hhmmss(p["start_time"]),
            "end_time": _hhmmss(p["end_time"]),
            "starts_at": _iso_local(booking_date, p["start_time"]),
            "ends_at": _iso_local(booking_date, p["end_time"]),
        }
        for p in periods
    ]
    return {
        "id": created[0]["id"],
        "ids": [c["id"] for c in created],
        "count": len(created),
        "user_id": body.user_id,
        "resource_id": body.resource_id,
        "date": booking_date.isoformat(),
        "time_id": created[0]["time_id"],
        "time_ids": [c["time_id"] for c in created],
        "start_time": created[0]["start_time"],
        "end_time": created[-1]["end_time"],
        "starts_at": created[0]["starts_at"],
        "ends_at": created[-1]["ends_at"],
        "status": BOOKED,
        "created_at": created_at.isoformat(),
        "bookings": created,
    }


def _booking_row(cur, booking_id):
    # Строка брони под блокировкой — для подтверждения/отмены.
    cancelled_id = status_id(cur, CANCELLED)
    cur.execute(
        'SELECT b."ID_Бронь"        AS id, '
        '       b."ID_Пользователь" AS user_id, '
        '       s."Название"        AS status, '
        '       b."Дата"            AS date, '
        '       v."ВремяНачала"     AS start_time, '
        '       v."ВремяОкончания"  AS end_time '
        'FROM "Бронь" b '
        'JOIN "Статус" s ON b."ID_Статус" = s."ID_Статус" '
        'JOIN "Время"  v ON b."ID_Время"  = v."ID_Время" '
        'WHERE b."ID_Бронь" = %s FOR UPDATE OF b',
        (booking_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "бронь не найдена")
    row["cancelled_id"] = cancelled_id
    return row


@app.post("/bookings/{booking_id}/cancel", summary="Отменить бронь (владелец или администратор)")
def cancel_booking(booking_id: int, user_id: int, token: str | None = None):
    # Отменить бронь может её владелец ИЛИ администратор. Два способа войти админом:
    #   ?token=...  — токен из POST /auth/admin-login (предпочтительно, не подделать);
    #   ?user_id=N  — если у N роль ADMIN (совместимость со старым фронтом).
    # Повторная отмена — 409, чужая бронь — 403. Период уже начался (в том числе
    # идёт прямо сейчас, например 08:01 для 08:00-09:00) — 409: прошедшее
    # не отменяем, в том числе администратором.
    # Перенос на фронте = «создать новую → отменить старую», поэтому прошедшую
    # бронь нельзя и перенести: шаг отмены упадёт, клиент откатит новую.
    # Строка брони блокируется до COMMIT, гонки безопасны.
    conn = db.connect()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        booking = _booking_row(cur, booking_id)

        is_admin = require_admin_or_none(cur, token) is not None \
            or user_role(cur, user_id) == ROLE_ADMIN
        if booking["user_id"] != user_id and not is_admin:
            raise HTTPException(403, "это не ваша бронь")
        if booking["status"] == CANCELLED:
            raise HTTPException(409, "бронь уже отменена")
        if _period_started(booking["date"], booking["start_time"]):
            raise HTTPException(
                409,
                "нельзя отменить прошедшую бронь: "
                + _booking_label(booking)
                + " (период уже начался)",
            )

        cur.execute(
            'UPDATE "Бронь" SET "ID_Статус" = %s WHERE "ID_Бронь" = %s',
            (booking["cancelled_id"], booking_id),
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "id": booking_id,
        "status": CANCELLED,
        "date": booking["date"].isoformat(),
        "start_time": _hhmmss(booking["start_time"]),
        "end_time": _hhmmss(booking["end_time"]),
        "starts_at": _iso_local(booking["date"], booking["start_time"]),
        "ends_at": _iso_local(booking["date"], booking["end_time"]),
    }
