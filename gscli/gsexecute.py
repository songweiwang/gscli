"""
GaussDB SQL Executor for gscli.

Uses the GaussDB psycopg2 driver (with custom libpq for SHA256 auth).

GaussDB maintains PostgreSQL-compatible pg_catalog system tables while adding
GaussDB-specific schemas (dbe_perf, sys, pkg_service, etc.) and features
(distribution, workload management, compatibility modes).

Reference:
- https://support.huaweicloud.com/centralized-devg-v8-gaussdb/gaussdb-42-0176.html
- https://support.huaweicloud.com/centralized-devg-v2-gaussdb/gaussdb_42_0897.html
"""

import logging
import select
import traceback

import psycopg2
import psycopg2.extensions as ext
import psycopg2.extras
import sqlparse
from psycopg2.extensions import POLL_OK, POLL_READ, POLL_WRITE, make_dsn

from .gsspecial import GSSpecial, CommandNotFound as GSCommandNotFound
from .packages.parseutils.meta import FunctionMetadata, ForeignKey

_logger = logging.getLogger(__name__)

# ─── Type registration ────────────────────────────────────────────────
# Cast all database input to unicode automatically.
ext.register_type(ext.UNICODE)
ext.register_type(ext.UNICODEARRAY)
ext.register_type(ext.new_type((705,), "UNKNOWN", ext.UNICODE))
ext.register_type(ext.new_type((2249,), "RECORD", ext.UNICODE))

# Cast bytea fields to text (hex strings with GaussDB/PG 9+).
ext.register_type(ext.new_type((17,), "BYTEA_TEXT", psycopg2.STRING))

# ─── Wait callback ────────────────────────────────────────────────────
_WAIT_SELECT_TIMEOUT = 1
_wait_callback_is_set = False


def _wait_select(conn):
    """Wait with timeout for GaussDB query execution (allows Ctrl-C)."""
    try:
        while 1:
            try:
                state = conn.poll()
                if state == POLL_OK:
                    break
                elif state == POLL_READ:
                    select.select([conn.fileno()], [], [], _WAIT_SELECT_TIMEOUT)
                elif state == POLL_WRITE:
                    select.select([], [conn.fileno()], [], _WAIT_SELECT_TIMEOUT)
                else:
                    raise conn.OperationalError("bad state from poll: %s" % state)
            except KeyboardInterrupt:
                conn.cancel()
                continue
            except OSError as e:
                errno = e.args[0]
                if errno != 4:
                    raise
    except psycopg2.OperationalError:
        pass


def _set_wait_callback():
    global _wait_callback_is_set
    if _wait_callback_is_set:
        return
    _wait_callback_is_set = True
    ext.set_wait_callback(_wait_select)


# ─── Type casters ─────────────────────────────────────────────────────

def register_date_typecasters(connection):
    """Cast date/timestamp to string to handle out-of-range dates (e.g. BC)."""

    def cast_date(value, cursor):
        return value

    cursor = connection.cursor()
    cursor.execute("SELECT NULL::date")
    if cursor.description is None:
        return
    date_oid = cursor.description[0][1]
    cursor.execute("SELECT NULL::timestamp")
    timestamp_oid = cursor.description[0][1]
    cursor.execute("SELECT NULL::timestamp with time zone")
    timestamptz_oid = cursor.description[0][1]
    oids = (date_oid, timestamp_oid, timestamptz_oid)
    new_type = psycopg2.extensions.new_type(oids, "DATE", cast_date)
    psycopg2.extensions.register_type(new_type)


def register_json_typecasters(conn, loads_fn):
    """Register JSON/JSONB type casters (GaussDB supports these in PG-compat mode)."""
    available = set()
    for name in ["json", "jsonb"]:
        try:
            psycopg2.extras.register_json(conn, loads=loads_fn, name=name)
            available.add(name)
        except (psycopg2.ProgrammingError, psycopg2.errors.ProtocolViolation):
            pass
    return available


# ─── Safe cursor ──────────────────────────────────────────────────────

class ProtocolSafeCursor(psycopg2.extensions.cursor):
    """Cursor that handles protocol errors gracefully."""

    def __init__(self, *args, **kwargs):
        self.protocol_error = False
        self.protocol_message = ""
        super().__init__(*args, **kwargs)

    def __iter__(self):
        if self.protocol_error:
            raise StopIteration
        return super().__iter__()

    def fetchall(self):
        if self.protocol_error:
            return [(self.protocol_message,)]
        return super().fetchall()

    def fetchone(self):
        if self.protocol_error:
            return (self.protocol_message,)
        return super().fetchone()

    def execute(self, sql, args=None):
        try:
            psycopg2.extensions.cursor.execute(self, sql, args)
            self.protocol_error = False
            self.protocol_message = ""
        except psycopg2.errors.ProtocolViolation as ex:
            self.protocol_error = True
            self.protocol_message = ex.pgerror
            _logger.debug("%s: %s" % (ex.__class__.__name__, ex))


# ─── Main executor ────────────────────────────────────────────────────

class GSExecute:
    """
    GaussDB SQL executor.

    Handles connection management, metadata queries, and SQL execution
    for GaussDB databases. Uses pg_catalog for compatibility and adds
    GaussDB-specific features (dbe_perf, compatibility mode, etc.).
    """

    # ── Metadata queries (GaussDB-compatible) ──────────────────────────
    # GaussDB maintains pg_catalog for backward compatibility.
    # These queries work in all compatibility modes (A/B/C/M).

    search_path_query = """
        SELECT * FROM unnest(current_schemas(true))"""

    schemata_query = """
        SELECT  nspname
        FROM    pg_catalog.pg_namespace
        WHERE   nspname NOT IN ('pg_catalog', 'pg_toast', 'information_schema')
          AND   nspname NOT LIKE 'pg_%'
        ORDER BY 1"""

    tables_query = """
        SELECT  n.nspname schema_name,
                c.relname table_name
        FROM    pg_catalog.pg_class c
                LEFT JOIN pg_catalog.pg_namespace n
                    ON n.oid = c.relnamespace
        WHERE   c.relkind = ANY(%s)
          AND   n.nspname NOT IN ('pg_catalog', 'pg_toast', 'information_schema')
        ORDER BY 1,2;"""

    databases_query = """
        SELECT d.datname
        FROM pg_catalog.pg_database d
        ORDER BY 1"""

    full_databases_query = """
        SELECT d.datname as "Name",
            pg_catalog.pg_get_userbyid(d.datdba) as "Owner",
            pg_catalog.pg_encoding_to_char(d.encoding) as "Encoding",
            d.datcollate as "Collate",
            d.datctype as "Ctype",
            pg_catalog.array_to_string(d.datacl, E'\n') AS "Access privileges"
        FROM pg_catalog.pg_database d
        ORDER BY 1"""

    socket_directory_query = """
        SELECT setting
        FROM pg_settings
        WHERE name = 'unix_socket_directories'
    """

    # ── GaussDB-specific queries ───────────────────────────────────────

    gaussdb_version_query = """
        SELECT version()"""

    gaussdb_compat_mode_query = """
        SELECT current_database() AS datname,
               setting AS compat_mode
        FROM pg_settings
        WHERE name = 'sql_compatibility'"""

    gaussdb_node_query = """
        SELECT node_name, node_host, node_port, node_type, nodeis_active
        FROM pgxc_node
        ORDER BY node_type, node_name"""

    # ───────────────────────────────────────────────────────────────────

    def __init__(
        self,
        database=None,
        user=None,
        password=None,
        host=None,
        port=None,
        dsn=None,
        sslmode=None,
        sslcert=None,
        sslkey=None,
        sslrootcert=None,
        **kwargs,
    ):
        self._conn_params = {}
        self.conn = None
        self.dbname = None
        self.user = None
        self.password = None
        self.host = None
        self.port = None
        self.server_version = None
        self.extra_args = None
        self.gaussdb_version = None
        self.compat_mode = None  # 'A' (Oracle), 'B'/'M' (MySQL), 'C' (PostgreSQL)
        self.is_distributed = False
        self.is_virtual_database = lambda: False
        self.connect(
            database, user, password, host, port, dsn,
            sslmode=sslmode, sslcert=sslcert, sslkey=sslkey,
            sslrootcert=sslrootcert, **kwargs,
        )
        self.reset_expanded = None

    def copy(self):
        """Returns a clone of the current executor."""
        return self.__class__(**self._conn_params)

    def connect(
        self,
        database=None,
        user=None,
        password=None,
        host=None,
        port=None,
        dsn=None,
        sslmode=None,
        sslcert=None,
        sslkey=None,
        sslrootcert=None,
        **kwargs,
    ):
        conn_params = self._conn_params.copy()

        new_params = {
            "database": database,
            "user": user,
            "password": password,
            "host": host,
            "port": port,
            "dsn": dsn,
        }
        new_params.update(kwargs)

        # GaussDB SSL connection parameters
        # Ref: https://support.huaweicloud.com/centralized-devg-v8-gaussdb/gaussdb-42-0176.html
        if sslmode:
            new_params["sslmode"] = sslmode
        if sslcert:
            new_params["sslcert"] = sslcert
        if sslkey:
            new_params["sslkey"] = sslkey
        if sslrootcert:
            new_params["sslrootcert"] = sslrootcert

        if new_params["dsn"]:
            new_params = {"dsn": new_params["dsn"], "password": new_params["password"]}
            if new_params["password"]:
                new_params["dsn"] = make_dsn(
                    new_params["dsn"], password=new_params.pop("password")
                )

        conn_params.update({k: v for k, v in new_params.items() if v})
        conn_params["cursor_factory"] = ProtocolSafeCursor

        conn = psycopg2.connect(**conn_params)
        conn.set_client_encoding("utf8")

        self._conn_params = conn_params
        if self.conn:
            self.conn.close()
        self.conn = conn
        self.conn.autocommit = True

        # Read actual connection parameters
        libpq_version = psycopg2.__libpq_version__
        dsn_parameters = {}
        if libpq_version >= 93000:
            dsn_parameters = conn.info.dsn_parameters
        else:
            try:
                dsn_parameters = conn.get_dsn_parameters()
            except Exception as x:
                _logger.info("Exception in get_dsn_parameters: %r", x)

        if dsn_parameters:
            self.dbname = dsn_parameters.get("dbname")
            self.user = dsn_parameters.get("user")
            self.host = dsn_parameters.get("host")
            self.port = dsn_parameters.get("port")
        else:
            self.dbname = conn_params.get("database")
            self.user = conn_params.get("user")
            self.host = conn_params.get("host")
            self.port = conn_params.get("port")

        self.password = password
        self.extra_args = kwargs

        if not self.host:
            self.host = self.get_socket_directory()

        self.pid = conn.get_backend_pid()
        self.superuser = conn.get_parameter_status("is_superuser") in ("on", "1")
        self.server_version = self._parse_server_version(conn)

        _set_wait_callback()

        # Register type casters
        register_date_typecasters(conn)
        register_json_typecasters(self.conn, self._json_typecaster)

        # Detect GaussDB-specific features
        self._detect_gaussdb_features()

    def _detect_gaussdb_features(self):
        """Detect GaussDB version, compatibility mode, and distribution."""
        try:
            with self.conn.cursor() as cur:
                # Get GaussDB version
                cur.execute(self.gaussdb_version_query)
                row = cur.fetchone()
                if row:
                    self.gaussdb_version = row[0]
                _logger.info("GaussDB version: %s", self.gaussdb_version)
        except Exception as e:
            _logger.debug("Could not get GaussDB version: %r", e)
            self.gaussdb_version = self.server_version

        try:
            with self.conn.cursor() as cur:
                # Detect compatibility mode
                cur.execute(self.gaussdb_compat_mode_query)
                row = cur.fetchone()
                if row:
                    self.compat_mode = row[1]
                    _logger.info("GaussDB compatibility mode: %s", self.compat_mode)
        except Exception as e:
            _logger.debug("Could not detect compatibility mode: %r", e)
            # In PG-compat mode (C) or older versions, this setting may not exist
            self.compat_mode = "C"

        try:
            with self.conn.cursor() as cur:
                # Check if distributed (pgxc_node exists)
                cur.execute("SELECT count(*) FROM pgxc_node WHERE node_type = 'D'")
                row = cur.fetchone()
                if row and row[0] > 0:
                    self.is_distributed = True
                    _logger.info("GaussDB distributed mode: %d data nodes", row[0])
        except Exception:
            _logger.debug("Not a distributed GaussDB instance")
            self.is_distributed = False

    @property
    def short_host(self):
        if "," in self.host:
            host, _, _ = self.host.partition(",")
        else:
            host = self.host
        short_host, _, _ = host.partition(".")
        return short_host

    @staticmethod
    def _parse_server_version(conn):
        """Parse server_version, handling GaussDB's non-standard format.

        GaussDB returns a version string like '9.2.8' (not the PostgreSQL
        format like '14.5'). This method normalizes it into a comparable form.
        Returns the raw string so callers see exactly what the server reports.
        """
        raw = conn.get_parameter_status("server_version") or ""
        return raw

    def _select_one(self, cur, sql):
        cur.execute(sql)
        return cur.fetchone()

    def _json_typecaster(self, json_data):
        return json_data

    def failed_transaction(self):
        status = self.conn.get_transaction_status()
        return status == ext.TRANSACTION_STATUS_INERROR

    def valid_transaction(self):
        status = self.conn.get_transaction_status()
        return (
            status == ext.TRANSACTION_STATUS_ACTIVE
            or status == ext.TRANSACTION_STATUS_INTRANS
        )

    def run(self, statement, gsspecial=None, exception_formatter=None, on_error_resume=False):
        """
        Execute SQL statements and yield results.

        :param statement: SQL text (may contain multiple statements)
        :param gsspecial: GSSpecial object for backslash commands
        :param exception_formatter: callable to format exceptions
        :param on_error_resume: continue after errors
        :return: generator of (title, rows, headers, status, query, success, is_special)
        """
        statement = statement.strip()
        if not statement:
            yield (None, None, None, None, statement, False, False)

        for sql in sqlparse.split(statement):
            sql = sql.rstrip(";")
            sql = sqlparse.format(sql, strip_comments=False).strip()
            if not sql:
                continue
            try:
                if gsspecial:
                    # \G handling
                    if sql.endswith("\\G"):
                        if not gsspecial.expanded_output:
                            gsspecial.expanded_output = True
                            self.reset_expanded = True
                        sql = sql[:-2].strip()

                    _logger.debug("Trying a gsspecial command. sql: %r", sql)
                    try:
                        cur = self.conn.cursor()
                    except psycopg2.InterfaceError:
                        cur = None
                    try:
                        response = gsspecial.execute(cur, sql)
                        if cur and cur.protocol_error:
                            yield None, None, None, cur.protocol_message, statement, False, False
                            self.connect()
                            continue
                        for result in response:
                            if len(result) < 7:
                                yield result + (sql, True, True)
                            else:
                                title, out_cur, headers, status, out_sql, success, is_special = result
                                # Execute SQL for special commands that yield query text
                                if is_special and success and out_sql and out_cur is None and headers is None:
                                    try:
                                        if cur is None:
                                            cur = self.conn.cursor()
                                        cur.execute(out_sql)
                                        if cur.description:
                                            headers = [d[0] for d in cur.description]
                                        else:
                                            headers = None
                                        status = cur.statusmessage
                                        yield (title, cur, headers, status, out_sql, True, True)
                                    except Exception as e:
                                        _logger.error("special sql: %r, error: %r", out_sql, e)
                                        yield (title, None, None, str(e), out_sql, False, True)
                                else:
                                    yield result
                        continue
                    except GSCommandNotFound:
                        pass

                # Normal SQL execution
                yield self.execute_normal_sql(sql) + (sql, True, False)
            except psycopg2.DatabaseError as e:
                _logger.error("sql: %r, error: %r", sql, e)
                _logger.error("traceback: %r", traceback.format_exc())

                if self._must_raise(e) or not exception_formatter:
                    raise

                yield None, None, None, exception_formatter(e), sql, False, False

                if not on_error_resume:
                    break
            finally:
                if self.reset_expanded:
                    gsspecial.expanded_output = False
                    self.reset_expanded = None

    def _must_raise(self, e):
        return self.conn.closed != 0

    def execute_normal_sql(self, split_sql):
        """Execute a normal SQL statement. Returns (title, rows, headers, status)."""
        _logger.debug("Regular sql statement. sql: %r", split_sql)
        cur = self.conn.cursor()
        cur.execute(split_sql)

        title = ""
        while len(self.conn.notices) > 0:
            title = self.conn.notices.pop() + title

        if cur.description:
            headers = [x[0] for x in cur.description]
            return title, cur, headers, cur.statusmessage
        elif cur.protocol_error:
            _logger.debug("Protocol error, unsupported command.")
            return title, None, None, cur.protocol_message
        else:
            _logger.debug("No rows in result.")
            return title, None, None, cur.statusmessage

    def search_path(self):
        """Returns the current search path as a list of schema names."""
        try:
            with self.conn.cursor() as cur:
                cur.execute(self.search_path_query)
                return [x[0] for x in cur.fetchall()]
        except psycopg2.ProgrammingError:
            fallback = "SELECT * FROM current_schemas(true)"
            with self.conn.cursor() as cur:
                cur.execute(fallback)
                return cur.fetchone()[0]

    def schemata(self):
        """Returns a list of schema names."""
        with self.conn.cursor() as cur:
            cur.execute(self.schemata_query)
            return [x[0] for x in cur.fetchall()]

    def _relations(self, kinds=("r", "p", "f", "v", "m")):
        """Get table or view name metadata."""
        kind_list = ", ".join("'%s'" % k for k in kinds)
        query = f"""
            SELECT  n.nspname schema_name,
                    c.relname table_name
            FROM    pg_catalog.pg_class c
                    LEFT JOIN pg_catalog.pg_namespace n
                        ON n.oid = c.relnamespace
            WHERE   c.relkind IN ({kind_list})
              AND   n.nspname NOT IN ('pg_catalog', 'pg_toast', 'information_schema')
            ORDER BY 1,2;"""
        with self.conn.cursor() as cur:
            cur.execute(query)
            yield from cur

    def tables(self):
        """Yields (schema_name, table_name) tuples."""
        yield from self._relations(kinds=["r", "p", "f"])

    def views(self):
        """Yields (schema_name, view_name) tuples."""
        yield from self._relations(kinds=["v", "m"])

    def _columns(self, kinds=("r", "p", "f", "v", "m")):
        """Get column metadata for tables and views.

        Uses pg_catalog which GaussDB maintains for compatibility.
        Works in all GaussDB compatibility modes (A/B/C/M).
        """
        kind_list = ", ".join("'%s'" % k for k in kinds)
        columns_query = f"""
            SELECT  nsp.nspname schema_name,
                    cls.relname table_name,
                    att.attname column_name,
                    pg_catalog.format_type(att.atttypid, att.atttypmod) type_name,
                    att.atthasdef AS has_default,
                    pg_catalog.pg_get_expr(def.adbin, def.adrelid, true) as "default"
            FROM    pg_catalog.pg_attribute att
                    INNER JOIN pg_catalog.pg_class cls
                        ON att.attrelid = cls.oid
                    INNER JOIN pg_catalog.pg_namespace nsp
                        ON cls.relnamespace = nsp.oid
                    LEFT OUTER JOIN pg_attrdef def
                        ON def.adrelid = att.attrelid
                        AND def.adnum = att.attnum
            WHERE   cls.relkind IN ({kind_list})
                    AND nsp.nspname NOT IN ('pg_catalog', 'pg_toast', 'information_schema')
                    AND nsp.nspname NOT LIKE 'pg_%'
                    AND NOT att.attisdropped
                    AND att.attnum > 0
            ORDER BY 1, 2, att.attnum"""

        with self.conn.cursor() as cur:
            cur.execute(columns_query)
            yield from cur

    def table_columns(self):
        yield from self._columns(kinds=["r", "p", "f"])

    def view_columns(self):
        yield from self._columns(kinds=["v", "m"])

    def databases(self):
        with self.conn.cursor() as cur:
            cur.execute(self.databases_query)
            return [x[0] for x in cur.fetchall()]

    def full_databases(self):
        with self.conn.cursor() as cur:
            cur.execute(self.full_databases_query)
            headers = [x[0] for x in cur.description]
            return cur.fetchall(), headers, cur.statusmessage

    def get_socket_directory(self):
        with self.conn.cursor() as cur:
            cur.execute(self.socket_directory_query)
            result = cur.fetchone()
            return result[0] if result else ""

    def foreignkeys(self):
        """Yields ForeignKey named tuples."""
        with self.conn.cursor() as cur:
            query = """
                SELECT s_p.nspname AS parentschema,
                       t_p.relname AS parenttable,
                       unnest((
                        select
                            array_agg(attname ORDER BY i)
                        from
                            (select unnest(confkey) as attnum, generate_subscripts(confkey, 1) as i) x
                            JOIN pg_catalog.pg_attribute c USING(attnum)
                            WHERE c.attrelid = fk.confrelid
                        )) AS parentcolumn,
                       s_c.nspname AS childschema,
                       t_c.relname AS childtable,
                       unnest((
                        select
                            array_agg(attname ORDER BY i)
                        from
                            (select unnest(conkey) as attnum, generate_subscripts(conkey, 1) as i) x
                            JOIN pg_catalog.pg_attribute c USING(attnum)
                            WHERE c.attrelid = fk.conrelid
                        )) AS childcolumn
                FROM pg_catalog.pg_constraint fk
                JOIN pg_catalog.pg_class      t_p ON t_p.oid = fk.confrelid
                JOIN pg_catalog.pg_namespace  s_p ON s_p.oid = t_p.relnamespace
                JOIN pg_catalog.pg_class      t_c ON t_c.oid = fk.conrelid
                JOIN pg_catalog.pg_namespace  s_c ON s_c.oid = t_c.relnamespace
                WHERE fk.contype = 'f';
                """
            cur.execute(query)
            for row in cur:
                yield ForeignKey(*row)

    def functions(self):
        """Yields FunctionMetadata named tuples.

        Uses pg_catalog.pg_proc which GaussDB maintains.
        """
        query = """
            SELECT n.nspname schema_name,
                    p.proname func_name,
                    p.proargnames,
                    COALESCE(proallargtypes::regtype[], proargtypes::regtype[])::text[],
                    p.proargmodes,
                    prorettype::regtype::text return_type,
                    p.prokind = 'a' is_aggregate,
                    p.prokind = 'w' is_window,
                    p.proretset is_set_returning,
                    d.deptype = 'e' is_extension,
                    pg_get_expr(proargdefaults, 0) AS arg_defaults
            FROM pg_catalog.pg_proc p
                    INNER JOIN pg_catalog.pg_namespace n
                        ON n.oid = p.pronamespace
            LEFT JOIN pg_depend d ON d.objid = p.oid and d.deptype = 'e'
            WHERE p.prorettype::regtype != 'trigger'::regtype
              AND n.nspname NOT IN ('pg_catalog', 'pg_toast', 'information_schema')
              AND n.nspname NOT LIKE 'pg_%'
            ORDER BY 1, 2
            """

        with self.conn.cursor() as cur:
            try:
                cur.execute(query)
            except psycopg2.ProgrammingError:
                # Fallback for older GaussDB without prokind
                query = """
                    SELECT n.nspname schema_name,
                            p.proname func_name,
                            p.proargnames,
                            COALESCE(proallargtypes::regtype[], proargtypes::regtype[])::text[],
                            p.proargmodes,
                            prorettype::regtype::text return_type,
                            p.proisagg is_aggregate,
                            p.proiswindow is_window,
                            p.proretset is_set_returning,
                            d.deptype = 'e' is_extension,
                            pg_get_expr(proargdefaults, 0) AS arg_defaults
                    FROM pg_catalog.pg_proc p
                            INNER JOIN pg_catalog.pg_namespace n
                                ON n.oid = p.pronamespace
                    LEFT JOIN pg_depend d ON d.objid = p.oid and d.deptype = 'e'
                    WHERE p.prorettype::regtype != 'trigger'::regtype
                    ORDER BY 1, 2
                    """
                cur.execute(query)
            for row in cur:
                yield FunctionMetadata(*row)

    def datatypes(self):
        """Yields tuples of (schema_name, type_name).

        Includes GaussDB-specific types from all user schemas.
        """
        query = """
            SELECT n.nspname schema_name,
                   t.typname type_name
            FROM   pg_catalog.pg_type t
                   INNER JOIN pg_catalog.pg_namespace n
                      ON n.oid = t.typnamespace
            WHERE ( t.typrelid = 0
                    OR (
                          SELECT c.relkind = 'c'
                          FROM pg_catalog.pg_class c
                          WHERE c.oid = t.typrelid
                        )
                  )
                  AND NOT EXISTS(
                        SELECT  1
                        FROM    pg_catalog.pg_type el
                        WHERE   el.oid = t.typelem AND el.typarray = t.oid
                      )
                  AND n.nspname <> 'pg_catalog'
                  AND n.nspname <> 'information_schema'
            ORDER BY 1, 2;
            """
        with self.conn.cursor() as cur:
            cur.execute(query)
            yield from cur

    def casing(self):
        """Yields the most common casing for names used in db functions."""
        with self.conn.cursor() as cur:
            query = r"""
          WITH Words AS (
                SELECT regexp_split_to_table(prosrc, '\W+') AS Word, COUNT(1)
                FROM pg_catalog.pg_proc P
                JOIN pg_catalog.pg_namespace N ON N.oid = P.pronamespace
                JOIN pg_catalog.pg_language L ON L.oid = P.prolang
                WHERE L.lanname IN ('sql', 'plpgsql')
                AND N.nspname NOT IN ('pg_catalog', 'information_schema')
                GROUP BY Word
            ),
            OrderWords AS (
                SELECT Word,
                    ROW_NUMBER() OVER(PARTITION BY LOWER(Word) ORDER BY Count DESC)
                FROM Words
                WHERE Word ~* '.*[a-z].*'
            ),
            Names AS (
                SELECT attname AS Name FROM pg_catalog.pg_attribute
                UNION
                SELECT relname FROM pg_catalog.pg_class
                UNION
                SELECT proname FROM pg_catalog.pg_proc
                UNION
                SELECT typname FROM pg_catalog.pg_type
                UNION
                SELECT nspname FROM pg_catalog.pg_namespace
                UNION
                SELECT unnest(proargnames) FROM pg_proc
            )
            SELECT Word
            FROM OrderWords
            WHERE LOWER(Word) IN (SELECT Name FROM Names)
            AND Row_Number = 1;
            """
            cur.execute(query)
            for row in cur:
                yield row[0]

    # ── GaussDB-specific methods ───────────────────────────────────────

    def gaussdb_info(self):
        """Returns GaussDB version and compatibility info."""
        info = {
            "version": self.gaussdb_version,
            "compat_mode": self.compat_mode,
            "is_distributed": self.is_distributed,
            "server_version": self.server_version,
        }
        return info

    def gaussdb_dbe_perf(self, view_name):
        """Query a dbe_perf view by name.

        :param view_name: name of the view in dbe_perf schema
        :return: (title, cur, headers, status) or raises
        """
        # Validate view_name: only allow alphanumeric and underscore
        import re
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', view_name):
            raise ValueError(f"Invalid view name: {view_name}")
        query = f"SELECT * FROM dbe_perf.{view_name}"
        with self.conn.cursor() as cur:
            cur.execute(query)
            headers = [x[0] for x in cur.description]
            return "", cur, headers, cur.statusmessage

    def gaussdb_nodes(self):
        """Returns list of GaussDB nodes (distributed mode)."""
        if not self.is_distributed:
            return []
        with self.conn.cursor() as cur:
            cur.execute(self.gaussdb_node_query)
            return cur.fetchall()

    def gaussdb_resource_pools(self):
        """Returns workload resource pool info (GaussDB-specific)."""
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    SELECT respool_name, active_statements,
                           max_dop, memory_limit
                    FROM pg_catalog.pg_resource_pool
                    ORDER BY 1
                """)
                return cur.fetchall()
        except Exception:
            return []
