import os
import pytest
from utils import (
    DB_HOST,
    DB_PORT,
    DB_USER,
    DB_PASSWORD,
    create_db,
    db_connection,
    drop_tables,
)
import gscli.gsexecute


@pytest.fixture(scope="function")
def connection():
    create_db("_test_db")
    connection = db_connection("_test_db")
    yield connection

    drop_tables(connection)
    connection.close()


@pytest.fixture
def cursor(connection):
    with connection.cursor() as cur:
        return cur


@pytest.fixture
def executor(connection):
    return gscli.gsexecute.GSExecute(
        database="_test_db",
        user=DB_USER,
        host=DB_HOST,
        password=DB_PASSWORD,
        port=DB_PORT,
        dsn=None,
    )


@pytest.fixture
def exception_formatter():
    return lambda e: str(e)


@pytest.fixture(scope="session", autouse=True)
def temp_config(tmpdir_factory):
    # this function runs on start of test session.
    # use temporary directory for config home so user config will not be used
    os.environ["XDG_CONFIG_HOME"] = str(tmpdir_factory.mktemp("data"))
