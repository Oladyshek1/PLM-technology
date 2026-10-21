# API бронирования помещений (FastAPI + PostgreSQL).
#
# Swagger: http://127.0.0.1:8000/docs
# Запуск:  python init_db.py  (один раз), затем  uvicorn app:app --reload
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from psycopg2.errors import UniqueViolation
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, Field

import db

ACTIVE = "ACTIVE"
CANCELLED = "CANCELLED"


def get_periods():
    # Справочник периодов брони — таблица "Время" (сеансы 08:00-09:00, 09:00-10:00, ...).
    # Источник истины — БД: фронт рисует сетку по этому списку.
    return query(
        'SELECT "ID_Время"      AS id, '
        '       "ВремяНачала"   AS start_time, '
        '       "ВремяОкончания" AS end_time '
        'FROM "Время" ORDER BY "ВремяНачала", "ID_Время"'
    )


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
    db.get_server()  # embedded PostgreSQL поднимается вместе с API
    yield


app = FastAPI(
    title="API бронирования помещений",
    description=(
        "Система бронирования общих помещений. "
        "Накладки невозможны: в POST /bookings строка ресурса блокируется "
        "(SELECT ... FOR UPDATE) до конца транзакции — параллельные запросы "
        "по тому же помещению выполняются строго по очереди."
    ),
    version="0.5.0",
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
    booking_date: date = Field(..., description="Дата брони, YYYY-MM-DD")
    time_id: int = Field(..., description="ID периода брони из GET /times")


class UserIn(BaseModel):
    name: str = Field(..., min_length=1, description="Имя")
    phone: str = Field(..., min_length=1, description="Телефон")


def query(sql, params=()):
    conn = db.connect()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def status_id(cur, name):
    cur.execute('SELECT "ID_Статус" FROM "Статус" WHERE "Название" = %s', (name,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(500, f"статус {name} не найден — запустите init_db.py")
    return row["ID_Статус"]


# ---------------------------------------------------------------- чтение

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@app.get("/settings", summary="Настройки расписания (рабочие часы, слоты)")
def get_settings():
    # Фронт берёт отсюда границы рабочего дня и шаг слота, чтобы построить
    # расписание дня/недели/месяца и список свободного времени.
    periods = get_periods()
    return {
        "working_hours": working_hours_block(),
        "periods_count": len(periods),
        "now": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/times", summary="Справочник периодов брони (сеансы)")
def list_times():
    # Периоды из таблицы "Время" — фронт по ним строит сетку расписания
    # и выпадающий список времени для брони.
    return get_periods()


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
        'SELECT "ID_Пользователь" AS id, "Имя" AS name, "Телефон" AS phone '
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
):
    # все параметры необязательные: без них — все брони;
    # ?user_id=1 — «мои брони», ?booking_date=... — расписание дня,
    # ?date_from=...&date_to=... — диапазон (вкладка «неделя» / «месяц»),
    # ?resource_id=2 — одно помещение
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(400, "date_from должен быть не позже date_to")

    sql = """
        SELECT b."ID_Бронь"        AS id,
               b."ID_Пользователь" AS user_id,
               u."Имя"             AS user,
               r."Название"        AS resource,
               s."Название"        AS status,
               b."ID_Время"        AS time_id,
               v."ВремяНачала"     AS start_time,
               v."ВремяОкончания"  AS end_time,
               b."Дата"            AS date
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
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += ' ORDER BY b."ID_Бронь"'
    return query(sql, tuple(params))


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
            'INSERT INTO "Пользователь" ("Имя","Телефон") VALUES (%s,%s) '
            'RETURNING "ID_Пользователь"',
            (body.name, body.phone),
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
    return {"id": new_id, "name": body.name, "phone": body.phone}


@app.post("/bookings", status_code=201, summary="Создать бронь (атомарно)")
def create_booking(body: BookingIn):
    conn = db.connect()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Период брони берётся из справочника "Время" (см. GET /times):
        # произвольное время забронировать нельзя.
        cur.execute(
            'SELECT "ID_Время", "ВремяНачала", "ВремяОкончания" FROM "Время" '
            'WHERE "ID_Время" = %s',
            (body.time_id,),
        )
        period = cur.fetchone()
        if period is None:
            raise HTTPException(404, "период не найден (см. GET /times)")
        start_time = period["ВремяНачала"]
        end_time = period["ВремяОкончания"]

        now = datetime.now()
        if body.booking_date < now.date() or (
            body.booking_date == now.date() and start_time < now.time()
        ):
            raise HTTPException(400, "нельзя бронировать прошедшее время")

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

        # Накладка: пересечение по (ресурс, дата) среди НЕ отменённых.
        # new.start < old.end AND new.end > old.start
        cur.execute("""
            SELECT 1
            FROM "Бронь" b
            JOIN "Время" v ON b."ID_Время" = v."ID_Время"
            WHERE b."ID_Ресурс" = %s
              AND b."Дата" = %s
              AND b."ID_Статус" <> %s
              AND v."ВремяНачала" < %s
              AND v."ВремяОкончания" > %s
            LIMIT 1
        """, (body.resource_id, body.booking_date, cancelled_id,
              end_time, start_time))
        if cur.fetchone() is not None:
            raise HTTPException(409, "накладка: это время уже занято")

        active_id = status_id(cur, ACTIVE)
        cur.execute(
            'INSERT INTO "Бронь" '
            '("ID_Пользователь","ID_Ресурс","ID_Статус","ID_Время","Дата") '
            'VALUES (%s,%s,%s,%s,%s) RETURNING "ID_Бронь"',
            (body.user_id, body.resource_id, active_id, body.time_id, body.booking_date),
        )
        new_id = cur.fetchone()["ID_Бронь"]


        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "id": new_id,
        "user_id": body.user_id,
        "resource_id": body.resource_id,
        "date": body.booking_date.isoformat(),
        "time_id": body.time_id,
        "start_time": start_time.strftime("%H:%M:%S"),
        "end_time": end_time.strftime("%H:%M:%S"),
        "status": ACTIVE,
    }


@app.post("/bookings/{booking_id}/cancel", summary="Отменить бронь")
def cancel_booking(booking_id: int):
    conn = db.connect()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cancelled_id = status_id(cur, CANCELLED)
        # UPDATE атомарен сам по себе: строка брони блокируется до COMMIT,
        # повторная отмена и гонка с другими запросами безопасны.
        cur.execute(
            'UPDATE "Бронь" SET "ID_Статус" = %s WHERE "ID_Бронь" = %s',
            (cancelled_id, booking_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "бронь не найдена")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"id": booking_id, "status": CANCELLED}
