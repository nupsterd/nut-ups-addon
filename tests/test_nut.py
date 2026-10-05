from __future__ import annotations

import socket

import pytest

from nut_ups.nut import MAX_LINE, NutClient, NutError, split_words
from tests.conftest import EPU1200_OL, UPS, list_var_response


def ok_handler(line: str) -> bytes:
    assert line == f"LIST VAR {UPS}"
    return list_var_response(UPS, EPU1200_OL)


def test_list_var_contra_upsd_falso(upsd_factory):
    srv = upsd_factory(ok_handler)
    client = NutClient("127.0.0.1", srv.port)
    assert client.list_var(UPS) == EPU1200_OL
    # La conexión se reusa entre consultas.
    assert client.list_var(UPS)["ups.status"] == "OL"
    assert srv.connections == 1
    assert srv.commands == [f"LIST VAR {UPS}"] * 2
    client.close()


def test_valores_con_espacios_y_escapes(upsd_factory):
    nut_vars = {"ups.status": "OB DISCHRG LB", "ups.firmware": 'v "2" \\ x', "battery.voltage": "23.10"}
    srv = upsd_factory(lambda line: list_var_response(UPS, nut_vars))
    client = NutClient("127.0.0.1", srv.port)
    assert client.list_var(UPS) == nut_vars
    client.close()


@pytest.mark.parametrize(
    ("line", "words"),
    [
        ('VAR epu1200 ups.status "OL"', ["VAR", "epu1200", "ups.status", "OL"]),
        ('VAR epu1200 ups.status "OB LB"', ["VAR", "epu1200", "ups.status", "OB LB"]),
        ('VAR u x ""', ["VAR", "u", "x", ""]),
        ('VAR u x "a\\"b\\\\c"', ["VAR", "u", "x", 'a"b\\c']),
        ("ERR UNKNOWN-UPS", ["ERR", "UNKNOWN-UPS"]),
        ("  BEGIN  LIST VAR u ", ["BEGIN", "LIST", "VAR", "u"]),
    ],
)
def test_split_words(line, words):
    assert split_words(line) == words


def test_comillas_sin_cerrar():
    with pytest.raises(NutError):
        split_words('VAR u x "abc')


@pytest.mark.parametrize("code", ["UNKNOWN-UPS", "DATA-STALE", "DRIVER-NOT-CONNECTED", "ACCESS-DENIED"])
def test_err_de_upsd(upsd_factory, code):
    srv = upsd_factory(lambda line: f"ERR {code}\n".encode())
    client = NutClient("127.0.0.1", srv.port)
    with pytest.raises(NutError) as ei:
        client.list_var(UPS)
    assert ei.value.code == code
    assert not client.connected  # se cierra y la próxima consulta reconecta


def test_err_a_mitad_de_la_lista(upsd_factory):
    srv = upsd_factory(lambda line: b"BEGIN LIST VAR epu1200\nERR DATA-STALE\n")
    with pytest.raises(NutError) as ei:
        NutClient("127.0.0.1", srv.port).list_var(UPS)
    assert ei.value.code == "DATA-STALE"


@pytest.mark.parametrize(
    "response",
    [
        b"BEGIN LIST VAR otra\nEND LIST VAR otra\n",  # otra UPS
        b"HOLA\n",
        b'BEGIN LIST VAR epu1200\nVAR otra ups.status "OL"\nEND LIST VAR epu1200\n',
        b"BEGIN LIST VAR epu1200\nVAR epu1200 ups.status\nEND LIST VAR epu1200\n",
        b"BEGIN LIST VAR epu1200\n" + b"x" * (MAX_LINE + 10) + b"\n",
    ],
    ids=["otra-ups", "basura", "var-de-otra-ups", "var-sin-valor", "linea-larga"],
)
def test_respuestas_que_no_respetan_el_protocolo(upsd_factory, response):
    srv = upsd_factory(lambda line: response)
    client = NutClient("127.0.0.1", srv.port)
    with pytest.raises(NutError) as ei:
        client.list_var(UPS)
    assert ei.value.code == "PROTOCOL"
    assert not client.connected


def test_upsd_cierra_a_mitad_de_respuesta(upsd_factory):
    calls = {"n": 0}

    def handler(line: str) -> bytes | None:
        calls["n"] += 1
        return None if calls["n"] == 1 else ok_handler(line)

    srv = upsd_factory(handler)
    client = NutClient("127.0.0.1", srv.port)
    with pytest.raises(ConnectionError):
        client.list_var(UPS)
    assert not client.connected
    # Reconecta sola en la siguiente consulta.
    assert client.list_var(UPS)["ups.status"] == "OL"
    assert srv.connections == 2
    client.close()


def test_upsd_caido():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nadie escucha en ese puerto
    client = NutClient("127.0.0.1", port, connect_timeout=1)
    with pytest.raises(OSError):
        client.list_var(UPS)
    assert not client.connected


def test_upsd_mudo_corta_por_timeout(upsd_factory):
    srv = upsd_factory(lambda line: b"")  # acepta y nunca responde
    client = NutClient("127.0.0.1", srv.port, read_timeout=0.3)
    with pytest.raises(TimeoutError):
        client.list_var(UPS)
    assert not client.connected
