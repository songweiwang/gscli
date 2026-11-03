import os
from unittest.mock import patch, MagicMock, ANY

import pytest
from configobj import ConfigObj
from click.testing import CliRunner
from sshtunnel import SSHTunnelForwarder

from gscli.main import cli, GSCli
from gscli.gsexecute import GSExecute


@pytest.fixture
def mock_ssh_tunnel_forwarder() -> MagicMock:
    mock_ssh_tunnel_forwarder = MagicMock(
        SSHTunnelForwarder, local_bind_ports=[1111], autospec=True
    )
    with patch(
        "gscli.main.sshtunnel.SSHTunnelForwarder",
        return_value=mock_ssh_tunnel_forwarder,
    ) as mock:
        yield mock


@pytest.fixture
def mock_gsexecute() -> MagicMock:
    with patch.object(GSExecute, "__init__", return_value=None) as mock_gsexecute:
        yield mock_gsexecute


def test_ssh_tunnel(
    mock_ssh_tunnel_forwarder: MagicMock, mock_gsexecute: MagicMock
) -> None:
    # Test with just a host
    tunnel_url = "some.host"
    db_params = {
        "database": "dbname",
        "host": "db.host",
        "user": "db_user",
        "passwd": "db_passwd",
    }
    expected_tunnel_params = {
        "local_bind_address": ("127.0.0.1",),
        "remote_bind_address": (db_params["host"], 5432),
        "ssh_address_or_host": (tunnel_url, 22),
        "logger": ANY,
    }

    gscli = GSCli(ssh_tunnel_url=tunnel_url)
    gscli.connect(**db_params)

    mock_ssh_tunnel_forwarder.assert_called_once_with(**expected_tunnel_params)
    mock_ssh_tunnel_forwarder.return_value.start.assert_called_once()
    mock_gsexecute.assert_called_once()

    call_args, call_kwargs = mock_gsexecute.call_args
    assert call_args == (
        db_params["database"],
        db_params["user"],
        db_params["passwd"],
        "127.0.0.1",
        gscli.ssh_tunnel.local_bind_ports[0],
        "",
    )
    mock_ssh_tunnel_forwarder.reset_mock()
    mock_gsexecute.reset_mock()

    # Test with a full url and with a specific db port
    tunnel_user = "tunnel_user"
    tunnel_passwd = "tunnel_pass"
    tunnel_host = "some.other.host"
    tunnel_port = 1022
    tunnel_url = f"ssh://{tunnel_user}:{tunnel_passwd}@{tunnel_host}:{tunnel_port}"
    db_params["port"] = 1234

    expected_tunnel_params["remote_bind_address"] = (
        db_params["host"],
        db_params["port"],
    )
    expected_tunnel_params["ssh_address_or_host"] = (tunnel_host, tunnel_port)
    expected_tunnel_params["ssh_username"] = tunnel_user
    expected_tunnel_params["ssh_password"] = tunnel_passwd

    gscli = GSCli(ssh_tunnel_url=tunnel_url)
    gscli.connect(**db_params)

    mock_ssh_tunnel_forwarder.assert_called_once_with(**expected_tunnel_params)
    mock_ssh_tunnel_forwarder.return_value.start.assert_called_once()
    mock_gsexecute.assert_called_once()

    call_args, call_kwargs = mock_gsexecute.call_args
    assert call_args == (
        db_params["database"],
        db_params["user"],
        db_params["passwd"],
        "127.0.0.1",
        gscli.ssh_tunnel.local_bind_ports[0],
        "",
    )
    mock_ssh_tunnel_forwarder.reset_mock()
    mock_gsexecute.reset_mock()

    # Test with DSN
    dsn = (
        f"user={db_params['user']} password={db_params['passwd']} "
        f"host={db_params['host']} port={db_params['port']}"
    )

    gscli = GSCli(ssh_tunnel_url=tunnel_url)
    gscli.connect(dsn=dsn)

    expected_dsn = (
        f"user={db_params['user']} password={db_params['passwd']} "
        f"host=127.0.0.1 port={gscli.ssh_tunnel.local_bind_ports[0]}"
    )

    mock_ssh_tunnel_forwarder.assert_called_once_with(**expected_tunnel_params)
    mock_gsexecute.assert_called_once()

    call_args, call_kwargs = mock_gsexecute.call_args
    assert expected_dsn in call_args


def test_cli_with_tunnel() -> None:
    runner = CliRunner()
    tunnel_url = "mytunnel"
    with patch.object(
        GSCli, "__init__", autospec=True, return_value=None
    ) as mock_gscli:
        runner.invoke(cli, ["--ssh-tunnel", tunnel_url])
        mock_gscli.assert_called_once()
        call_args, call_kwargs = mock_gscli.call_args
        assert call_kwargs["ssh_tunnel_url"] == tunnel_url


def test_config(
    tmpdir: os.PathLike, mock_ssh_tunnel_forwarder: MagicMock, mock_gsexecute: MagicMock
) -> None:
    gslirc = str(tmpdir.join("rcfile"))

    tunnel_user = "tunnel_user"
    tunnel_passwd = "tunnel_pass"
    tunnel_host = "tunnel.host"
    tunnel_port = 1022
    tunnel_url = f"{tunnel_user}:{tunnel_passwd}@{tunnel_host}:{tunnel_port}"

    tunnel2_url = "tunnel2.host"

    config = ConfigObj()
    config.filename = gslirc
    config["ssh tunnels"] = {}
    config["ssh tunnels"][r"\.com$"] = tunnel_url
    config["ssh tunnels"][r"^hello-"] = tunnel2_url
    config.write()

    # Unmatched host
    gscli = GSCli(gslirc_file=gslirc)
    gscli.connect(host="unmatched.host")
    mock_ssh_tunnel_forwarder.assert_not_called()

    # Host matching first tunnel
    gscli = GSCli(gslirc_file=gslirc)
    gscli.connect(host="matched.host.com")
    mock_ssh_tunnel_forwarder.assert_called_once()
    call_args, call_kwargs = mock_ssh_tunnel_forwarder.call_args
    assert call_kwargs["ssh_address_or_host"] == (tunnel_host, tunnel_port)
    assert call_kwargs["ssh_username"] == tunnel_user
    assert call_kwargs["ssh_password"] == tunnel_passwd
    mock_ssh_tunnel_forwarder.reset_mock()

    # Host matching second tunnel
    gscli = GSCli(gslirc_file=gslirc)
    gscli.connect(host="hello-i-am-matched")
    mock_ssh_tunnel_forwarder.assert_called_once()

    call_args, call_kwargs = mock_ssh_tunnel_forwarder.call_args
    assert call_kwargs["ssh_address_or_host"] == (tunnel2_url, 22)
    mock_ssh_tunnel_forwarder.reset_mock()

    # Host matching both tunnels (will use the first one matched)
    gscli = GSCli(gslirc_file=gslirc)
    gscli.connect(host="hello-i-am-matched.com")
    mock_ssh_tunnel_forwarder.assert_called_once()

    call_args, call_kwargs = mock_ssh_tunnel_forwarder.call_args
    assert call_kwargs["ssh_address_or_host"] == (tunnel_host, tunnel_port)
    assert call_kwargs["ssh_username"] == tunnel_user
    assert call_kwargs["ssh_password"] == tunnel_passwd
