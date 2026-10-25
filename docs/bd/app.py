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
        "по тому же помещению выполняются строго по очереди. "
        "Одним запросом можно забронировать несколько периодов подряд "
        "(time_ids) — все или ни один. Список свободного времени на дату — "
        "GET /availability (занятые и прошедшие периоды в него не попадают)."
    ),
    version="0.6.0",
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
    # Можно выбрать как один период (time_id — старый формат, работает),
    # так и несколько подряд (time_ids): всё создаётся одной бронью-пакетом.
    time_id: int | None = Field(
        None, description="ID периода брони из GET /times (один период)"
    )
    time_ids: list[int] | None = Field(
        None,
        description="ID периодов из GET /times (несколько, например 9:00 и 10:00) — "
                    "создаются все сразу и атомарно",
    )


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


def _period_label(p):
    # "09:00-10:00" — для текста ошибки
    return f'{p["start_time"].strftime("%H:%M")}-{p["end_time"].strftime("%H:%M")}'


def _hhmmss(t):
    return t.strftime("%H:%M:%S")


def _requested_period_ids(body):
    # Принимаем оба формата: time_id (один период) и time_ids (несколько).
    # Дубли убираем, порядок не важен — дальше всё сортируется по времени.
    ids = []
    if body.time_id is not None:
        ids.append(body.time_id)
    for tid in body.time_ids or []:
        if tid not in ids:
            ids.append(tid)
    if not ids:
        raise HTTPException(400, "укажите период: time_id или time_ids")
    return ids


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


@app.post("/bookings", status_code=201, summary="Создать бронь (атомарно, можно несколько периодов)")
def create_booking(body: BookingIn):
    # Одна бронь = один период. Несколько периодов подряд (например 09:00-10:00
    # и 10:00-11:00) приходят как time_ids и создаются ВСЕ ИЛИ НИ ОДИН:
    # та же транзакция и та же блокировка строки ресурса, что и для одного слота.
    # Если занят хотя бы один период — 409 и в базу не пишется ничего.
    requested = _requested_period_ids(body)

    conn = db.connect()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Периоды брони берутся из справочника "Время" (см. GET /times):
        # произвольное время забронировать нельзя.
        cur.execute(
            'SELECT "ID_Время" AS id, '
            '       "ВремяНачала" AS start_time, '
            '       "ВремяОкончания" AS end_time '
            'FROM "Время" WHERE "ID_Время" = ANY(%s) '
            'ORDER BY "ВремяНачала", "ID_Время"',
            (requested,),
        )
        periods = cur.fetchall()
        found = {p["id"] for p in periods}
        missing = [tid for tid in requested if tid not in found]
        if missing:
            raise HTTPException(404, f"период не найден: {missing} (см. GET /times)")

        # Прошедшее время — 400 целиком: частичная бронь сломала бы ожидание
        # «выделил с 9 до 11 — получил с 9 до 11».
        now = datetime.now()
        past = [
            p for p in periods
            if body.booking_date < now.date()
            or (body.booking_date == now.date() and p["start_time"] < now.time())
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
        """, (body.resource_id, body.booking_date, cancelled_id, requested))
        busy = cur.fetchall()
        if busy:
            raise HTTPException(
                409,
                "накладка: уже занято " + ", ".join(_period_label(p) for p in busy),
            )

        active_id = status_id(cur, ACTIVE)
        # INSERT ... SELECT вставляет все выбранные периоды одним оператором.
        cur.execute(
            'INSERT INTO "Бронь" '
            '("ID_Пользователь","ID_Ресурс","ID_Статус","ID_Время","Дата") '
            'SELECT %s, %s, %s, v."ID_Время", %s FROM "Время" v '
            'WHERE v."ID_Время" = ANY(%s) '
            'RETURNING "ID_Бронь", "ID_Время"',
            (body.user_id, body.resource_id, active_id, body.booking_date, requested),
        )
        new_ids = {row["ID_Время"]: row["ID_Бронь"] for row in cur.fetchall()}

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Ответ: и старые поля (id/time_id/start_time/end_time), и пакет целиком.
    created = [
        {
            "id": new_ids[p["id"]],
            "time_id": p["id"],
            "start_time": _hhmmss(p["start_time"]),
            "end_time": _hhmmss(p["end_time"]),
        }
        for p in periods
    ]
    return {
        "id": created[0]["id"],
        "ids": [c["id"] for c in created],
        "count": len(created),
        "user_id": body.user_id,
        "resource_id": body.resource_id,
        "date": body.booking_date.isoformat(),
        "time_id": created[0]["time_id"],
        "time_ids": [c["time_id"] for c in created],
        "start_time": created[0]["start_time"],
        "end_time": created[-1]["end_time"],
        "status": ACTIVE,
        "bookings": created,
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
