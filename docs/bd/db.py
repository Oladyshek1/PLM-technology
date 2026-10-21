# Embedded PostgreSQL (пакет pgserver) — общий модуль для init_db.py и app.py.
#
# Сервер поднимается автоматически при первом обращении (initdb + старт),
# данные лежат в папке pgdata рядом со скриптом. Останавливается сервер,
# когда завершается последний процесс, его открывший.
from pathlib import Path

import pgserver
import psycopg2

BASE_DIR = Path(__file__).resolve().parent
PGDATA = BASE_DIR / "pgdata"
DB_NAME = "booking"

_server = None


def get_server() -> pgserver.PostgresServer:
    # Возвращает handle сервера; при первом вызове инициализирует и запускает PG.
    global _server
    if _server is None:
        _server = pgserver.get_server(str(PGDATA))
    return _server


def get_uri(database: str = DB_NAME) -> str:
    return get_server().get_uri(database=database)


def connect(database: str = DB_NAME) -> psycopg2.extensions.connection:
    # Подключение к PostgreSQL.
    # autocommit=False (по умолчанию psycopg2): транзакция стартует вместе с первым
    # оператором и закрывается явно conn.commit() / conn.rollback().
    return psycopg2.connect(get_uri(database))
