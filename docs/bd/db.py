# Подключение к PostgreSQL — общий модуль для init_db.py и app.py.
#
# Два режима, выбираются автоматически:
#
# 1) ОБЛАКО / внешняя БД (Render, Neon, Supabase, локальный Postgres):
#    задана переменная окружения DATABASE_URL, например
#      postgresql://user:pass@host:5432/booking
#    — подключаемся напрямую, ничего не поднимаем.
#
# 2) ЛОКАЛЬНАЯ РАЗРАБОТКА (по умолчанию):
#    DATABASE_URL не задана → используется встроенный PostgreSQL (пакет pgserver).
#    Сервер поднимается сам при первом обращении (initdb + старт), данные лежат
#    в папке pgdata рядом со скриптом. Останавливается вместе с последним
#    процессом, который его открыл.
#
# Переменные можно положить в файл .env рядом с этим модулем (см. .env.example).
from pathlib import Path
import os

import psycopg2

BASE_DIR = Path(__file__).resolve().parent
PGDATA = BASE_DIR / "pgdata"
DB_NAME = "booking"

_server = None


def _load_dotenv():
    # Читаем .env (если он есть), не перетирая уже заданные переменные окружения.
    # Мини-реализация вместо зависимости от python-dotenv: формат простой —
    # строка KEY=VALUE, # в начале — комментарий.
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
    # Строка подключения из окружения (облачный Postgres) или None —
    # тогда работаем через встроенный pgserver.
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    # Heroku/Render иногда отдают postgres:// — psycopg2 понимает только
    # postgresql://, поэтому правим схему.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def is_embedded() -> bool:
    # True — локальный встроенный PostgreSQL (pgserver), False — внешняя БД.
    return database_url() is None


def get_server():
    # Локальный встроенный сервер (только если DATABASE_URL не задана).
    global _server
    if _server is None:
        import pgserver  # импорт внутри, чтобы в облаке пакет не требовался
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
    # keepalives: облачные пулеры (Supabase pooler, PgBouncer) рвут «тихие»
    # соединения, поэтому держим их живыми и ставим таймаут на установку.
    return psycopg2.connect(
        get_uri(database),
        connect_timeout=15,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
