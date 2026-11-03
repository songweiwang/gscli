"""
gscli package main entry point.

Loads GaussDB psycopg2 driver (if available) before importing the CLI module,
ensuring the correct libpq is used for GaussDB SHA256 authentication.

Driver discovery order:
1. GS_DRIVER_PATH env var
2. Bundled libs/gaussdb_driver/ directory (dev tree or PyInstaller bundle)
"""
import os
import sys


def _try_load_bundled_driver():
    """Try to locate and load the GaussDB driver from known locations."""
    # 1. GS_DRIVER_PATH env var
    driver_path = os.environ.get("GS_DRIVER_PATH", "")

    # 2. PyInstaller bundle: libs/gaussdb_driver/ at MEIPASS root
    if not driver_path and getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidate = os.path.join(sys._MEIPASS, "libs", "gaussdb_driver")
        if os.path.isdir(os.path.join(candidate, "psycopg2")):
            driver_path = candidate

    # 3. Project source tree: libs/gaussdb_driver/ relative to this file
    if not driver_path:
        candidate = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "libs", "gaussdb_driver")
        )
        if os.path.isdir(os.path.join(candidate, "psycopg2")):
            driver_path = candidate

    if not driver_path:
        return

    # Preload libpq.so.5.5 (ctypes may not be available in all frozen builds)
    lib_dir = os.path.join(driver_path, "lib")
    libpq_path = os.path.join(lib_dir, "libpq.so.5.5")
    if os.path.isfile(libpq_path):
        ld_path = os.environ.get("LD_LIBRARY_PATH", "")
        paths = [p for p in ld_path.split(":") if p]
        if lib_dir not in paths:
            os.environ["LD_LIBRARY_PATH"] = (
                lib_dir + ":" + ld_path if ld_path else lib_dir
            )
        try:
            import ctypes  # noqa: E402
            ctypes.cdll.LoadLibrary(libpq_path)
        except (ImportError, OSError):
            pass

    # Add driver to sys.path so 'import psycopg2' finds the GaussDB version
    psycopg2_dir = os.path.join(driver_path, "psycopg2")
    if os.path.isdir(psycopg2_dir) and driver_path not in sys.path:
        sys.path.insert(0, driver_path)


_try_load_bundled_driver()

from .main import cli  # noqa: E402

if __name__ == "__main__":
    cli()
