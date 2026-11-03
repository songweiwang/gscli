import pytest
import psycopg2
import psycopg2.extras
from gscli.main import format_output, OutputSettings
from gscli.gsexecute import register_json_typecasters
from os import getenv

DB_USER = getenv("GSUSER", "gaussdb")
DB_HOST = getenv("GSHOST", "localhost")
DB_PORT = getenv("GSPORT", 5432)
DB_PASSWORD = getenv("GSPASSWORD", "gaussdb")


def db_connection(dbname=None):
    conn = psycopg2.connect(
        user=DB_USER,
        host=DB_HOST,
        password=DB_PASSWORD,
        port=DB_PORT,
        database=dbname,
    )
    conn.autocommit = True
    return conn


try:
    conn = db_connection()
    CAN_CONNECT_TO_DB = True
    SERVER_VERSION = conn.server_version
    json_types = register_json_typecasters(conn, lambda x: x)
    JSON_AVAILABLE = "json" in json_types
    JSONB_AVAILABLE = "jsonb" in json_types
except Exception:
    CAN_CONNECT_TO_DB = JSON_AVAILABLE = JSONB_AVAILABLE = False
    SERVER_VERSION = 0


dbtest = pytest.mark.skipif(
    not CAN_CONNECT_TO_DB,
    reason="Need a GaussDB instance at localhost accessible by user 'gaussdb'",
)


requires_json = pytest.mark.skipif(
    not JSON_AVAILABLE, reason="GaussDB server unavailable or json type not defined"
)


requires_jsonb = pytest.mark.skipif(
    not JSONB_AVAILABLE, reason="GaussDB server unavailable or jsonb type not defined"
)


def create_db(dbname):
    with db_connection().cursor() as cur:
        try:
            cur.execute("""CREATE DATABASE _test_db""")
        except Exception:
            pass


def drop_tables(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            DROP SCHEMA public CASCADE;
            CREATE SCHEMA public;
            DROP SCHEMA IF EXISTS schema1 CASCADE;
            DROP SCHEMA IF EXISTS schema2 CASCADE"""
        )


def run(
    executor, sql, join=False, expanded=False, gsspecial=None, exception_formatter=None
):
    "Return string output for the sql to be run"

    results = executor.run(sql, gsspecial, exception_formatter)
    formatted = []
    settings = OutputSettings(
        table_format="psql", dcmlfmt="d", floatfmt="g", expanded=expanded
    )
    for title, rows, headers, status, sql, success, is_special in results:
        formatted.extend(format_output(title, rows, headers, status, settings))
    if join:
        formatted = "\n".join(formatted)

    return formatted


def completions_to_set(completions):
    return {
        (completion.display_text, completion.display_meta_text)
        for completion in completions
    }
