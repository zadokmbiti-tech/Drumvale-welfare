from dotenv import load_dotenv
load_dotenv()

import os
import psycopg2
from psycopg2 import pool
from sqlalchemy import create_engine
from urllib.parse import urlparse

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
    engine = create_engine(DATABASE_URL)

_pool = None


def _parse_database_url(url):
    """Parse DATABASE_URL to extract connection parameters."""
    parsed = urlparse(url)
    return {
        'host': parsed.hostname,
        'port': parsed.port or 5432,
        'dbname': parsed.path.lstrip('/'),
        'user': parsed.username,
        'password': parsed.password,
    }


def init_pool():
    global _pool
    
    # Try individual env vars first, fall back to parsing DATABASE_URL
    db_host = os.getenv("DB_HOST")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    
    # If any required var is missing, try to parse DATABASE_URL
    if not (db_host and db_name and db_user and db_password):
        if DATABASE_URL:
            parsed = _parse_database_url(DATABASE_URL)
            db_host = db_host or parsed['host']
            db_port = db_port if os.getenv("DB_PORT") else str(parsed['port'])
            db_name = db_name or parsed['dbname']
            db_user = db_user or parsed['user']
            db_password = db_password or parsed['password']
    
    _pool = pool.SimpleConnectionPool(
        minconn=1,
        maxconn=10,
        host=db_host,
        port=db_port,
        dbname=db_name,
        user=db_user,
        password=db_password,
    )


def get_connection():
    return _pool.getconn()


def release_connection(conn):
    _pool.putconn(conn)