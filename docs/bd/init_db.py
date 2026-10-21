# Создаёт embedded PostgreSQL (папка pgdata рядом со скриптом), БД booking,
# таблицы и тестовые данные. Идемпотентно — можно запускать повторно.
#
# Запуск:
# python init_db.py

from datetime import time

import psycopg2

import db

# Справочник периодов брони («сеансы»): 08:00-09:00, 09:00-10:00, ..., 21:00-22:00.
# Заливается в таблицу "Время" — одна бронь ссылается на один период.
WORKING_START = time(8, 0)
WORKING_END = time(22, 0)
PERIOD_MINUTES = 60

DDL = """
CREATE TABLE IF NOT EXISTS "Пользователь" (
    "ID_Пользователь" SERIAL PRIMARY KEY,
    "Имя"             TEXT NOT NULL,
    "Телефон"         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS "Ресурс" (
    "ID_Ресурс"    SERIAL PRIMARY KEY,
    "Название"     TEXT NOT NULL,
    "Площадь"      DOUBLE PRECISION,
    "Вместимость"  INTEGER,
    "Оборудование" TEXT
);

CREATE TABLE IF NOT EXISTS "Статус" (
    "ID_Статус" SERIAL PRIMARY KEY,
    "Название"  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS "Время" (
    "ID_Время"       SERIAL PRIMARY KEY,
    "ВремяНачала"    TIME NOT NULL,   -- HH:MM:SS
    "ВремяОкончания" TIME NOT NULL,
    CONSTRAINT "chk_время_порядок" CHECK ("ВремяНачала" < "ВремяОкончания")
);

CREATE TABLE IF NOT EXISTS "Бронь" (
    "ID_Бронь"        SERIAL PRIMARY KEY,
    "ID_Пользователь" INTEGER NOT NULL REFERENCES "Пользователь"("ID_Пользователь"),
    "ID_Ресурс"       INTEGER NOT NULL REFERENCES "Ресурс"("ID_Ресурс"),
    "ID_Статус"       INTEGER NOT NULL REFERENCES "Статус"("ID_Статус"),
    "ID_Время"        INTEGER NOT NULL REFERENCES "Время"("ID_Время"),
    "Дата"            DATE NOT NULL
);

CREATE INDEX IF NOT EXISTS "ix_booking_resource_date"
    ON "Бронь" ("ID_Ресурс", "Дата");

CREATE UNIQUE INDEX IF NOT EXISTS "uq_user_phone"
    ON "Пользователь" ("Телефон");
"""


def ensure_database():
    # Создаёт БД booking, если её ещё нет (CREATE DATABASE нельзя в транзакции).
    admin = psycopg2.connect(db.get_uri("postgres"))
    admin.autocommit = True
    try:
        cur = admin.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db.DB_NAME,))
        if cur.fetchone() is None:
            cur.execute(f'CREATE DATABASE "{db.DB_NAME}"')
            print(f"Создана БД {db.DB_NAME}")
        else:
            print(f"БД {db.DB_NAME} уже существует")
    finally:
        admin.close()


def wanted_periods():
    # Список периодов суток: WORKING_START..WORKING_END с шагом PERIOD_MINUTES
    start_min = WORKING_START.hour * 60 + WORKING_START.minute
    end_min = WORKING_END.hour * 60 + WORKING_END.minute
    periods = []
    cur_min = start_min
    while cur_min + PERIOD_MINUTES <= end_min:
        periods.append((_hhmm(cur_min), _hhmm(cur_min + PERIOD_MINUTES)))
        cur_min += PERIOD_MINUTES
    return periods


def _hhmm(minutes):
    return time(minutes // 60, minutes % 60)


def seed_periods(cur):
    # Справочник периодов в таблице "Время". Идемпотентно: недостающие
    # доливаются, дубликаты одного периода схлопываются (брони переводятся
    # на оставшуюся строку), периоды вне справочника удаляются вместе
    # с брони, которые на них ссылались (старые данные до перехода на справочник).
    wanted = wanted_periods()
    wanted_set = set(wanted)

    for start_t, end_t in wanted:
        cur.execute(
            'SELECT 1 FROM "Время" WHERE "ВремяНачала" = %s AND "ВремяОкончания" = %s',
            (start_t, end_t),
        )
        if cur.fetchone() is None:
            cur.execute(
                'INSERT INTO "Время" ("ВремяНачала","ВремяОкончания") VALUES (%s,%s)',
                (start_t, end_t),
            )

    cur.execute(
        'SELECT "ID_Время", "ВремяНачала", "ВремяОкончания" FROM "Время" ORDER BY "ID_Время"'
    )
    groups = {}
    for tid, start_t, end_t in cur.fetchall():
        groups.setdefault((start_t, end_t), []).append(tid)

    # дубликаты одного и того же периода: оставляем первую строку,
    # брони с остальных id переводим на неё
    for (start_t, end_t), ids in groups.items():
        if len(ids) > 1:
            keep, drop = ids[0], ids[1:]
            cur.execute(
                'UPDATE "Бронь" SET "ID_Время" = %s WHERE "ID_Время" = ANY(%s)',
                (keep, drop),
            )
            cur.execute('DELETE FROM "Время" WHERE "ID_Время" = ANY(%s)', (drop,))
            print(f"Справочник периодов: дубликат {start_t}-{end_t} схлопнут "
                  f"(брони переведены на id {keep})")

    # периоды вне справочника: брони на них удаляются, строки — тоже
    cur.execute(
        'SELECT "ID_Время", "ВремяНачала", "ВремяОкончания" FROM "Время" ORDER BY "ID_Время"'
    )
    extra = [tid for tid, s, e in cur.fetchall() if (s, e) not in wanted_set]
    if extra:
        cur.execute('SELECT COUNT(*) FROM "Бронь" WHERE "ID_Время" = ANY(%s)', (extra,))
        dropped = cur.fetchone()[0]
        cur.execute('DELETE FROM "Бронь" WHERE "ID_Время" = ANY(%s)', (extra,))
        cur.execute('DELETE FROM "Время" WHERE "ID_Время" = ANY(%s)', (extra,))
        print(f"Справочник периодов: удалено броний вне сетки — {dropped}, "
              f"удалено старых периодов — {len(extra)}")

    return len(wanted)


def main():
    print("Embedded PostgreSQL: первый запуск может занять несколько секунд...")
    db.get_server()
    ensure_database()

    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute(DDL)  # несколько операторов в одной транзакции

        cur.executemany(
            'INSERT INTO "Статус" ("Название") VALUES (%s) '
            'ON CONFLICT ("Название") DO NOTHING',
            [("ACTIVE",), ("CANCELLED",)],
        )

        cur.execute('SELECT COUNT(*) FROM "Ресурс"')
        if cur.fetchone()[0] == 0:
            cur.executemany(
                'INSERT INTO "Ресурс" '
                '("Название","Площадь","Вместимость","Оборудование") '
                'VALUES (%s,%s,%s,%s)',
                [
                    ("Переговорная А", 20.0,  8,   "Проектор, маркерная доска"),
                    ("Переговорная Б", 30.0,  12,  "Проектор, видеосвязь"),
                    ("Актовый зал",    120.0, 100, "Сцена, микрофоны, проектор"),
                ],
            )

        cur.execute('SELECT COUNT(*) FROM "Пользователь"')
        if cur.fetchone()[0] == 0:
            cur.executemany(
                'INSERT INTO "Пользователь" ("Имя","Телефон") VALUES (%s,%s)',
                [("Иван", "+79001112233"), ("Мария", "+79004445566")],
            )

        periods = seed_periods(cur)

        conn.commit()
        print(f"OK: таблицы созданы, залиты статусы, 3 переговорные, 2 пользователя, "
              f"{periods} периодов брони ({WORKING_START.strftime('%H:%M')}-"
              f"{WORKING_END.strftime('%H:%M')}, шаг {PERIOD_MINUTES} мин).")
        print("Подключение:", db.get_uri())
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
