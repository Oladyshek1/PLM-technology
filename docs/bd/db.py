# Подключение к PostgreSQL — общий модуль для init_db.py и app.py.
#
# Два режима, выбираются автоматически:
#
# 1) ОБЛАКО (Supabase / Neon / свой сервер): задана переменная окружения
#    DATABASE_URL, например
#      postgresql://postgres.xxx:pass@aws-0-eu-west-1.pooler.supabase.com:5432/postgres
#    — подключаемся напрямую, встроенный PostgreSQL не поднимаем.
#
# 2) ЛОКАЛЬНАЯ РАЗРАБОТКА (по умолчанию): DATABASE_URL не задана — работает
#    встроенный PostgreSQL (пакет pgserver), данные лежат в папке pgdata.
#
# Переменную можно положить в файл .env рядом с этим модулем (см. .env.example).
from pathlib import Path
import os

import psycopg2

BASE_DIR = Path(__file__).resolve().parent
PGDATA = BASE_DIR / "pgdata"
DB_NAME = "booking"

_server = None


def _load_dotenv():
    # Читаем .env (если есть), не перетирая уже заданные переменные окружения.
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def database_url() -> str | None:
    # Строка подключения из окружения или None (тогда работает pgserver).
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def is_embedded() -> bool:
    # True — локальный встроенный PostgreSQL, False — внешняя БД.
    return database_url() is None


def get_server():
    # Локальный встроенный сервер (только если DATABASE_URL не задана).
    global _server
    if _server is None:
        import pgserver  # импорт внутри: в облаке пакет не нужен
        _server = pgserver.get_server(str(PGDATA))
    return _server


def get_uri(database: str = DB_NAME) -> str:
    url = database_url()
    if url is not None:
        return url
    return get_server().get_uri(database=database)


def connect(database: str = DB_NAME) -> psycopg2.extensions.connection:
    # Подключение к PostgreSQL.
    # autocommit=False (по умолчанию psycopg2): транзакция стартует вместе с первым
    # оператором и закрывается явно conn.commit() / conn.rollback().
    #
    # ВАЖНО: каждый вызов открывает НОВОЕ соединение, и вызывающий код обязан
    # закрыть его (conn.close()) — иначе на бесплатном Supabase упрёмся в лимит
    # «max clients 15». Пула здесь намеренно нет: он отдавал мёртвые соединения.
    return psycopg2.connect(get_uri(database), connect_timeout=15)
