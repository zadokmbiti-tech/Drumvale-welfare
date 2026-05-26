from dotenv import load_dotenv
load_dotenv()

import os
import psycopg2
from psycopg2 import pool

# Try DATABASE_URL first, fall back to individual variables
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    engine = create_engine(DATABASE_URL)
else:
    DB_HOST = os.getenv("DB_HOST")
    DB_PORT = os.getenv("DB_PORT", "5432")
    DB_NAME = os.getenv("DB_NAME")
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

_pool = None


def init_pool():
    global _pool
    _pool = pool.SimpleConnectionPool(
        minconn=1,
        maxconn=10,
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )


def get_connection():
    return _pool.getconn()


def release_connection(conn):
    _pool.putconn(conn)