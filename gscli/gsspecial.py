"""
gsspecial - GaussDB backslash commands for gscli.

GaussDB uses pg_catalog system tables (maintained for PG-compatible access),
plus GaussDB-specific schemas (dbe_perf, sys, etc.).
"""

import logging
import re
from collections import OrderedDict


def _sanitize_pattern(pattern):
    """Sanitize user input for SQL LIKE patterns.

    Escapes SQL wildcards and special characters to prevent injection.
    Only allows alphanumeric, underscore, dot, and wildcard (% and _).
    """
    if not pattern:
        return pattern
    # Remove any characters that aren't alphanumeric, underscore, dot, space, or wildcards
    sanitized = re.sub(r"[^a-zA-Z0-9_.% *?]", "", pattern)
    # Convert shell-style wildcards to SQL LIKE
    sanitized = sanitized.replace("*", "%").replace("?", "_")
    return sanitized


def _sanitize_identifier(name):
    """Sanitize a SQL identifier (schema/table/column name).

    Only allows alphanumeric and underscore. Rejects anything with
    SQL metacharacters.
    """
    if not name:
        return name
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        return None  # Invalid identifier
    return name

_logger = logging.getLogger(__name__)

# Command argument types
NO_QUERY = 0
QUERY = 1


class CommandNotFound(Exception):
    pass


class GSSpecialCommand:
    """A single backslash command."""

    def __init__(self, handler, command, syntax, description,
                 arg_type=QUERY, case_sensitive=False, aliases=()):
        self.handler = handler
        self.command = command
        self.syntax = syntax
        self.description = description
        self.arg_type = arg_type
        self.case_sensitive = case_sensitive
        self.aliases = aliases


class GSSpecial:
    """GaussDB special (backslash) commands manager."""

    def __init__(self):
        self.commands = OrderedDict()
        self.timing_enabled = False
        self.expanded_output = False
        self.auto_expand = False
        self.pager_config = "on"
        self._register_builtin_commands()

    def register(self, handler, command, syntax, description,
                 arg_type=QUERY, case_sensitive=False, aliases=()):
        """Register a backslash command."""
        cmd = GSSpecialCommand(
            handler, command, syntax, description,
            arg_type, case_sensitive, aliases
        )
        self.commands[command] = cmd
        for alias in aliases:
            self.commands[alias] = cmd

    def _register_builtin_commands(self):
        """Register built-in GaussDB backslash commands."""
        # Database info
        self.register(
            self._cmd_list_databases, '\\l', '\\l',
            'List all databases.',
            arg_type=NO_QUERY,
        )
        # Table commands
        self.register(
            self._cmd_list_tables, '\\dt', '\\dt[+] [pattern]',
            'List tables.',
            arg_type=QUERY,
            aliases=('\\dt+',),
        )
        # Index commands
        self.register(
            self._cmd_list_indexes, '\\di', '\\di[+] [pattern]',
            'List indexes.',
            arg_type=QUERY,
            aliases=('\\di+',),
        )
        # View commands
        self.register(
            self._cmd_list_views, '\\dv', '\\dv[+] [pattern]',
            'List views.',
            arg_type=QUERY,
            aliases=('\\dv+',),
        )
        # Sequence commands
        self.register(
            self._cmd_list_sequences, '\\ds', '\\ds[+] [pattern]',
            'List sequences.',
            arg_type=QUERY,
            aliases=('\\ds+',),
        )
        # Function commands
        self.register(
            self._cmd_list_functions, '\\df', '\\df[+] [pattern]',
            'List functions.',
            arg_type=QUERY,
            aliases=('\\df+',),
        )
        # Schema commands
        self.register(
            self._cmd_list_schemas, '\\dn', '\\dn[+] [pattern]',
            'List schemas.',
            arg_type=NO_QUERY,
            aliases=('\\dn+',),
        )
        # User/role commands
        self.register(
            self._cmd_list_users, '\\du', '\\du[+] [pattern]',
            'List roles/users.',
            arg_type=NO_QUERY,
            aliases=('\\du+',),
        )
        # Table description
        self.register(
            self._cmd_describe_table, '\\d', '\\d[+] [pattern]',
            'Describe table or view.',
            arg_type=QUERY,
            aliases=('\\d+',),
        )
        # GaussDB-specific: version info
        self.register(
            self._cmd_gaussdb_version, '\\version', '\\version',
            'Show GaussDB version information.',
            arg_type=NO_QUERY,
        )
        # GaussDB-specific: compatibility mode
        self.register(
            self._cmd_compat_mode, '\\compat', '\\compat',
            'Show database compatibility mode.',
            arg_type=NO_QUERY,
        )
        # GaussDB-specific: node info (distributed)
        self.register(
            self._cmd_node_info, '\\nodes', '\\nodes',
            'Show GaussDB node information.',
            arg_type=NO_QUERY,
        )
        # GaussDB-specific: performance stats
        self.register(
            self._cmd_perf_stats, '\\perf', '\\perf',
            'Show basic performance statistics from dbe_perf.',
            arg_type=NO_QUERY,
        )
        # GaussDB-specific: workload info
        self.register(
            self._cmd_workload, '\\workload', '\\workload',
            'Show workload group and resource pool information.',
            arg_type=NO_QUERY,
        )
        # GaussDB-specific: distribution info
        self.register(
            self._cmd_distribution, '\\dist', '\\dist [table]',
            'Show table distribution/sharding information.',
            arg_type=QUERY,
        )
        # Encoding
        self.register(
            self._cmd_encoding, '\\encoding', '\\encoding',
            'Show client encoding.',
            arg_type=NO_QUERY,
        )

    def execute(self, cur, sql):
        """Try to execute sql as a special command.

        :param cur: cursor (can be None for NO_QUERY commands)
        :param sql: the SQL text to try
        :return: generator of (title, rows, headers, status, ..., is_special) tuples
        :raises CommandNotFound: if sql is not a special command
        """
        # Normalize: extract the command part
        stripped = sql.strip()

        # Split command and argument
        parts = stripped.split(None, 1)
        command = parts[0]
        arg = parts[1] if len(parts) > 1 else ""

        # Try exact match first, then case-insensitive
        cmd = self.commands.get(command)
        if cmd is None:
            # Try case-insensitive match
            for key, val in self.commands.items():
                if key.lower() == command.lower():
                    cmd = val
                    break

        if cmd is None:
            raise CommandNotFound(sql)

        # Execute the command handler
        yield from cmd.handler(pattern=arg, verbose='+' in command)

    # ---- Built-in command handlers ----

    def _cmd_list_databases(self, pattern="", **_):
        """List all databases."""
        query = """
            SELECT d.datname AS "Name",
                   pg_catalog.pg_get_userbyid(d.datdba) AS "Owner",
                   pg_catalog.pg_encoding_to_char(d.encoding) AS "Encoding",
                   d.datcollate AS "Collation"
            FROM pg_catalog.pg_database d
            ORDER BY 1"""
        yield (None, None, None, None, query, True, True)

    def _cmd_list_tables(self, pattern="", verbose=False, **_):
        """List tables matching pattern."""
        schema_filter = ""
        name_filter = ""
        if '.' in pattern:
            schema_filter, name_filter = pattern.split('.', 1)
        elif pattern:
            name_filter = pattern

        if verbose:
            cols = """
                c.relname AS "Name",
                pg_catalog.pg_get_userbyid(c.relowner) AS "Owner",
                CASE c.relkind
                    WHEN 'r' THEN 'table'
                    WHEN 'p' THEN 'partitioned table'
                    WHEN 'f' THEN 'foreign table'
                END AS "Type",
                pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) AS "Size",
                pg_catalog.obj_description(c.oid, 'pg_class') AS "Description"
            """
        else:
            cols = """
                n.nspname AS "Schema",
                c.relname AS "Name",
                CASE c.relkind
                    WHEN 'r' THEN 'table'
                    WHEN 'p' THEN 'partitioned table'
                    WHEN 'f' THEN 'foreign table'
                END AS "Type"
            """

        query = f"""
            SELECT {cols}
            FROM pg_catalog.pg_class c
            LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'f')
              AND n.nspname <> 'pg_catalog'
              AND n.nspname <> 'information_schema'
              AND pg_catalog.pg_table_is_visible(c.oid)
        """
        safe_name = _sanitize_pattern(name_filter) if name_filter else None
        safe_schema = _sanitize_identifier(schema_filter) if schema_filter else None
        if safe_name:
            query += f" AND c.relname ILIKE '{safe_name}'"
        if safe_schema:
            query += f" AND n.nspname = '{safe_schema}'"
        query += " ORDER BY 1, 2"
        yield (None, None, None, None, query, True, True)

    def _cmd_list_indexes(self, pattern="", verbose=False, **_):
        """List indexes."""
        if verbose:
            cols = """
                n.nspname AS "Schema",
                c.relname AS "Name",
                pg_catalog.pg_get_userbyid(c.relowner) AS "Owner",
                tc.relname AS "Table",
                am.amname AS "Type",
                pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(c.oid)) AS "Size"
            """
        else:
            cols = """
                n.nspname AS "Schema",
                c.relname AS "Name",
                tc.relname AS "Table",
                am.amname AS "Type"
            """
        query = f"""
            SELECT {cols}
            FROM pg_catalog.pg_index i
            JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
            JOIN pg_catalog.pg_class tc ON tc.oid = i.indrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_catalog.pg_am am ON am.oid = c.relam
            WHERE n.nspname <> 'pg_catalog'
              AND pg_catalog.pg_table_is_visible(c.oid)
        """
        safe_pattern = _sanitize_pattern(pattern) if pattern else None
        # Note: pattern will be applied by the executor using mogrify
        if safe_pattern:
            query += f" AND c.relname ILIKE '{safe_pattern}'"
        query += " ORDER BY 1, 2"
        yield (None, None, None, None, query, True, True)

    def _cmd_list_views(self, pattern="", verbose=False, **_):
        """List views."""
        if verbose:
            cols = """
                n.nspname AS "Schema",
                c.relname AS "Name",
                pg_catalog.pg_get_userbyid(c.relowner) AS "Owner",
                pg_catalog.obj_description(c.oid, 'pg_class') AS "Description"
            """
        else:
            cols = """
                n.nspname AS "Schema",
                c.relname AS "Name"
            """
        query = f"""
            SELECT {cols}
            FROM pg_catalog.pg_class c
            LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('v', 'm')
              AND n.nspname <> 'pg_catalog'
              AND n.nspname <> 'information_schema'
              AND pg_catalog.pg_table_is_visible(c.oid)
        """
        safe_pattern = _sanitize_pattern(pattern) if pattern else None
        if safe_pattern:
            query += f" AND c.relname ILIKE '{safe_pattern}'"
        query += " ORDER BY 1, 2"
        yield (None, None, None, None, query, True, True)

    def _cmd_list_sequences(self, pattern="", verbose=False, **_):
        """List sequences."""
        if verbose:
            cols = """
                n.nspname AS "Schema",
                c.relname AS "Name",
                pg_catalog.pg_get_userbyid(c.relowner) AS "Owner"
            """
        else:
            cols = """
                n.nspname AS "Schema",
                c.relname AS "Name"
            """
        query = f"""
            SELECT {cols}
            FROM pg_catalog.pg_class c
            LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'S'
              AND n.nspname <> 'pg_catalog'
              AND n.nspname <> 'information_schema'
              AND pg_catalog.pg_table_is_visible(c.oid)
        """
        safe_pattern = _sanitize_pattern(pattern) if pattern else None
        if safe_pattern:
            query += f" AND c.relname ILIKE '{safe_pattern}'"
        query += " ORDER BY 1, 2"
        yield (None, None, None, None, query, True, True)

    def _cmd_list_functions(self, pattern="", verbose=False, **_):
        """List functions."""
        if verbose:
            cols = """
                n.nspname AS "Schema",
                p.proname AS "Name",
                pg_catalog.pg_get_function_result(p.oid) AS "Result data type",
                pg_catalog.pg_get_function_arguments(p.oid) AS "Argument data types",
                CASE p.prokind
                    WHEN 'a' THEN 'agg'
                    WHEN 'w' THEN 'window'
                    WHEN 'p' THEN 'proc'
                    ELSE 'func'
                END AS "Type"
            """
        else:
            cols = """
                n.nspname AS "Schema",
                p.proname AS "Name",
                pg_catalog.pg_get_function_result(p.oid) AS "Result data type",
                pg_catalog.pg_get_function_arguments(p.oid) AS "Argument data types"
            """
        query = f"""
            SELECT {cols}
            FROM pg_catalog.pg_proc p
            LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname <> 'pg_catalog'
              AND n.nspname <> 'information_schema'
              AND pg_catalog.pg_function_is_visible(p.oid)
        """
        safe_pattern = _sanitize_pattern(pattern) if pattern else None
        if safe_pattern:
            query += f" AND p.proname ILIKE '{safe_pattern}'"
        query += " ORDER BY 1, 2"
        yield (None, None, None, None, query, True, True)

    def _cmd_list_schemas(self, pattern="", **_):
        """List schemas."""
        query = """
            SELECT n.nspname AS "Name",
                   pg_catalog.pg_get_userbyid(n.nspowner) AS "Owner"
            FROM pg_catalog.pg_namespace n
            WHERE n.nspname !~ '^pg_'
              AND n.nspname <> 'information_schema'
            ORDER BY 1"""
        yield (None, None, None, None, query, True, True)

    def _cmd_list_users(self, pattern="", **_):
        """List roles/users."""
        query = """
            SELECT r.rolname AS "Role name",
                   ARRAY(SELECT b.rolname
                         FROM pg_catalog.pg_auth_members m
                         JOIN pg_catalog.pg_roles b ON (m.roleid = b.oid)
                         WHERE m.member = r.oid) AS "Member of",
                   r.rolsuper AS "Super?",
                   r.rolinherit AS "Inherit?",
                   r.rolcreaterole AS "Create role?",
                   r.rolcreatedb AS "Create DB?",
                   r.rolcanlogin AS "Can login?"
            FROM pg_catalog.pg_roles r
            WHERE r.rolname !~ '^pg_'
            ORDER BY 1"""
        yield (None, None, None, None, query, True, True)

    def _cmd_describe_table(self, pattern="", verbose=False, **_):
        """Describe a table or view."""
        if not pattern:
            # List all tables if no pattern
            yield from self._cmd_list_tables(verbose=verbose)
            return

        # Column details for a specific table
        if verbose:
            cols = """
                a.attname AS "Column",
                pg_catalog.format_type(a.atttypid, a.atttypmod) AS "Type",
                CASE WHEN a.attnotnull THEN 'not null' ELSE '' END AS "Nullable",
                COALESCE(pg_catalog.pg_get_expr(ad.adbin, ad.adrelid), '') AS "Default",
                col_description(a.attrelid, a.attnum) AS "Description"
            """
        else:
            cols = """
                a.attname AS "Column",
                pg_catalog.format_type(a.atttypid, a.atttypmod) AS "Type",
                CASE WHEN a.attnotnull THEN 'not null' ELSE '' END AS "Nullable"
            """
        safe_pattern = _sanitize_identifier(pattern)
        if not safe_pattern:
            yield (None, None, None, None,
                   f"Invalid table name: {pattern}", False, True)
            return
        query = f"""
            SELECT {cols}
            FROM pg_catalog.pg_attribute a
            LEFT JOIN pg_catalog.pg_attrdef ad ON (a.attrelid = ad.adrelid AND a.attnum = ad.adnum)
            JOIN pg_catalog.pg_class c ON (a.attrelid = c.oid)
            JOIN pg_catalog.pg_namespace n ON (c.relnamespace = n.oid)
            WHERE c.relname = '{safe_pattern}'
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY a.attnum"""
        yield (None, None, None, None, query, True, True)

    def _cmd_gaussdb_version(self, **_):
        """Show GaussDB version info."""
        query = """
            SELECT version() AS "Version"
        """
        yield (None, None, None, None, query, True, True)

    def _cmd_compat_mode(self, **_):
        """Show database compatibility mode."""
        query = """
            SELECT
                current_database() AS "Database",
                CASE
                    WHEN EXISTS (SELECT 1 FROM pg_settings WHERE name = 'sql_compatibility' AND setting = 'A')
                        THEN 'Oracle (A)'
                    WHEN EXISTS (SELECT 1 FROM pg_settings WHERE name = 'sql_compatibility' AND setting = 'B')
                        THEN 'MySQL (B)'
                    WHEN EXISTS (SELECT 1 FROM pg_settings WHERE name = 'sql_compatibility' AND setting IN ('C', 'PG'))
                        THEN 'PostgreSQL (pg)'
                    WHEN EXISTS (SELECT 1 FROM pg_settings WHERE name = 'sql_compatibility' AND setting = 'M')
                        THEN 'MySQL (M)'
                    ELSE 'Unknown'
                END AS "Compatibility Mode"
        """
        try:
            yield (None, None, None, None, query, True, True)
        except Exception:
            # Fallback if sql_compatibility setting doesn't exist
            yield (None, None, None, None, "SELECT current_database() AS \"Database\", 'Unknown' AS \"Compatibility Mode\"", True, True)

    def _cmd_node_info(self, **_):
        """Show GaussDB node information."""
        # Try GaussDB-specific node views first
        query = """
            SELECT
                node_name,
                node_host AS host,
                node_port AS port,
                node_type,
                nodeis_active AS status
            FROM pgxc_node
            WHERE node_type IN ('C', 'D')
            ORDER BY node_type, node_name
        """
        try:
            yield (None, None, None, None, query, True, True)
        except Exception:
            # Single-node GaussDB or non-distributed
            yield (None, None, None, None,
                   "SELECT 'standalone' AS \"Node\", inet_server_addr() AS \"Host\", "
                   "inet_server_port() AS \"Port\"", True, True)

    def _cmd_perf_stats(self, **_):
        """Show basic performance statistics."""
        query = """
            SELECT
                datname AS "Database",
                numbackends AS "Backends",
                xact_commit AS "Xact Commit",
                xact_rollback AS "Xact Rollback",
                blks_read AS "Blocks Read",
                blks_hit AS "Blocks Hit",
                tup_returned AS "Tuples Returned",
                tup_fetched AS "Tuples Fetched",
                tup_inserted AS "Tuples Inserted",
                tup_updated AS "Tuples Updated",
                tup_deleted AS "Tuples Deleted"
            FROM pg_stat_database
            WHERE datname = current_database()
        """
        yield (None, None, None, None, query, True, True)

    def _cmd_workload(self, **_):
        """Show workload group and resource pool info."""
        query = """
            SELECT
                respool_name AS "Resource Pool",
                active_statements AS "Active Statements",
                max_dop AS "Max DOP",
                memory_limit AS "Memory Limit"
            FROM pg_catalog.pg_resource_pool
            ORDER BY 1
        """
        try:
            yield (None, None, None, None, query, True, True)
        except Exception:
            yield (None, None, None, None,
                   "SELECT 'N/A' AS \"Info\", 'Workload management not available' AS \"Status\"",
                   True, True)

    def _cmd_distribution(self, pattern="", **_):
        """Show table distribution info."""
        if not pattern:
            yield (None, None, None, None,
                   "Please specify a table name: \\dist tablename", True, True)
            return
        safe_pattern = _sanitize_identifier(pattern)
        if not safe_pattern:
            yield (None, None, None, None,
                   f"Invalid table name: {pattern}", False, True)
            return
        query = f"""
            SELECT
                n.nspname AS "Schema",
                c.relname AS "Table",
                CASE c.relkind
                    WHEN 'r' THEN 'table'
                    WHEN 'p' THEN 'partitioned table'
                END AS "Type",
                pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) AS "Size"
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
            WHERE c.relname = '{safe_pattern}'
        """
        yield (None, None, None, None, query, True, True)

    def _cmd_encoding(self, **_):
        """Show client encoding."""
        query = "SELECT pg_client_encoding() AS \"Client Encoding\""
        yield (None, None, None, None, query, True, True)

    def pset_pager(self, value):
        """Set pager configuration."""
        self.pager_config = value
