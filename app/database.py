from dotenv import load_dotenv
load_dotenv()

import os
import psycopg2
from sqlalchemy import create_engine

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    DB_HOST = os.getenv("DB_HOST")
    DB_PORT = os.getenv("DB_PORT", "5432")
    DB_NAME = os.getenv("DB_NAME")
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

engine = create_engine(DATABASE_URL)


def get_connection():
    from urllib.parse import urlparse
    u = urlparse(DATABASE_URL)
    return psycopg2.connect(
        host=u.hostname,
        port=u.port or 5432,
        dbname=u.path.lstrip("/"),
        user=u.username,
        password=u.password,
        sslmode="require",
        options="-c TimeZone=Africa/Nairobi"
    )


def release_connection(conn):
    try:
        if conn and not conn.closed:
            conn.close()
    except Exception:
        pass


def init_pool():
    pass