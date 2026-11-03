"""
GaussDB psycopg2 driver loader.

GaussDB uses a custom libpq with proprietary SHA256 authentication (different
from PostgreSQL's scram-sha-256). The official GaussDB psycopg2 driver
(provided as a compiled _psycopg.so linked against GaussDB's libpq.so.5.5)
must be loaded instead of the PyPI psycopg2.

Driver search order (first found wins):
1. driver_path argument passed to load_gaussdb_driver()
2. GS_DRIVER_PATH environment variable
3. Bundled path: libs/gaussdb_driver/ (relative to project root or PyInstaller bundle)
4. Default paths: /tmp/gaussdb_conn, /usr/local/gaussdb/driver
"""

import ctypes
import logging
import os
import sys

_logger = logging.getLogger(__name__)

DEFAULT_DRIVER_PATHS = [
    "/tmp/gaussdb_conn",
    "/usr/local/gaussdb/driver",
]

ENV_VAR = "GS_DRIVER_PATH"


def _bundled_driver_path() -> str:
    """Return the path to the GaussDB driver bundled in the project libs/ directory.

    Handles both development mode (project source tree) and PyInstaller frozen mode.
    """
    # PyInstaller: files are extracted to sys._MEIPASS
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidate = os.path.join(sys._MEIPASS, "libs", "gaussdb_driver")
        if os.path.isdir(os.path.join(candidate, "psycopg2")):
            return candidate

    # Development mode: relative to driver_loader.py (i.e., gscli/driver_loader.py)
    loader_dir = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(loader_dir, "..", "libs", "gaussdb_driver")
    candidate = os.path.abspath(candidate)
    if os.path.isdir(os.path.join(candidate, "psycopg2")):
        return candidate

    return ""


def _preload_libpq(driver_path: str) -> bool:
    """Preload GaussDB's libpq.so.5.5 so _psycopg.so resolves it."""
    lib_dir = os.path.join(driver_path, "lib")
    libpq_path = os.path.join(lib_dir, "libpq.so.5.5")

    if not os.path.isfile(libpq_path):
        _logger.warning("GaussDB libpq not found at %s", libpq_path)
        return False

    # Ensure the lib directory is in LD_LIBRARY_PATH for the dynamic linker
    current = os.environ.get("LD_LIBRARY_PATH", "")
    paths = [p for p in current.split(":") if p]
    if lib_dir not in paths:
        os.environ["LD_LIBRARY_PATH"] = lib_dir + ":" + current if current else lib_dir

    # Preload with ctypes to resolve libpq immediately (optional in frozen builds)
    try:
        ctypes.cdll.LoadLibrary(libpq_path)
        _logger.info("Loaded GaussDB libpq from %s", libpq_path)
    except (OSError, ImportError) as e:
        _logger.debug("libpq preload skipped (LD_LIBRARY_PATH is set): %s", e)
    return True


def _add_to_syspath(driver_path: str) -> bool:
    """Prepend the driver directory to sys.path so 'import psycopg2' finds the GaussDB version."""
    psycopg2_dir = os.path.join(driver_path, "psycopg2")
    if not os.path.isdir(psycopg2_dir):
        _logger.warning("psycopg2 directory not found at %s", psycopg2_dir)
        return False
    if driver_path not in sys.path:
        sys.path.insert(0, driver_path)
    return True


def load_gaussdb_driver(driver_path: str = None) -> bool:
    """Load the GaussDB psycopg2 driver.

    Args:
        driver_path: Path to GaussDB driver directory (containing psycopg2/ and lib/).
                     If None, searches GS_DRIVER_PATH env var, bundled path,
                     then DEFAULT_DRIVER_PATHS.

    Returns:
        True if GaussDB driver was loaded, False if using default psycopg2.
    """
    if driver_path is None:
        driver_path = os.environ.get(ENV_VAR)

    if not driver_path:
        driver_path = _bundled_driver_path()

    if not driver_path:
        for path in DEFAULT_DRIVER_PATHS:
            candidate = os.path.expanduser(path)
            if os.path.isdir(os.path.join(candidate, "psycopg2")):
                driver_path = candidate
                break

    if not driver_path:
        _logger.debug("No GaussDB driver path configured, using default psycopg2")
        return False

    driver_path = os.path.abspath(os.path.expanduser(driver_path))

    if not os.path.isdir(driver_path):
        _logger.warning("GaussDB driver path does not exist: %s", driver_path)
        return False

    _preload_libpq(driver_path)
    path_ok = _add_to_syspath(driver_path)

    if not path_ok:
        return False

    # Verify the GaussDB psycopg2 is loadable
    try:
        import psycopg2 as _check  # noqa: F811
        _logger.info(
            "GaussDB driver loaded from %s (psycopg2 at %s)",
            driver_path, _check.__file__,
        )
        return True
    except ImportError as e:
        _logger.warning(
            "psycopg2 not found after loading driver from %s: %s", driver_path, e
        )
        return False


def is_gaussdb_driver() -> bool:
    """Check if the current psycopg2 is the GaussDB version."""
    try:
        import psycopg2
        mod_file = getattr(psycopg2, "__file__", "") or ""
        return "gaussdb" in mod_file.lower() or (
            psycopg2.__doc__ and "gaussdb" in psycopg2.__doc__.lower()
        )
    except ImportError:
        return False
