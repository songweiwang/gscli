import platform
import warnings

from configobj import ConfigObj, ParseError
# NamedQueries removed - gscli uses its own config-based named queries
from .config import skip_initial_comment

warnings.filterwarnings("ignore", category=UserWarning, module="psycopg2")

import atexit
import os
import re
import shlex
import sys
import traceback
import logging
import threading
import shutil
import functools
import pendulum
import datetime as dt
import itertools
import platform
from time import time, sleep
from typing import Optional
from urllib.parse import urlparse

keyring = None  # keyring will be loaded later

from cli_helpers.tabular_output import TabularOutputFormatter
from cli_helpers.tabular_output.preprocessors import align_decimals, format_numbers
from cli_helpers.utils import strip_ansi
import click

try:
    import setproctitle
except ImportError:
    setproctitle = None
from prompt_toolkit.completion import DynamicCompleter, ThreadedCompleter
from prompt_toolkit.enums import DEFAULT_BUFFER, EditingMode
from prompt_toolkit.shortcuts import PromptSession, CompleteStyle
from prompt_toolkit.document import Document
from prompt_toolkit.filters import HasFocus, IsDone
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.layout.processors import (
    ConditionalProcessor,
    HighlightMatchingBracketProcessor,
    TabsProcessor,
)
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from pygments.lexers.sql import PostgresLexer

from .gsspecial import GSSpecial, NO_QUERY, QUERY
from .namedqueries import NamedQueries, subst_args

# Pager constants
PAGER_OFF = 'off'
PAGER_LONG_OUTPUT = 'long'
from . import gsspecial as special

from .gscompleter import GSCompleter
from .gstoolbar import create_toolbar_tokens_func
from .gsstyle import style_factory, style_factory_output
from .gsexecute import GSExecute
from .completion_refresher import CompletionRefresher
from .config import (
    get_casing_file,
    load_config,
    config_location,
    ensure_dir_exists,
    get_config,
    get_config_filename,
)
from .key_bindings import gscli_bindings
from .packages.prompt_utils import confirm_destructive_query
from .__init__ import __version__

click.disable_unicode_literals_warning = True

try:
    from urlparse import urlparse, unquote, parse_qs
except ImportError:
    from urllib.parse import urlparse, unquote, parse_qs

from getpass import getuser
from psycopg2 import OperationalError, InterfaceError

# pg3: https://www.psycopg.org/psycopg3/docs/api/conninfo.html
from psycopg2.extensions import make_dsn, parse_dsn
import psycopg2

from collections import namedtuple

from textwrap import dedent

try:
    import sshtunnel

    SSH_TUNNEL_SUPPORT = True
except ImportError:
    SSH_TUNNEL_SUPPORT = False


# Ref: https://stackoverflow.com/questions/30425105/filter-special-chars-such-as-color-codes-from-shell-output
COLOR_CODE_REGEX = re.compile(r"\x1b(\[.*?[@-~]|\].*?(\x07|\x1b\\))")
DEFAULT_MAX_FIELD_WIDTH = 500

# Query tuples are used for maintaining history
MetaQuery = namedtuple(
    "Query",
    [
        "query",  # The entire text of the command
        "successful",  # True If all subqueries were successful
        "total_time",  # Time elapsed executing the query and formatting results
        "execution_time",  # Time elapsed executing the query
        "meta_changed",  # True if any subquery executed create/alter/drop
        "db_changed",  # True if any subquery changed the database
        "path_changed",  # True if any subquery changed the search path
        "mutated",  # True if any subquery executed insert/update/delete
        "is_special",  # True if the query is a special command
    ],
)
MetaQuery.__new__.__defaults__ = ("", False, 0, 0, False, False, False, False)

OutputSettings = namedtuple(
    "OutputSettings",
    "table_format dcmlfmt floatfmt missingval expanded max_width case_function style_output max_field_width",
)
OutputSettings.__new__.__defaults__ = (
    None,
    None,
    None,
    "<null>",
    False,
    None,
    lambda x: x,
    None,
    DEFAULT_MAX_FIELD_WIDTH,
)


class GsCliQuitError(Exception):
    pass


class GSCli:
    default_prompt = "\\u@\\d> "
    max_len_prompt = 30

    def set_default_pager(self, config):
        configured_pager = config["main"].get("pager")
        os_environ_pager = os.environ.get("PAGER")

        if configured_pager:
            self.logger.info(
                'Default pager found in config file: "%s"', configured_pager
            )
            os.environ["PAGER"] = configured_pager
        elif os_environ_pager:
            self.logger.info(
                'Default pager found in PAGER environment variable: "%s"',
                os_environ_pager,
            )
            os.environ["PAGER"] = os_environ_pager
        else:
            self.logger.info(
                "No default pager found in environment. Using os default pager"
            )

        # Set default set of less recommended options, if they are not already set.
        # They are ignored if pager is different than less.
        if not os.environ.get("LESS"):
            os.environ["LESS"] = "-SRXF"

    def __init__(
        self,
        force_passwd_prompt=False,
        never_passwd_prompt=False,
        gsexecute=None,
        gslirc_file=None,
        row_limit=None,
        single_connection=False,
        less_chatty=None,
        prompt=None,
        prompt_dsn=None,
        auto_vertical_output=False,
        warn=None,
        ssh_tunnel_url: Optional[str] = None,
    ):
        self.force_passwd_prompt = force_passwd_prompt
        self.never_passwd_prompt = never_passwd_prompt
        self.gsexecute = gsexecute
        self.dsn_alias = None
        self.watch_command = None

        # Load config.
        c = self.config = get_config(gslirc_file)

        # at this point, config should be written to gslirc_file if it did not exist. Read it.
        self.config_writer = load_config(get_config_filename(gslirc_file))

        # make sure to use self.config_writer, not self.config
        NamedQueries.instance = NamedQueries.from_config(self.config_writer)

        self.logger = logging.getLogger(__name__)
        self.initialize_logging()

        self.set_default_pager(c)
        self.output_file = None
        self.gsspecial = GSSpecial()

        self.multi_line = c["main"].as_bool("multi_line")
        self.multiline_mode = c["main"].get("multi_line_mode", "psql")
        self.vi_mode = c["main"].as_bool("vi")
        self.auto_expand = auto_vertical_output or c["main"].as_bool("auto_expand")
        self.expanded_output = c["main"].as_bool("expand")
        self.gsspecial.timing_enabled = c["main"].as_bool("timing")
        if row_limit is not None:
            self.row_limit = row_limit
        else:
            self.row_limit = c["main"].as_int("row_limit")

        # if not specified, set to DEFAULT_MAX_FIELD_WIDTH
        # if specified but empty, set to None to disable truncation
        # ellipsis will take at least 3 symbols, so this can't be less than 3 if specified and > 0
        max_field_width = c["main"].get("max_field_width", DEFAULT_MAX_FIELD_WIDTH)
        if max_field_width and max_field_width.lower() != "none":
            max_field_width = max(3, abs(int(max_field_width)))
        else:
            max_field_width = None
        self.max_field_width = max_field_width

        self.min_num_menu_lines = c["main"].as_int("min_num_menu_lines")
        self.multiline_continuation_char = c["main"]["multiline_continuation_char"]
        self.table_format = c["main"]["table_format"]
        self.syntax_style = c["main"]["syntax_style"]
        self.cli_style = c["colors"]
        self.wider_completion_menu = c["main"].as_bool("wider_completion_menu")
        self.destructive_warning = warn or c["main"]["destructive_warning"]
        # also handle boolean format of destructive warning
        self.destructive_warning = {"true": "all", "false": "off"}.get(
            self.destructive_warning.lower(), self.destructive_warning
        )
        self.less_chatty = bool(less_chatty) or c["main"].as_bool("less_chatty")
        self.null_string = c["main"].get("null_string", "<null>")
        self.prompt_format = (
            prompt
            if prompt is not None
            else c["main"].get("prompt", self.default_prompt)
        )
        self.prompt_dsn_format = prompt_dsn
        self.on_error = c["main"]["on_error"].upper()
        self.decimal_format = c["data_formats"]["decimal"]
        self.float_format = c["data_formats"]["float"]
        self.initialize_keyring()
        self.show_bottom_toolbar = c["main"].as_bool("show_bottom_toolbar")

        self.gsspecial.pset_pager(
            self.config["main"].as_bool("enable_pager") and "on" or "off"
        )

        self.style_output = style_factory_output(self.syntax_style, c["colors"])

        self.now = dt.datetime.today()

        self.completion_refresher = CompletionRefresher()

        self.query_history = []

        # Initialize completer
        smart_completion = c["main"].as_bool("smart_completion")
        keyword_casing = c["main"]["keyword_casing"]
        self.settings = {
            "casing_file": get_casing_file(c),
            "generate_casing_file": c["main"].as_bool("generate_casing_file"),
            "generate_aliases": c["main"].as_bool("generate_aliases"),
            "asterisk_column_order": c["main"]["asterisk_column_order"],
            "qualify_columns": c["main"]["qualify_columns"],
            "case_column_headers": c["main"].as_bool("case_column_headers"),
            "search_path_filter": c["main"].as_bool("search_path_filter"),
            "single_connection": single_connection,
            "less_chatty": less_chatty,
            "keyword_casing": keyword_casing,
        }

        completer = GSCompleter(
            smart_completion, gsspecial=self.gsspecial, settings=self.settings
        )
        self.completer = completer
        self._completer_lock = threading.Lock()
        self.register_special_commands()

        self.prompt_app = None

        self.ssh_tunnel_config = c.get("ssh tunnels")
        self.ssh_tunnel_url = ssh_tunnel_url
        self.ssh_tunnel = None

    def quit(self, **kwargs):
        raise GsCliQuitError

    def register_special_commands(self):

        self.gsspecial.register(
            self.show_help,
            "\\?",
            "\\?",
            "Show help for backslash commands.",
            arg_type=NO_QUERY,
        )

        self.gsspecial.register(
            self.change_db,
            "\\c",
            "\\c[onnect] database_name",
            "Change to a new database.",
            aliases=("use", "\\connect", "USE"),
        )

        refresh_callback = lambda *a, **kw: self.refresh_completions(persist_priorities="all")

        self.gsspecial.register(
            self.quit,
            "\\q",
            "\\q",
            "Quit gscli.",
            arg_type=NO_QUERY,
            case_sensitive=True,
            aliases=(":q",),
        )
        self.gsspecial.register(
            self.quit,
            "quit",
            "quit",
            "Quit gscli.",
            arg_type=NO_QUERY,
            case_sensitive=False,
            aliases=("exit",),
        )
        self.gsspecial.register(
            refresh_callback,
            "\\#",
            "\\#",
            "Refresh auto-completions.",
            arg_type=NO_QUERY,
        )
        self.gsspecial.register(
            refresh_callback,
            "\\refresh",
            "\\refresh",
            "Refresh auto-completions.",
            arg_type=NO_QUERY,
        )
        self.gsspecial.register(
            self.execute_from_file, "\\i", "\\i filename", "Execute commands from file."
        )
        self.gsspecial.register(
            self.write_to_file,
            "\\o",
            "\\o [filename]",
            "Send all query results to file.",
        )
        self.gsspecial.register(
            self.toggle_expanded_display,
            "\\x",
            "\\x",
            "Toggle expanded/vertical output.",
            arg_type=NO_QUERY,
        )
        self.gsspecial.register(
            self.info_connection, "\\conninfo", "\\conninfo", "Get connection details"
        )
        # 命名查询（\n 系列）
        self.gsspecial.register(
            self.named_query,
            "\\n",
            "\\n[+] [name [args...]]",
            "List, print or execute a named query.",
            arg_type=QUERY,
            aliases=("\\n+",),
        )
        self.gsspecial.register(
            self.named_query_print,
            "\\np",
            "\\np pattern",
            "Print a named query.",
            arg_type=NO_QUERY,
        )
        self.gsspecial.register(
            self.named_query_save,
            "\\ns",
            "\\ns name query",
            "Save a named query.",
            arg_type=NO_QUERY,
        )
        self.gsspecial.register(
            self.named_query_delete,
            "\\nd",
            "\\nd name",
            "Delete a named query.",
            arg_type=NO_QUERY,
        )
        self.gsspecial.register(
            self.change_table_format,
            "\\T",
            "\\T [format]",
            "Change the table format used to output results",
        )

        # Editor command (handled interactively by handle_editor_command)
        self.gsspecial.register(
            self._cmd_editor_help,
            "\\e", "\\e [filename]",
            "Edit query or file in external editor.",
            arg_type=NO_QUERY,
        )
        # Watch command (handled interactively by handle_watch_command)
        self.gsspecial.register(
            self._cmd_watch_help,
            "\\watch", "\\watch [seconds]",
            "Execute query repeatedly (default every 2s).",
            arg_type=NO_QUERY,
        )

    def _cmd_editor_help(self, **_):
        yield (None, None, None,
               "\\e is only available in interactive mode.")

    def _cmd_watch_help(self, **_):
        yield (None, None, None,
               "\\watch is only available in interactive mode.")

    def toggle_expanded_display(self, **_):
        """Toggle expanded/vertical output mode."""
        self.gsspecial.expanded_output = not self.gsspecial.expanded_output
        state = "on" if self.gsspecial.expanded_output else "off"
        yield (None, None, None, f"Expanded display is {state}.")

    def show_help(self, **_):
        """Show all backslash commands with descriptions."""
        seen = set()
        unique = []
        for cmd in self.gsspecial.commands.values():
            if id(cmd) in seen:
                continue
            seen.add(id(cmd))
            unique.append(cmd)
        unique.sort(key=lambda c: c.command.replace("\\", "").lower())
        lines = ["General", "-------"]
        for cmd in unique:
            lines.append(f"  {cmd.syntax:<40s} {cmd.description}")
        lines.append("")
        yield (None, None, None, "\n".join(lines))

    # ---- 命名查询（\n 系列） ----

    def _named_query_list(self, verbose=False):
        """列出全部命名查询；verbose 时带 SQL 定义。"""
        nq = NamedQueries.instance
        names = nq.list()
        if not names:
            yield (None, None, None, NamedQueries.usage)
            return
        if verbose:
            width = max(len(n) for n in names)
            lines = ["%-*s  %s" % (width, n, nq.get(n)) for n in names]
        else:
            lines = list(names)
        yield (None, None, None, "\n".join(lines))

    def named_query(self, pattern="", verbose=False, **_):
        """\\n：无参列出；带名字执行；\\n+ 列出定义 / 打印定义。"""
        pattern = (pattern or "").strip()
        if not pattern:
            yield from self._named_query_list(verbose)
            return

        params = shlex.split(pattern)
        name = params.pop(0) if params else ""
        if not name:
            yield from self._named_query_list(verbose)
            return

        query = NamedQueries.instance.get(name)
        if query is None:
            yield (None, None, None, "No named query: %s" % name)
            return

        sql, leftovers = subst_args(query, params)
        if leftovers:
            yield (
                None,
                None,
                None,
                "Too many arguments for '%s': %s" % (name, " ".join(leftovers)),
            )
            return
        if verbose:
            # \n+ 名字：只看不跑
            yield (None, None, None, "> %s" % sql)
            return
        # 交给执行器去跑（第 5 项是 SQL，后两项 success/is_special）
        yield (None, None, None, None, sql, True, True)

    def named_query_print(self, pattern="", **_):
        """\\np：按正则打印命名查询。"""
        pattern = (pattern or "").strip()
        if not pattern:
            yield (None, None, None, "Syntax: \\np pattern.\n\n" + NamedQueries.usage)
            return
        rows = NamedQueries.instance.match(pattern)
        if not rows:
            yield (None, None, None, "No match found")
            return
        width = max(len(n) for n, _ in rows)
        yield (
            None,
            None,
            None,
            "\n".join("%-*s  %s" % (width, n, q) for n, q in rows),
        )

    def named_query_save(self, pattern="", **_):
        """\\ns 名字 SQL：保存。"""
        pattern = (pattern or "").strip()
        name, _, query = pattern.partition(" ")
        name, query = name.strip(), query.strip()
        if not name or not query:
            yield (None, None, None, "Syntax: \\ns name query.\n\n" + NamedQueries.usage)
            return
        NamedQueries.instance.save(name, query)
        yield (None, None, None, "%s: Saved." % name)

    def named_query_delete(self, pattern="", **_):
        """\\nd 名字：删除。"""
        names = shlex.split((pattern or "").strip())
        if not names:
            yield (None, None, None, "Syntax: \\nd name.\n\n" + NamedQueries.usage)
            return
        out = []
        for name in names:
            if NamedQueries.instance.delete(name):
                out.append("%s: Deleted" % name)
            else:
                out.append("%s: Not Found." % name)
        yield (None, None, None, "\n".join(out))

    def change_table_format(self, pattern, **_):
        try:
            if pattern not in TabularOutputFormatter().supported_formats:
                raise ValueError()
            self.table_format = pattern
            yield (None, None, None, f"Changed table format to {pattern}")
        except ValueError:
            msg = f"Table format {pattern} not recognized. Allowed formats:"
            for table_type in TabularOutputFormatter().supported_formats:
                msg += f"\n\t{table_type}"
            msg += "\nCurrently set to: %s" % self.table_format
            yield (None, None, None, msg)

    def info_connection(self, **_):
        if self.gsexecute.host.startswith("/"):
            host = 'socket "%s"' % self.gsexecute.host
        else:
            host = 'host "%s"' % self.gsexecute.host

        yield (
            None,
            None,
            None,
            'You are connected to database "%s" as user '
            '"%s" on %s at port "%s".'
            % (self.gsexecute.dbname, self.gsexecute.user, host, self.gsexecute.port),
        )

    def change_db(self, pattern, **_):
        if pattern:
            # Get all the parameters in pattern, handling double quotes if any.
            infos = re.findall(r'"[^"]*"|[^"\'\s]+', pattern)
            # Now removing quotes.
            list(map(lambda s: s.strip('"'), infos))

            infos.extend([None] * (4 - len(infos)))
            db, user, host, port = infos
            try:
                self.gsexecute.connect(
                    database=db,
                    user=user,
                    host=host,
                    port=port,
                    **self.gsexecute.extra_args,
                )
            except OperationalError as e:
                click.secho(str(e), err=True, fg="red")
                click.echo("Previous connection kept")
        else:
            self.gsexecute.connect()

        yield (
            None,
            None,
            None,
            'You are now connected to database "%s" as '
            'user "%s"' % (self.gsexecute.dbname, self.gsexecute.user),
        )

    def execute_from_file(self, pattern, **_):
        if not pattern:
            message = "\\i: missing required argument"
            return [(None, None, None, message, "", False, True)]
        try:
            with open(os.path.expanduser(pattern), encoding="utf-8") as f:
                query = f.read()
        except OSError as e:
            return [(None, None, None, str(e), "", False, True)]

        if (
            self.destructive_warning != "off"
            and confirm_destructive_query(query, self.destructive_warning) is False
        ):
            message = "Wise choice. Command execution stopped."
            return [(None, None, None, message)]

        on_error_resume = self.on_error == "RESUME"
        return self.gsexecute.run(
            query, self.gsspecial, on_error_resume=on_error_resume
        )

    def write_to_file(self, pattern, **_):
        if not pattern:
            self.output_file = None
            message = "File output disabled"
            return [(None, None, None, message, "", True, True)]
        filename = os.path.abspath(os.path.expanduser(pattern))
        if not os.path.isfile(filename):
            try:
                open(filename, "w").close()
            except OSError as e:
                self.output_file = None
                message = str(e) + "\nFile output disabled"
                return [(None, None, None, message, "", False, True)]
        self.output_file = filename
        message = 'Writing to file "%s"' % self.output_file
        return [(None, None, None, message, "", True, True)]

    # ── Editor & Watch helpers ────────────────────────────────────────

    @staticmethod
    def _editor_command(text):
        """Check if text starts with \\e (external editor command).

        Must check that the next character after \\e is not alphabetic,
        to avoid false-matching commands like \\encoding.
        """
        if not text:
            return None
        stripped = text.strip()
        if stripped.startswith('\\e'):
            rest = stripped[2:]
            if not rest or not rest[0].isalpha():
                return '\\e'
        return None

    @staticmethod
    def _get_editor_filename(text):
        """Extract filename from \\e command."""
        parts = text.strip().split(None, 1)
        if len(parts) > 1 and not parts[1].startswith('\\'):
            return parts[1]
        return None

    @staticmethod
    def _get_editor_query(text):
        """Extract the query part after \\e."""
        parts = text.strip().split(None, 1)
        if len(parts) > 1:
            return parts[1]
        return None

    @staticmethod
    def _open_external_editor(filename=None, sql=None):
        """Open an external editor for SQL editing."""
        import tempfile
        import subprocess

        if filename:
            try:
                with open(filename, 'r') as f:
                    return f.read(), None
            except OSError as e:
                return None, str(e)

        editor = os.environ.get('EDITOR', 'vi')
        with tempfile.NamedTemporaryFile(suffix='.sql', mode='w', delete=False) as f:
            if sql:
                f.write(sql)
            tmpfile = f.name

        try:
            subprocess.call([editor, tmpfile])
            with open(tmpfile, 'r') as f:
                return f.read(), None
        except Exception as e:
            return None, str(e)
        finally:
            try:
                os.unlink(tmpfile)
            except OSError:
                pass

    @staticmethod
    def _get_watch_command(text):
        """Parse \\watch command. Returns (command, seconds) or (None, None)."""
        if '\\watch' not in text:
            return None, None
        parts = text.split('\\watch')
        command = parts[0].strip()
        timing_part = parts[1].strip() if len(parts) > 1 else ""
        # Parse timing: default 2 seconds
        seconds = 2
        if timing_part:
            try:
                seconds = int(timing_part)
            except ValueError:
                pass
        return command if command else None, seconds

    def initialize_logging(self):

        log_file = self.config["main"]["log_file"]
        if log_file == "default":
            log_file = config_location() + "log"
        ensure_dir_exists(log_file)
        log_level = self.config["main"]["log_level"]

        # Disable logging if value is NONE by switching to a no-op handler.
        # Set log level to a high value so it doesn't even waste cycles getting called.
        if log_level.upper() == "NONE":
            handler = logging.NullHandler()
        else:
            handler = logging.FileHandler(os.path.expanduser(log_file))

        level_map = {
            "CRITICAL": logging.CRITICAL,
            "ERROR": logging.ERROR,
            "WARNING": logging.WARNING,
            "INFO": logging.INFO,
            "DEBUG": logging.DEBUG,
            "NONE": logging.CRITICAL,
        }

        log_level = level_map[log_level.upper()]

        formatter = logging.Formatter(
            "%(asctime)s (%(process)d/%(threadName)s) "
            "%(name)s %(levelname)s - %(message)s"
        )

        handler.setFormatter(formatter)

        root_logger = logging.getLogger("gscli")
        root_logger.addHandler(handler)
        root_logger.setLevel(log_level)

        root_logger.debug("Initializing gscli logging.")
        root_logger.debug("Log file %r.", log_file)

        gs_logger = logging.getLogger("gsspecial")
        gs_logger.addHandler(handler)
        gs_logger.setLevel(log_level)

    def initialize_keyring(self):
        global keyring

        keyring_enabled = self.config["main"].as_bool("keyring")
        if keyring_enabled:
            # Try best to load keyring (issue #1041).
            import importlib

            try:
                keyring = importlib.import_module("keyring")
            except Exception as e:  # ImportError for Python 2, ModuleNotFoundError for Python 3
                self.logger.warning("import keyring failed: %r.", e)

    def connect_dsn(self, dsn, **kwargs):
        self.connect(dsn=dsn, **kwargs)

    def connect_service(self, service, user):
        service_config, file = parse_service_info(service)
        if service_config is None:
            click.secho(
                f"service '{service}' was not found in {file}", err=True, fg="red"
            )
            sys.exit(1)
        self.connect(
            database=service_config.get("dbname"),
            host=service_config.get("host"),
            user=user or service_config.get("user"),
            port=service_config.get("port"),
            passwd=service_config.get("password"),
        )

    def connect_uri(self, uri, **kwargs):
        kwargs_uri = psycopg2.extensions.parse_dsn(uri)
        remap = {"dbname": "database", "password": "passwd"}
        kwargs_uri = {remap.get(k, k): v for k, v in kwargs_uri.items()}
        kwargs_uri.update(kwargs)
        self.connect(**kwargs_uri)

    def connect(
        self, database="", host="", user="", port="", passwd="", dsn="",
        sslmode="", sslcert="", sslkey="", sslrootcert="", **kwargs
    ):
        # Connect to the database.

        if not user:
            user = getuser()

        if not database:
            database = user

        kwargs.setdefault("application_name", "gscli")

        # GaussDB SSL connection parameters
        # Ref: https://support.huaweicloud.com/centralized-devg-v8-gaussdb/gaussdb-42-0176.html
        # sslmode: disable, allow, prefer, require, verify-ca, verify-full
        # sslcert: client certificate path
        # sslkey: client key path
        # sslrootcert: root certificate path
        if sslmode:
            kwargs["sslmode"] = sslmode
        if sslcert:
            kwargs["sslcert"] = sslcert
        if sslkey:
            kwargs["sslkey"] = sslkey
        if sslrootcert:
            kwargs["sslrootcert"] = sslrootcert

        # If password prompt is not forced but no password is provided, try
        # getting it from environment variable.
        if not self.force_passwd_prompt and not passwd:
            passwd = os.environ.get("GSPASSWORD", "")

        # Find password from store
        key = f"{user}@{host}"
        keyring_error_message = dedent(
            """\
            {}
            {}
            To remove this message do one of the following:
            - prepare keyring as described at: https://keyring.readthedocs.io/en/stable/
            - uninstall keyring: pip uninstall keyring
            - disable keyring in our configuration: add keyring = False to [main]"""
        )

        # Prompt for a password immediately if requested via the -W flag. This
        # avoids wasting time trying to connect to the database and catching a
        # no-password exception.
        # If we successfully parsed a password from a URI, there's no need to
        # prompt for it, even with the -W flag
        if self.force_passwd_prompt and not passwd:
            passwd = click.prompt(
                "Password for %s" % user, hide_input=True, show_default=False, type=str
            )

        if not passwd and keyring:

            try:
                passwd = keyring.get_password("gscli", key)
            except (RuntimeError, keyring.errors.InitError) as e:
                click.secho(
                    keyring_error_message.format(
                        "Load your password from keyring returned:", str(e)
                    ),
                    err=True,
                    fg="red",
                )

        def should_ask_for_password(exc):
            # Prompt for a password after 1st attempt to connect
            # fails. Don't prompt if the -w flag is supplied
            if self.never_passwd_prompt:
                return False
            error_msg = exc.args[0]
            if "no password supplied" in error_msg:
                return True
            if "password authentication failed" in error_msg:
                return True
            return False

        if dsn:
            parsed_dsn = parse_dsn(dsn)
            if "host" in parsed_dsn:
                host = parsed_dsn["host"]
            if "port" in parsed_dsn:
                port = parsed_dsn["port"]

        if self.ssh_tunnel_config and not self.ssh_tunnel_url:
            for db_host_regex, tunnel_url in self.ssh_tunnel_config.items():
                if re.search(db_host_regex, host):
                    self.ssh_tunnel_url = tunnel_url
                    break

        if self.ssh_tunnel_url:
            # We add the protocol as urlparse doesn't find it by itself
            if "://" not in self.ssh_tunnel_url:
                self.ssh_tunnel_url = f"ssh://{self.ssh_tunnel_url}"

            # An SSH tunnel exposes exactly one remote endpoint, so a multi-host
            # / multi-port connection cannot be tunneled as-is.
            if "," in str(host) or "," in str(port):
                click.secho(
                    "Warning: SSH tunnel supports a single host/port, "
                    "using %s and %s only." % (str(host).split(",")[0], str(port).split(",")[0]),
                    err=True,
                    fg="yellow",
                )
                host = str(host).split(",")[0]
                port = str(port).split(",")[0]

            tunnel_info = urlparse(self.ssh_tunnel_url)
            params = {
                "local_bind_address": ("127.0.0.1",),
                "remote_bind_address": (host, int(port or 5432)),
                "ssh_address_or_host": (tunnel_info.hostname, tunnel_info.port or 22),
                "logger": self.logger,
            }
            if tunnel_info.username:
                params["ssh_username"] = tunnel_info.username
            if tunnel_info.password:
                params["ssh_password"] = tunnel_info.password

            # Hack: sshtunnel adds a console handler to the logger, so we revert handlers.
            # We can remove this when https://github.com/pahaz/sshtunnel/pull/250 is merged.
            logger_handlers = self.logger.handlers.copy()
            try:
                self.ssh_tunnel = sshtunnel.SSHTunnelForwarder(**params)
                self.ssh_tunnel.start()
            except Exception as e:
                self.logger.handlers = logger_handlers
                self.logger.error("traceback: %r", traceback.format_exc())
                click.secho(str(e), err=True, fg="red")
                sys.exit(1)
            self.logger.handlers = logger_handlers

            atexit.register(self.ssh_tunnel.stop)
            host = "127.0.0.1"
            port = self.ssh_tunnel.local_bind_ports[0]

            if dsn:
                dsn = make_dsn(dsn, host=host, port=port)

        # Attempt to connect to the database.
        # Note that passwd may be empty on the first attempt. If connection
        # fails because of a missing or incorrect password, but we're allowed to
        # prompt for a password (no -w flag), prompt for a passwd and try again.
        try:
            try:
                gsexecute = GSExecute(database, user, passwd, host, port, dsn, **kwargs)
            except (OperationalError, InterfaceError) as e:
                if should_ask_for_password(e):
                    passwd = click.prompt(
                        "Password for %s" % user,
                        hide_input=True,
                        show_default=False,
                        type=str,
                    )
                    gsexecute = GSExecute(
                        database, user, passwd, host, port, dsn, **kwargs
                    )
                else:
                    raise e
            if passwd and keyring:
                try:
                    keyring.set_password("gscli", key, passwd)
                except (RuntimeError, keyring.errors.KeyringError) as e:
                    click.secho(
                        keyring_error_message.format(
                            "Set password in keyring returned:", str(e)
                        ),
                        err=True,
                        fg="red",
                    )

        except Exception as e:  # Connecting to a database could fail.
            self.logger.debug("Database connection failed: %r.", e)
            self.logger.error("traceback: %r", traceback.format_exc())
            click.secho(str(e), err=True, fg="red")
            error_msg = str(e).lower()
            if "no password supplied" in error_msg or "password authentication failed" in error_msg:
                if os.environ.get("GS_DRIVER_PATH"):
                    click.secho(
                        "Hint: GS_DRIVER_PATH is set but the GaussDB driver may not be loaded. "
                        "Try running with the driver preloaded:\n"
                        "  eval $(python -c 'import os; print(\"LD_LIBRARY_PATH=\" + os.path.join(os.environ[\"GS_DRIVER_PATH\"], \"lib\"))')",
                        err=True, fg="yellow",
                    )
                else:
                    click.secho(
                        "Hint: If connecting to GaussDB with SHA256 authentication, "
                        "set GS_DRIVER_PATH to the GaussDB official psycopg2 driver:\n"
                        "  export GS_DRIVER_PATH=/tmp/gaussdb_conn",
                        err=True, fg="yellow",
                    )
            sys.exit(1)

        self.gsexecute = gsexecute

    def handle_editor_command(self, text):
        r"""
        Editor command is any query that is prefixed or suffixed
        by a '\e'. The reason for a while loop is because a user
        might edit a query multiple times.
        For eg:
        "select * from \e"<enter> to edit it in vim, then come
        back to the prompt with the edited query "select * from
        blah where q = 'abc'\e" to edit it again.
        :param text: Document
        :return: Document
        """
        editor_command = self._editor_command(text)
        while editor_command:
            filename = self._get_editor_filename(text)
            query = self._get_editor_query(text) or self.get_last_query()
            sql, message = self._open_external_editor(filename, sql=query)
            if message:
                # Something went wrong. Raise an exception and bail.
                raise RuntimeError(message)
            while True:
                try:
                    text = self.prompt_app.prompt(default=sql)
                    break
                except KeyboardInterrupt:
                    sql = ""

            editor_command = self._editor_command(text)
        return text

    def execute_command(self, text):
        logger = self.logger

        query = MetaQuery(query=text, successful=False)

        try:
            if self.destructive_warning != "off":
                destroy = confirm = confirm_destructive_query(
                    text, self.destructive_warning
                )
                if destroy is False:
                    click.secho("Wise choice!")
                    raise KeyboardInterrupt
                elif destroy:
                    click.secho("Your call!")
            output, query = self._evaluate_command(text)
        except KeyboardInterrupt:
            # Restart connection to the database
            self.gsexecute.connect()
            logger.debug("cancelled query, sql: %r", text)
            click.secho("cancelled query", err=True, fg="red")
        except NotImplementedError:
            click.secho("Not Yet Implemented.", fg="yellow")
        except OperationalError as e:
            logger.error("sql: %r, error: %r", text, e)
            logger.error("traceback: %r", traceback.format_exc())
            self._handle_server_closed_connection(text)
        except (GsCliQuitError, EOFError) as e:
            raise
        except Exception as e:
            logger.error("sql: %r, error: %r", text, e)
            logger.error("traceback: %r", traceback.format_exc())
            click.secho(str(e), err=True, fg="red")
        else:
            try:
                if self.output_file and not text.startswith(("\\o ", "\\? ")):
                    try:
                        with open(self.output_file, "a", encoding="utf-8") as f:
                            click.echo(text, file=f)
                            click.echo("\n".join(output), file=f)
                            click.echo("", file=f)  # extra newline
                    except OSError as e:
                        click.secho(str(e), err=True, fg="red")
                else:
                    if output:
                        self.echo_via_pager("\n".join(output))
            except KeyboardInterrupt:
                pass

            if self.gsspecial.timing_enabled:
                # Only add humanized time display if > 1 second
                if query.total_time > 1:
                    print(
                        "Time: %0.03fs (%s), executed in: %0.03fs (%s)"
                        % (
                            query.total_time,
                            pendulum.Duration(seconds=query.total_time).in_words(),
                            query.execution_time,
                            pendulum.Duration(seconds=query.execution_time).in_words(),
                        )
                    )
                else:
                    print("Time: %0.03fs" % query.total_time)

            # Check if we need to update completions, in order of most
            # to least drastic changes
            if query.db_changed:
                with self._completer_lock:
                    self.completer.reset_completions()
                self.refresh_completions(persist_priorities="keywords")
            elif query.meta_changed:
                self.refresh_completions(persist_priorities="all")
            elif query.path_changed:
                logger.debug("Refreshing search path")
                with self._completer_lock:
                    self.completer.set_search_path(self.gsexecute.search_path())
                logger.debug("Search path: %r", self.completer.search_path)
        return query

    def run_cli(self):
        logger = self.logger

        history_file = self.config["main"]["history_file"]
        if history_file == "default":
            history_file = config_location() + "history"
        history = FileHistory(os.path.expanduser(history_file))
        self.refresh_completions(history=history, persist_priorities="none")

        self.prompt_app = self._build_cli(history)

        if not self.less_chatty:
            print("Server: GaussDB", self.gsexecute.server_version)
            print("Version:", __version__)
            print("Home: https://github.com/songweiwang/gscli")

        try:
            while True:
                try:
                    text = self.prompt_app.prompt()
                except KeyboardInterrupt:
                    continue

                try:
                    text = self.handle_editor_command(text)
                except RuntimeError as e:
                    logger.error("sql: %r, error: %r", text, e)
                    logger.error("traceback: %r", traceback.format_exc())
                    click.secho(str(e), err=True, fg="red")
                    continue

                self.handle_watch_command(text)

                self.now = dt.datetime.today()

                # Allow GSCompleter to learn user's preferred keywords, etc.
                with self._completer_lock:
                    self.completer.extend_query_history(text)

        except (GsCliQuitError, EOFError):
            if not self.less_chatty:
                print("Goodbye!")

    def handle_watch_command(self, text):
        # Initialize default metaquery in case execution fails
        self.watch_command, timing = self._get_watch_command(text)

        # If we run \watch without a command, apply it to the last query run.
        if self.watch_command is not None and not self.watch_command.strip():
            try:
                self.watch_command = self.query_history[-1].query
            except IndexError:
                click.secho(
                    "\\watch cannot be used with an empty query", err=True, fg="red"
                )
                self.watch_command = None

        # If there's a command to \watch, run it in a loop.
        if self.watch_command:
            while self.watch_command:
                try:
                    query = self.execute_command(self.watch_command)
                    click.echo(f"Waiting for {timing} seconds before repeating")
                    sleep(timing)
                except KeyboardInterrupt:
                    self.watch_command = None

        # Otherwise, execute it as a regular command.
        else:
            query = self.execute_command(text)

        self.query_history.append(query)

    def _build_cli(self, history):
        key_bindings = gscli_bindings(self)

        def get_message():
            if self.dsn_alias and self.prompt_dsn_format is not None:
                prompt_format = self.prompt_dsn_format
            else:
                prompt_format = self.prompt_format

            prompt = self.get_prompt(prompt_format)

            if (
                prompt_format == self.default_prompt
                and len(prompt) > self.max_len_prompt
            ):
                prompt = self.get_prompt("\\d> ")

            prompt = prompt.replace("\\x1b", "\x1b")
            return ANSI(prompt)

        def get_continuation(width, line_number, is_soft_wrap):
            continuation = self.multiline_continuation_char * (width - 1) + " "
            return [("class:continuation", continuation)]

        get_toolbar_tokens = create_toolbar_tokens_func(self)

        if self.wider_completion_menu:
            complete_style = CompleteStyle.MULTI_COLUMN
        else:
            complete_style = CompleteStyle.COLUMN

        with self._completer_lock:
            prompt_app = PromptSession(
                lexer=PygmentsLexer(PostgresLexer),
                reserve_space_for_menu=self.min_num_menu_lines,
                message=get_message,
                prompt_continuation=get_continuation,
                bottom_toolbar=get_toolbar_tokens if self.show_bottom_toolbar else None,
                complete_style=complete_style,
                input_processors=[
                    # Highlight matching brackets while editing.
                    ConditionalProcessor(
                        processor=HighlightMatchingBracketProcessor(chars="[](){}"),
                        filter=HasFocus(DEFAULT_BUFFER) & ~IsDone(),
                    ),
                    # Render \t as 4 spaces instead of "^I"
                    TabsProcessor(char1=" ", char2=" "),
                ],
                auto_suggest=AutoSuggestFromHistory(),
                tempfile_suffix=".sql",
                # N.b. gscli's multi-line mode controls submit-on-Enter (which
                # overrides the default behaviour of prompt_toolkit) and is
                # distinct from prompt_toolkit's multiline mode here, which
                # controls layout/display of the prompt/buffer
                multiline=True,
                history=history,
                completer=ThreadedCompleter(DynamicCompleter(lambda: self.completer)),
                complete_while_typing=True,
                style=style_factory(self.syntax_style, self.cli_style),
                include_default_pygments_style=False,
                key_bindings=key_bindings,
                enable_open_in_editor=True,
                enable_system_prompt=True,
                enable_suspend=True,
                editing_mode=EditingMode.VI if self.vi_mode else EditingMode.EMACS,
                search_ignore_case=True,
            )

            return prompt_app

    def _should_limit_output(self, sql, cur):
        """returns True if the output should be truncated, False otherwise."""
        if not is_select(sql):
            return False

        return (
            not self._has_limit(sql)
            and self.row_limit != 0
            and cur
            and cur.rowcount > self.row_limit
        )

    def _has_limit(self, sql):
        if not sql:
            return False
        return "limit " in sql.lower()

    def _limit_output(self, cur):
        limit = min(self.row_limit, cur.rowcount)
        new_cur = itertools.islice(cur, limit)
        new_status = "SELECT " + str(limit)
        click.secho("The result was limited to %s rows" % limit, fg="red")

        return new_cur, new_status

    def _evaluate_command(self, text):
        """Used to run a command entered by the user during CLI operation
        (Puts the E in REPL)

        returns (results, MetaQuery)
        """
        logger = self.logger
        logger.debug("sql: %r", text)

        all_success = True
        meta_changed = False  # CREATE, ALTER, DROP, etc
        mutated = False  # INSERT, DELETE, etc
        db_changed = False
        path_changed = False
        output = []
        total = 0
        execution = 0

        # Run the query.
        start = time()
        on_error_resume = self.on_error == "RESUME"
        res = self.gsexecute.run(
            text, self.gsspecial, exception_formatter, on_error_resume
        )

        is_special = None

        for title, cur, headers, status, sql, success, is_special in res:
            logger.debug("headers: %r", headers)
            logger.debug("rows: %r", cur)
            logger.debug("status: %r", status)

            if self._should_limit_output(sql, cur):
                cur, status = self._limit_output(cur)

            if self.gsspecial.auto_expand or self.auto_expand:
                max_width = self.prompt_app.output.get_size().columns
            else:
                max_width = None

            expanded = self.gsspecial.expanded_output or self.expanded_output
            settings = OutputSettings(
                table_format=self.table_format,
                dcmlfmt=self.decimal_format,
                floatfmt=self.float_format,
                missingval=self.null_string,
                expanded=expanded,
                max_width=max_width,
                case_function=(
                    self.completer.case
                    if self.settings["case_column_headers"]
                    else lambda x: x
                ),
                style_output=self.style_output,
                max_field_width=self.max_field_width,
            )
            execution = time() - start
            formatted = format_output(title, cur, headers, status, settings)

            output.extend(formatted)
            total = time() - start

            # Keep track of whether any of the queries are mutating or changing
            # the database
            if success:
                mutated = mutated or is_mutating(status)
                db_changed = db_changed or has_change_db_cmd(sql)
                meta_changed = meta_changed or has_meta_cmd(sql)
                path_changed = path_changed or has_change_path_cmd(sql)
            else:
                all_success = False

        meta_query = MetaQuery(
            text,
            all_success,
            total,
            execution,
            meta_changed,
            db_changed,
            path_changed,
            mutated,
            is_special,
        )

        return output, meta_query

    def _handle_server_closed_connection(self, text):
        """Used during CLI execution."""
        try:
            click.secho("Reconnecting...", fg="green")
            self.gsexecute.connect()
            click.secho("Reconnected!", fg="green")
            self.execute_command(text)
        except OperationalError as e:
            click.secho("Reconnect Failed", fg="red")
            click.secho(str(e), err=True, fg="red")

    def refresh_completions(self, history=None, persist_priorities="all"):
        """Refresh outdated completions

        :param history: A prompt_toolkit.history.FileHistory object. Used to
                        load keyword and identifier preferences

        :param persist_priorities: 'all' or 'keywords'
        """

        callback = functools.partial(
            self._on_completions_refreshed, persist_priorities=persist_priorities
        )
        return self.completion_refresher.refresh(
            self.gsexecute,
            self.gsspecial,
            callback,
            history=history,
            settings=self.settings,
        )

    def _on_completions_refreshed(self, new_completer, persist_priorities):
        self._swap_completer_objects(new_completer, persist_priorities)

        if self.prompt_app:
            # After refreshing, redraw the CLI to clear the statusbar
            # "Refreshing completions..." indicator
            self.prompt_app.app.invalidate()

    def _swap_completer_objects(self, new_completer, persist_priorities):
        """Swap the completer object with the newly created completer.

        persist_priorities is a string specifying how the old completer's
        learned prioritizer should be transferred to the new completer.

          'none'     - The new prioritizer is left in a new/clean state

          'all'      - The new prioritizer is updated to exactly reflect
                       the old one

          'keywords' - The new prioritizer is updated with old keyword
                       priorities, but not any other.

        """
        with self._completer_lock:
            old_completer = self.completer
            self.completer = new_completer

            if persist_priorities == "all":
                # Just swap over the entire prioritizer
                new_completer.prioritizer = old_completer.prioritizer
            elif persist_priorities == "keywords":
                # Swap over the entire prioritizer, but clear name priorities,
                # leaving learned keyword priorities alone
                new_completer.prioritizer = old_completer.prioritizer
                new_completer.prioritizer.clear_names()
            elif persist_priorities == "none":
                # Leave the new prioritizer as is
                pass
            self.completer = new_completer

    def get_completions(self, text, cursor_positition):
        with self._completer_lock:
            return self.completer.get_completions(
                Document(text=text, cursor_position=cursor_positition), None
            )

    def get_prompt(self, string):
        # should be before replacing \\d
        string = string.replace("\\dsn_alias", self.dsn_alias or "")
        string = string.replace("\\t", self.now.strftime("%x %X"))
        string = string.replace("\\u", self.gsexecute.user or "(none)")
        string = string.replace("\\H", self.gsexecute.host or "(none)")
        string = string.replace("\\h", self.gsexecute.short_host or "(none)")
        string = string.replace("\\d", self.gsexecute.dbname or "(none)")
        string = string.replace(
            "\\p",
            str(self.gsexecute.port) if self.gsexecute.port is not None else "5432",
        )
        string = string.replace("\\i", str(self.gsexecute.pid) or "(none)")
        string = string.replace("\\#", "#" if self.gsexecute.superuser else ">")
        string = string.replace("\\n", "\n")
        return string

    def get_last_query(self):
        """Get the last query executed or None."""
        return self.query_history[-1][0] if self.query_history else None

    def is_too_wide(self, line):
        """Will this line be too wide to fit into terminal?"""
        if not self.prompt_app:
            return False
        return (
            len(COLOR_CODE_REGEX.sub("", line))
            > self.prompt_app.output.get_size().columns
        )

    def is_too_tall(self, lines):
        """Are there too many lines to fit into terminal?"""
        if not self.prompt_app:
            return False
        return len(lines) >= (self.prompt_app.output.get_size().rows - 4)

    def echo_via_pager(self, text, color=None):
        if self.gsspecial.pager_config == PAGER_OFF or self.watch_command:
            click.echo(text, color=color)
        elif (
            self.gsspecial.pager_config == PAGER_LONG_OUTPUT
            and self.table_format != "csv"
        ):
            lines = text.split("\n")

            # The last 4 lines are reserved for the gscli menu and padding
            if self.is_too_tall(lines) or any(self.is_too_wide(l) for l in lines):
                click.echo_via_pager(text, color=color)
            else:
                click.echo(text, color=color)
        else:
            click.echo_via_pager(text, color)


def _validate_port(ctx, param, value):
    """Accept a single port or a comma separated list for multi-host connections.

    libpq matches host and port lists element by element, e.g.
    host="h1,h2,h3" port="8000,8001,8002". A single port is applied to all hosts.
    """
    if value is None:
        return value
    port_str = str(value).strip()
    if not port_str:
        return port_str
    for item in port_str.split(","):
        if not item.strip().isdigit() or not 0 < int(item) < 65536:
            raise click.BadParameter(
                "%r is not a valid port list. Use e.g. 8000 or 8000,8001,8002."
                % value
            )
    return port_str


@click.command()
# Default host is '' so psycopg2 can default to either localhost or unix socket
@click.option(
    "-h",
    "--host",
    default="",
    envvar="GSHOST",
    help="Host address of the gaussdb database. "
    "Multiple hosts may be given as a comma separated list, e.g. h1,h2,h3.",
)
@click.option(
    "-p",
    "--port",
    default="5432",
    callback=_validate_port,
    help="Port number at which the GaussDB instance is listening. "
    "For multi-host connections use a comma separated list, e.g. 8000,8001,8002.",
    envvar="GSPORT",
)
@click.option(
    "-U",
    "--username",
    "username_opt",
    help="Username to connect to the gaussdb database.",
)
@click.option(
    "-u", "--user", "username_opt", help="Username to connect to the gaussdb database."
)
@click.option(
    "-W",
    "--password",
    "prompt_passwd",
    is_flag=True,
    default=False,
    help="Force password prompt.",
)
@click.option(
    "-w",
    "--no-password",
    "never_prompt",
    is_flag=True,
    default=False,
    help="Never prompt for password.",
)
@click.option(
    "--single-connection",
    "single_connection",
    is_flag=True,
    default=False,
    help="Do not use a separate connection for completions.",
)
@click.option("-v", "--version", is_flag=True, help="Version of gscli.")
@click.option("-d", "--dbname", "dbname_opt", help="database name to connect to.")
@click.option(
    "--gslirc",
    default=config_location() + "config",
    envvar="GSLIRC",
    help="Location of gslirc file.",
    type=click.Path(dir_okay=False),
)
@click.option(
    "-D",
    "--dsn",
    default="",
    envvar="DSN",
    help="Use DSN configured into the [alias_dsn] section of gslirc file.",
)
@click.option(
    "--list-dsn",
    "list_dsn",
    is_flag=True,
    help="list of DSN configured into the [alias_dsn] section of gslirc file.",
)
@click.option(
    "--row-limit",
    default=None,
    envvar="GSROWLIMIT",
    type=click.INT,
    help="Set threshold for row limit prompt. Use 0 to disable prompt.",
)
@click.option(
    "--less-chatty",
    "less_chatty",
    is_flag=True,
    default=False,
    help="Skip intro on startup and goodbye on exit.",
)
@click.option("--prompt", help='Prompt format (Default: "\\u@\\d> ").')
@click.option(
    "--prompt-dsn",
    help='Prompt format for connections using DSN aliases (Default: "\\u@\\d> ").',
)
@click.option(
    "-l",
    "--list",
    "list_databases",
    is_flag=True,
    help="list available databases, then exit.",
)
@click.option(
    "--auto-vertical-output",
    is_flag=True,
    help="Automatically switch to vertical output mode if the result is wider than the terminal width.",
)
@click.option(
    "--warn",
    default=None,
    type=click.Choice(["all", "moderate", "off"]),
    help="Warn before running a destructive query.",
)
@click.option(
    "--ssh-tunnel",
    default=None,
    help="Open an SSH tunnel to the given address and connect to the database from it.",
)
@click.option(
    "--sslmode",
    default=None,
    envvar="GSSLMODE",
    type=click.Choice(["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]),
    help="SSL mode for GaussDB connection. "
         "disable: no SSL. allow: try SSL if server requires. prefer: prefer SSL. "
         "require: require SSL without verifying server cert. "
         "verify-ca: require SSL and verify server CA. "
         "verify-full: require SSL and verify server identity.",
)
@click.option(
    "--sslcert",
    default=None,
    envvar="GSSLCERT",
    help="Path to the client SSL certificate file.",
)
@click.option(
    "--sslkey",
    default=None,
    envvar="GSSLKEY",
    help="Path to the client SSL private key file.",
)
@click.option(
    "--sslrootcert",
    default=None,
    envvar="GSSLROOTCERT",
    help="Path to the SSL root certificate authority (CA) file.",
)
@click.option(
    "--target-session-attrs",
    default=None,
    envvar="GSTARGETSESSIONATTRS",
    type=click.Choice(
        ["any", "read-write", "read-only", "primary", "standby", "prefer-standby"]
    ),
    help="Connect only to a server instance with the requested session "
         "characteristics. Most useful with a multi-host connection, e.g. "
         "--target-session-attrs read-only to land on a standby.",
)
@click.option(
    "--gaussdb-driver-path",
    default=None,
    envvar="GS_DRIVER_PATH",
    help="Path to GaussDB official psycopg2 driver (containing psycopg2/ and lib/). "
         "Required for GaussDB SHA256 authentication. "
         "Defaults to GS_DRIVER_PATH env var.",
    type=click.Path(exists=True, file_okay=False, readable=True),
)
@click.argument("dbname", default=lambda: None, envvar="GSDATABASE", nargs=1)
@click.argument("username", default=lambda: None, envvar="GSUSER", nargs=1)
def cli(
    dbname,
    username_opt,
    host,
    port,
    prompt_passwd,
    never_prompt,
    single_connection,
    dbname_opt,
    username,
    version,
    gslirc,
    dsn,
    row_limit,
    less_chatty,
    prompt,
    prompt_dsn,
    list_databases,
    auto_vertical_output,
    list_dsn,
    warn,
    ssh_tunnel: str,
    sslmode,
    sslcert,
    sslkey,
    sslrootcert,
    target_session_attrs,
    gaussdb_driver_path,
):
    if version:
        from .build_info import BUILD_TIME
        print("Version:", __version__)
        print("Build time:", BUILD_TIME)
        sys.exit(0)

    # Always show version banner on startup
    from .build_info import BUILD_TIME
    click.secho("gscli v%s (built %s)" % (__version__, BUILD_TIME), fg="green", err=True)

    config_dir = os.path.dirname(config_location())
    if not os.path.exists(config_dir):
        os.makedirs(config_dir)

    # Migrate the config file from old location.
    config_full_path = config_location() + "config"
    if os.path.exists(os.path.expanduser("~/.gslirc")):
        if not os.path.exists(config_full_path):
            shutil.move(os.path.expanduser("~/.gslirc"), config_full_path)
            print("Config file (~/.gslirc) moved to new location", config_full_path)
        else:
            print("Config file is now located at", config_full_path)
            print(
                "Please move the existing config file ~/.gslirc to",
                config_full_path,
            )
    # Load GaussDB psycopg2 driver if path is provided (handles reconnections)
    if gaussdb_driver_path:
        from .driver_loader import load_gaussdb_driver
        loaded = load_gaussdb_driver(gaussdb_driver_path)
        if loaded and "psycopg2" in sys.modules:
            click.secho(
                "Note: psycopg2 was already loaded before --gaussdb-driver-path was processed.\n"
                "      For first-load compatibility, set the GS_DRIVER_PATH environment variable.",
                fg="yellow",
            )

    if list_dsn:
        try:
            cfg = load_config(gslirc, config_full_path)
            for alias in cfg["alias_dsn"]:
                click.secho(alias + " : " + cfg["alias_dsn"][alias])
            sys.exit(0)
        except Exception as err:
            click.secho(
                "Invalid DSNs found in the config file. "
                'Please check the "[alias_dsn]" section in gslirc.',
                err=True,
                fg="red",
            )
            sys.exit(1)

    if ssh_tunnel and not SSH_TUNNEL_SUPPORT:
        click.secho(
            'Cannot open SSH tunnel, "sshtunnel" package was not found. '
            "Please install gscli with `pip install gscli[sshtunnel]` if you want SSH tunnel support.",
            err=True,
            fg="red",
        )
        sys.exit(1)

    gscli = GSCli(
        prompt_passwd,
        never_prompt,
        gslirc_file=gslirc,
        row_limit=row_limit,
        single_connection=single_connection,
        less_chatty=less_chatty,
        prompt=prompt,
        prompt_dsn=prompt_dsn,
        auto_vertical_output=auto_vertical_output,
        warn=warn,
        ssh_tunnel_url=ssh_tunnel,
    )

    # Choose which ever one has a valid value.
    if dbname_opt and dbname:
        # work as psql: when database is given as option and argument use the argument as user
        username = dbname
    database = dbname_opt or dbname or ""
    user = username_opt or username
    service = None
    if database.startswith("service="):
        service = database[8:]
    elif os.getenv("GSSERVICE") is not None:
        service = os.getenv("GSSERVICE")
    # because option --list or -l are not supposed to have a db name
    if list_databases:
        database = "gaussdb"

    # Show help when no connection parameters are provided
    if not database and not user and not host and not dsn and not list_databases:
        click.echo(cli.get_help(click.Context(cli)))
        sys.exit(0)

    # Connection kwargs shared by every connect path below. They are forwarded
    # unchanged to psycopg2.connect(), so libpq keywords such as
    # target_session_attrs work for multi-host connections too.
    conn_kwargs = dict(
        sslmode=sslmode,
        sslcert=sslcert,
        sslkey=sslkey,
        sslrootcert=sslrootcert,
    )
    if target_session_attrs:
        conn_kwargs["target_session_attrs"] = target_session_attrs

    if dsn != "":
        try:
            cfg = load_config(gslirc, config_full_path)
            dsn_config = cfg["alias_dsn"][dsn]
        except KeyError:
            click.secho(
                f"Could not find a DSN with alias {dsn}. "
                'Please check the "[alias_dsn]" section in gslirc.',
                err=True,
                fg="red",
            )
            sys.exit(1)
        except Exception:
            click.secho(
                "Invalid DSNs found in the config file. "
                'Please check the "[alias_dsn]" section in gslirc.',
                err=True,
                fg="red",
            )
            sys.exit(1)
        gscli.connect_uri(dsn_config, **conn_kwargs)
        gscli.dsn_alias = dsn
    elif "://" in database:
        gscli.connect_uri(database, **conn_kwargs)
    elif "=" in database and service is None:
        gscli.connect_dsn(database, user=user, **conn_kwargs)
    elif service is not None:
        gscli.connect_service(service, user)
    else:
        gscli.connect(database, host, user, port, **conn_kwargs)

    if list_databases:
        cur, headers, status = gscli.gsexecute.full_databases()

        title = "List of databases"
        settings = OutputSettings(table_format="ascii", missingval="<null>")
        formatted = format_output(title, cur, headers, status, settings)
        gscli.echo_via_pager("\n".join(formatted))

        sys.exit(0)

    gscli.logger.debug(
        "Launch Params: \n" "\tdatabase: %r" "\tuser: %r" "\thost: %r" "\tport: %r",
        database,
        user,
        host,
        port,
    )

    if setproctitle:
        obfuscate_process_password()

    gscli.run_cli()


def obfuscate_process_password():
    process_title = setproctitle.getproctitle()
    if "://" in process_title:
        process_title = re.sub(r":(.*):(.*)@", r":\1:xxxx@", process_title)
    elif "=" in process_title:
        process_title = re.sub(
            r"password=(.+?)((\s[a-zA-Z]+=)|$)", r"password=xxxx\2", process_title
        )

    setproctitle.setproctitle(process_title)


def has_meta_cmd(query):
    """Determines if the completion needs a refresh by checking if the sql
    statement is an alter, create, drop, commit or rollback."""
    try:
        first_token = query.split()[0]
        if first_token.lower() in ("alter", "create", "drop", "commit", "rollback"):
            return True
    except Exception:
        return False

    return False


def has_change_db_cmd(query):
    """Determines if the statement is a database switch such as 'use' or '\\c'"""
    try:
        first_token = query.split()[0]
        if first_token.lower() in ("use", "\\c", "\\connect"):
            return True
    except Exception:
        return False

    return False


def has_change_path_cmd(sql):
    """Determines if the search_path should be refreshed by checking if the
    sql has 'set search_path'."""
    return "set search_path" in sql.lower()


def is_mutating(status):
    """Determines if the statement is mutating based on the status."""
    if not status:
        return False

    mutating = {"insert", "update", "delete"}
    return status.split(None, 1)[0].lower() in mutating


def is_select(status):
    """Returns true if the first word in status is 'select'."""
    if not status:
        return False
    return status.split(None, 1)[0].lower() == "select"


def exception_formatter(e):
    return click.style(str(e), fg="red")


def format_output(title, cur, headers, status, settings):
    output = []
    expanded = settings.expanded or settings.table_format == "vertical"
    table_format = "vertical" if settings.expanded else settings.table_format
    max_width = settings.max_width
    case_function = settings.case_function
    formatter = TabularOutputFormatter(format_name=table_format)

    def format_array(val):
        if val is None:
            return settings.missingval
        if not isinstance(val, list):
            return val
        return "{" + ",".join(str(format_array(e)) for e in val) + "}"

    def format_arrays(data, headers, **_):
        data = list(data)
        for row in data:
            row[:] = [
                format_array(val) if isinstance(val, list) else val for val in row
            ]

        return data, headers

    def format_status(cur, status):
        # redshift does not return rowcount as part of status.
        # See https://github.com/dbcli/gscli/issues/1320
        if cur and hasattr(cur, "rowcount") and cur.rowcount is not None:
            if status and not status.endswith(str(cur.rowcount)):
                status += " %s" % cur.rowcount
        return status

    output_kwargs = {
        "sep_title": "RECORD {n}",
        "sep_character": "-",
        "sep_length": (1, 25),
        "missing_value": settings.missingval,
        "integer_format": settings.dcmlfmt,
        "float_format": settings.floatfmt,
        "preprocessors": (format_numbers, format_arrays),
        "disable_numparse": True,
        "preserve_whitespace": True,
        "style": settings.style_output,
        "max_field_width": settings.max_field_width,
    }
    if not settings.floatfmt:
        output_kwargs["preprocessors"] = (align_decimals,)

    if table_format == "csv":
        # The default CSV dialect is "excel" which is not handling newline values correctly
        # Nevertheless, we want to keep on using "excel" on Windows since it uses '\r\n'
        # as the line terminator
        # https://github.com/dbcli/gscli/issues/1102
        dialect = "excel" if platform.system() == "Windows" else "unix"
        output_kwargs["dialect"] = dialect

    if title:  # Only print the title if it's not None.
        output.append(title)

    if cur:
        headers = [case_function(x) for x in headers]
        if max_width is not None:
            cur = list(cur)
        column_types = None
        if hasattr(cur, "description"):
            column_types = []
            for d in cur.description:
                # pg3: type_name = cur.adapters.types[d.type_code].name
                if (
                    # pg3: type_name in ("numeric", "float4", "float8")
                    d[1] in psycopg2.extensions.DECIMAL.values
                    or d[1] in psycopg2.extensions.FLOAT.values
                ):
                    column_types.append(float)
                if (
                    # pg3: type_name in ("int2", "int4", "int8")
                    d[1] == psycopg2.extensions.INTEGER.values
                    or d[1] in psycopg2.extensions.LONGINTEGER.values
                ):
                    column_types.append(int)
                else:
                    column_types.append(str)

        formatted = formatter.format_output(cur, headers, **output_kwargs)
        if isinstance(formatted, str):
            formatted = iter(formatted.splitlines())
        first_line = next(formatted)
        formatted = itertools.chain([first_line], formatted)
        if (
            not expanded
            and max_width
            and len(strip_ansi(first_line)) > max_width
            and headers
        ):
            formatted = formatter.format_output(
                cur, headers, format_name="vertical", column_types=None, **output_kwargs
            )
            if isinstance(formatted, str):
                formatted = iter(formatted.splitlines())

        output = itertools.chain(output, formatted)

    # Only print the status if it's not None and we are not producing CSV
    if status and table_format != "csv":
        output = itertools.chain(output, [format_status(cur, status)])

    return output


def parse_service_info(service):
    service = service or os.getenv("GSSERVICE")
    service_file = os.getenv("GSSERVICEFILE")
    if not service_file:
        # try ~/.gs_service.conf (if that exists)
        if platform.system() == "Windows":
            service_file = os.getenv("GSSYSCONFDIR") + "\\gs_service.conf"
        elif os.getenv("GSSYSCONFDIR"):
            service_file = os.path.join(os.getenv("GSSYSCONFDIR"), ".gs_service.conf")
        else:
            service_file = os.path.expanduser("~/.gs_service.conf")
    if not service or not os.path.exists(service_file):
        # nothing to do
        return None, service_file
    with open(service_file, newline="") as f:
        skipped_lines = skip_initial_comment(f)
        try:
            service_file_config = ConfigObj(f)
        except ParseError as err:
            err.line_number += skipped_lines
            raise err
    if service not in service_file_config:
        return None, service_file
    service_conf = service_file_config.get(service)
    return service_conf, service_file


if __name__ == "__main__":
    cli()
