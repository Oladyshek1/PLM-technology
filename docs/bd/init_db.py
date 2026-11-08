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

# Жизненный цикл брони: PENDING (создана) → CONFIRMED (подтверждена),
# CANCELLED — отменена. Слот занимают все статусы, кроме CANCELLED.
PENDING = "PENDING"
CONFIRMED = "CONFIRMED"
CANCELLED = "CANCELLED"

# Роли пользователей: обычный пользователь и администратор (может видеть
# и отменять брони всех пользователей). При регистрации всегда USER —
# администратор создаётся только сидом init_db.py.
ROLE_USER = "USER"
ROLE_ADMIN = "ADMIN"

# Учётная запись администратора: создаётся этим скриптом (ensure_admin),
# через регистрацию её не получить — телефон занят, POST /users вернёт 409.
ADMIN_NAME = "admin"
ADMIN_PHONE = "+70000000000"

DDL = """
CREATE TABLE IF NOT EXISTS "Пользователь" (
    "ID_Пользователь" SERIAL PRIMARY KEY,
    "Имя"             TEXT NOT NULL,
    "Телефон"         TEXT NOT NULL,
    "Роль"            TEXT NOT NULL DEFAULT 'USER'
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


def ensure_statuses(cur):
    # Справочник статусов брони (идемпотентно).
    cur.executemany(
        'INSERT INTO "Статус" ("Название") VALUES (%s) '
        'ON CONFLICT ("Название") DO NOTHING',
        [(PENDING,), (CONFIRMED,), (CANCELLED,)],
    )


def migrate_active_status(cur):
    # Старый статус ACTIVE заменён на CONFIRMED: брони переводим, строку убираем.
    cur.execute('SELECT "ID_Статус" FROM "Статус" WHERE "Название" = %s', ("ACTIVE",))
    old = cur.fetchone()
    if old is None:
        return
    cur.execute('SELECT "ID_Статус" FROM "Статус" WHERE "Название" = %s', (CONFIRMED,))
    confirmed_id = cur.fetchone()[0]
    cur.execute(
        'UPDATE "Бронь" SET "ID_Статус" = %s WHERE "ID_Статус" = %s',
        (confirmed_id, old[0]),
    )
    moved = cur.rowcount
    cur.execute('DELETE FROM "Статус" WHERE "ID_Статус" = %s', (old[0],))
    print(f"Статусы: ACTIVE объединён с CONFIRMED (броней переведено: {moved})")


def ensure_active_slot_index(cur):
    # Одна активная (не отменённая) бронь на (помещение, дата, период).
    # Сначала вычищаем возможные дубли: самую раннюю бронь оставляем,
    # остальные переводим в CANCELLED (история сохраняется).
    cur.execute('SELECT "ID_Статус" FROM "Статус" WHERE "Название" = %s', (CANCELLED,))
    cancelled_id = int(cur.fetchone()[0])

    cur.execute("""
        SELECT "ID_Бронь" FROM (
            SELECT "ID_Бронь",
                   ROW_NUMBER() OVER (
                       PARTITION BY "ID_Ресурс", "Дата", "ID_Время"
                       ORDER BY "ID_Бронь"
                   ) AS rn
            FROM "Бронь"
            WHERE "ID_Статус" <> %s
        ) t
        WHERE rn > 1
    """, (cancelled_id,))
    duplicates = [row[0] for row in cur.fetchall()]
    if duplicates:
        cur.execute(
            'UPDATE "Бронь" SET "ID_Статус" = %s WHERE "ID_Бронь" = ANY(%s)',
            (cancelled_id, duplicates),
        )
        print(f"Слоты: отменено дублей активных броней — {len(duplicates)}")

    # Частичный уникальный индекс: отменённые брони один слот не блокируют.
    cur.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS "uq_booking_active_slot" '
        'ON "Бронь" ("ID_Ресурс", "Дата", "ID_Время") '
        f'WHERE "ID_Статус" <> {cancelled_id}'
    )


def migrate_roles(cur):
    # Миграция старых баз: колонки "Роль" может ещё не быть. Добавляем её
    # со значением по умолчанию USER, затем ставим ограничение на значения.
    cur.execute(
        'ALTER TABLE "Пользователь" '
        'ADD COLUMN IF NOT EXISTS "Роль" TEXT NOT NULL DEFAULT %s',
        (ROLE_USER,),
    )
    cur.execute(
        'SELECT 1 FROM pg_constraint WHERE conname = %s',
        ("chk_пользователь_роль",),
    )
    if cur.fetchone() is None:
        cur.execute(
            'ALTER TABLE "Пользователь" ADD CONSTRAINT "chk_пользователь_роль" '
            f"CHECK (\"Роль\" IN ('{ROLE_USER}', '{ROLE_ADMIN}'))"
        )


def ensure_admin(cur):
    # Администратор с фиксированными именем и телефоном должен существовать
    # всегда: его ищут на входе по телефону (POST /users → 409 на дубль), поэтому
    # заводить его можно только здесь. Идемпотентно: если такой телефон уже есть —
    # только поднимаем роль до ADMIN (старые базы, где Иван был админом).
    cur.execute(
        'SELECT "ID_Пользователь", "Роль" FROM "Пользователь" WHERE "Телефон" = %s',
        (ADMIN_PHONE,),
    )
    row = cur.fetchone()
    if row is not None:
        if row[1] != ROLE_ADMIN:
            cur.execute('UPDATE "Пользователь" SET "Роль" = %s WHERE "ID_Пользователь" = %s',
                        (ROLE_ADMIN, row[0]))
            print(f"Роль ADMIN выдана существующему аккаунту {ADMIN_PHONE} (id {row[0]})")
        return

    cur.execute(
        'INSERT INTO "Пользователь" ("Имя","Телефон","Роль") VALUES (%s,%s,%s) '
        'RETURNING "ID_Пользователь"',
        (ADMIN_NAME, ADMIN_PHONE, ROLE_ADMIN),
    )
    print(f"Создан администратор {ADMIN_NAME} (id {cur.fetchone()[0]}, "
          f"телефон {ADMIN_PHONE})")


def main():
    print("Embedded PostgreSQL: первый запуск может занять несколько секунд...")
    db.get_server()
    ensure_database()

    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute(DDL)  # несколько операторов в одной транзакции

        ensure_statuses(cur)
        migrate_roles(cur)

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
                'INSERT INTO "Пользователь" ("Имя","Телефон","Роль") VALUES (%s,%s,%s)',
                [("Иван", "+79001112233", ROLE_USER),
                 ("Мария", "+79004445566", ROLE_USER)],
            )

        ensure_admin(cur)

        periods = seed_periods(cur)
        migrate_active_status(cur)
        ensure_active_slot_index(cur)

        conn.commit()
        print(f"OK: таблицы созданы, залиты статусы ({PENDING}, {CONFIRMED}, {CANCELLED}), "
              f"3 переговорные, 3 пользователя (админ {ADMIN_NAME} / {ADMIN_PHONE}), "
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
