"""Shared test setup."""

import ipaddress
import socket

import pytest

_LOCAL_NAMES = frozenset({"localhost", "testserver", "127.0.0.1", "::1", ""})
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex


def _is_local(host: object) -> bool:
    if host is None:
        return True
    text = host.decode() if isinstance(host, bytes) else str(host)
    if text.lower() in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(text.split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _server_clock(monkeypatch):
    """Tools use the server clock for "today" in tests (no athlete time zone lookup).

    Tests of the time zone lookup unset ATHLETE_TIMEZONE themselves.
    """
    monkeypatch.setenv("ATHLETE_TIMEZONE", "server")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test may reach the network (intervals.icu or anything else): only loopback is allowed.

    A blocked attempt raises inside the test and also fails it afterwards, in case the code under
    test swallowed the error (the API client turns connection errors into error results).
    """
    attempts: list[str] = []

    def getaddrinfo(host, *args, **kwargs):
        if not _is_local(host):
            attempts.append(f"resolve {host}")
            raise OSError(f"network access to {host} is blocked in tests")
        return _REAL_GETADDRINFO(host, *args, **kwargs)

    def _check(address) -> None:
        host = address[0] if isinstance(address, tuple) else None
        if not _is_local(host):
            attempts.append(f"connect {address}")
            raise OSError(f"network access to {address} is blocked in tests")

    def connect(sock, address):
        _check(address)
        return _REAL_CONNECT(sock, address)

    def connect_ex(sock, address):
        _check(address)
        return _REAL_CONNECT_EX(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield
    assert not attempts, f"test tried to reach the network: {attempts}"
