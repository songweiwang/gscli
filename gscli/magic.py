from .main import GSCli
import sql.parse
import sql.connection
import logging

_logger = logging.getLogger(__name__)


def load_ipython_extension(ipython):
    """This is called via the ipython command '%load_ext gscli.magic'"""

    # first, load the sql magic if it isn't already loaded
    if not ipython.find_line_magic("sql"):
        ipython.run_line_magic("load_ext", "sql")

    # register our own magic
    ipython.register_magic_function(gscli_line_magic, "line", "gscli")


def gscli_line_magic(line):
    _logger.debug("gscli magic called: %r", line)
    parsed = sql.parse.parse(line, {})
    # "get" was renamed to "set" in ipython-sql:
    # https://github.com/catherinedevlin/ipython-sql/commit/f4283c65aaf68f961e84019e8b939e4a3c501d43
    if hasattr(sql.connection.Connection, "get"):
        conn = sql.connection.Connection.get(parsed["connection"])
    else:
        try:
            conn = sql.connection.Connection.set(parsed["connection"])
        # a new positional argument was added to Connection.set in version 0.4.0 of ipython-sql
        except TypeError:
            conn = sql.connection.Connection.set(parsed["connection"], False)

    try:
        # A corresponding gscli object already exists
        gscli = conn._gscli
        _logger.debug("Reusing existing gscli")
    except AttributeError:
        # I can't figure out how to get the underylying psycopg2 connection
        # from the sqlalchemy connection, so just grab the url and make a
        # new connection
        gscli = GSCli()
        u = conn.session.engine.url
        _logger.debug("New gscli: %r", str(u))

        gscli.connect(u.database, u.host, u.username, u.port, u.password)
        conn._gscli = gscli

    # For convenience, print the connection alias
    print(f"Connected: {conn.name}")

    try:
        gscli.run_cli()
    except SystemExit:
        pass

    if not gscli.query_history:
        return

    q = gscli.query_history[-1]

    if not q.successful:
        _logger.debug("Unsuccessful query - ignoring")
        return

    if q.meta_changed or q.db_changed or q.path_changed:
        _logger.debug("Dangerous query detected -- ignoring")
        return

    ipython = get_ipython()
    return ipython.run_cell_magic("sql", line, q.query)
