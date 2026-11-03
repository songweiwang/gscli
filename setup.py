import platform
from setuptools import setup, find_packages

from gscli import __version__

description = "CLI for GaussDB Database. With auto-completion and syntax highlighting."

install_requirements = [
    "click >= 4.1",
    "Pygments>=2.0",
    "prompt_toolkit>=2.0.6,<4.0.0",
    "sqlparse >=0.3.0,<0.5",
    "configobj >= 5.0.6",
    "pendulum>=2.1.0",
    "cli_helpers[styles] >= 2.2.1",
]


# setproctitle is used to mask the password when running `ps` in command line.
# But this is not necessary in Windows since the password is never shown in the
# task manager. Also setproctitle is a hard dependency to install in Windows,
# so we'll only install it if we're not in Windows.
if platform.system() != "Windows" and not platform.system().startswith("CYGWIN"):
    install_requirements.append("setproctitle >= 1.1.9")

setup(
    name="gscli",
    author="songweiwang@126.com",
    author_email="songweiwang@126.com",
    version=__version__,
    license="BSD",
    url="https://github.com/songweiwang/gscli",
    project_urls={
        "Source": "https://github.com/songweiwang/gscli",
    },
    packages=find_packages(),
    package_data={"gscli": ["gslirc", "packages/gsliterals/gsliterals.json"]},
    description=description,
    long_description=open("README.rst").read(),
    install_requires=install_requirements,
    extras_require={
        "keyring": ["keyring >= 12.2.0"],
        "sshtunnel": ["sshtunnel >= 0.4.0"],
    },
    python_requires=">=3.6",
    entry_points="""
        [console_scripts]
        gscli=gscli.main:cli
    """,
    classifiers=[
        "Intended Audience :: Developers",
        "License :: OSI Approved :: BSD License",
        "Operating System :: Unix",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: SQL",
        "Topic :: Database",
        "Topic :: Database :: Front-Ends",
        "Topic :: Software Development",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
