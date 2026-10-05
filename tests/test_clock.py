from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
import requests

from nut_ups import clock as clock_mod
from nut_ups.clock import ClockGate, supervisor_sync_check, supervisor_token
from nut_ups.records import Observation, Reading
from tests.conftest import FakeClock, utc


class Switch:
    def __init__(self, synced: bool = False) -> None:
        self.synced = synced
        self.calls = 0

    def __call__(self) -> tuple[bool, str]:
        self.calls += 1
        return self.synced, "" if self.synced else "dt_synchronized = false"


def obs(mono: float, kind: str = "status") -> Observation:
    return Observation(kind=kind, reading=Reading(status="OL"), mono=mono)


def test_sincronizado_fecha_al_instante():
    clock = FakeClock(utc(2026, 10, 5, 13, 0, 0))
    gate = ClockGate(Switch(True), now=clock.now, monotonic=clock.monotonic)
    [(o, ts)] = gate.submit(obs(clock.mono))
    assert ts == utc(2026, 10, 5, 13, 0, 0)
    assert gate.held_count == 0


def test_retiene_hasta_sincronizar_y_fecha_con_el_monotonico(caplog):
    """La Pi vuelve de un corte total con la hora corrida 3 h hacia atrás."""
    clock = FakeClock(utc(2026, 10, 5, 10, 0, 0))  # hora falsa del arranque sin NTP
    sw = Switch(False)
    gate = ClockGate(sw, now=clock.now, monotonic=clock.monotonic)
    with caplog.at_level(logging.WARNING):
        assert gate.submit(obs(clock.mono)) == []  # lectura A en mono=1000
        clock.advance(60)
        assert gate.submit(obs(clock.mono, "sample")) == []  # lectura B en mono=1060
        clock.advance(40)
        assert gate.flush() == []
    assert gate.held_count == 2
    avisos = [r for r in caplog.records if "NO confirmada" in r.getMessage()]
    assert len(avisos) == 1  # con rate limit
    # NTP corrige el reloj de pared (+3 h); el monotónico sigue igual.
    clock.wall = utc(2026, 10, 5, 13, 1, 40)
    sw.synced = True
    out = gate.flush()
    assert [(o.kind, ts) for o, ts in out] == [
        ("status", utc(2026, 10, 5, 13, 0, 0)),
        ("sample", utc(2026, 10, 5, 13, 1, 0)),
    ]
    assert gate.held_count == 0
    assert all(ts.tzinfo is not None and ts.utcoffset() == timedelta(0) for _, ts in out)


def test_falla_del_chequeo_cuenta_como_no_sincronizado():
    def boom() -> tuple[bool, str]:
        raise RuntimeError("supervisor caído")

    clock = FakeClock()
    gate = ClockGate(boom, now=clock.now, monotonic=clock.monotonic)
    assert gate.submit(obs(clock.mono)) == []
    assert gate.held_count == 1


def test_sin_retenidas_no_consulta_al_supervisor():
    sw = Switch(True)
    gate = ClockGate(sw)
    assert gate.flush() == []
    assert sw.calls == 0


def test_tope_de_retenidas_descarta_las_mas_viejas():
    clock = FakeClock()
    sw = Switch(False)
    gate = ClockGate(sw, now=clock.now, monotonic=clock.monotonic, max_held=3)
    for i in range(5):
        gate.submit(obs(float(i)))
    assert gate.held_count == 3
    sw.synced = True
    assert [o.mono for o, _ in gate.flush()] == [2.0, 3.0, 4.0]


class Resp:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.mark.parametrize(
    ("resp", "esperado"),
    [
        (Resp(200, {"result": "ok", "data": {"dt_synchronized": True, "use_ntp": True}}), True),
        (Resp(200, {"result": "ok", "data": {"dt_synchronized": False}}), False),
        (Resp(200, {"result": "ok", "data": {"dt_synchronized": "true"}}), False),  # solo True estricto
        (Resp(200, {"result": "ok", "data": {}}), False),
        (Resp(200, ValueError("no json")), False),
        (Resp(403, {"result": "error"}), False),
        (requests.ConnectionError("x"), False),
    ],
)
def test_supervisor_sync_check(monkeypatch, resp, esperado):
    seen = {}

    def fake_get(url, headers, timeout):
        seen.update(url=url, headers=headers, timeout=timeout)
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(clock_mod.requests, "get", fake_get)
    synced, why = supervisor_sync_check(token="tok-123")()
    assert synced is esperado
    assert (why == "") is esperado
    assert seen["url"] == "http://supervisor/host/info"
    assert seen["headers"] == {"Authorization": "Bearer tok-123"}


def test_token_del_entorno_o_del_archivo_de_s6(monkeypatch, tmp_path):
    """s6-overlay v3 no pasa el entorno al CMD: el token queda en /run/s6/container_environment."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert supervisor_token(str(tmp_path)) == ""
    (tmp_path / "SUPERVISOR_TOKEN").write_text("tok-s6")
    assert supervisor_token(str(tmp_path)) == "tok-s6"
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok-env")
    assert supervisor_token(str(tmp_path)) == "tok-env"


def test_supervisor_sync_check_sin_token(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(clock_mod, "S6_ENV_DIR", str(tmp_path))
    monkeypatch.setattr(clock_mod.requests, "get", lambda *a, **k: pytest.fail("no debe llamar"))
    synced, why = supervisor_sync_check()()
    assert synced is False and "SUPERVISOR_TOKEN" in why


def test_now_naive_es_un_error():
    gate = ClockGate(Switch(True), now=lambda: datetime(2026, 10, 5))  # noqa: DTZ001
    with pytest.raises(ValueError):
        gate.submit(obs(0.0))


def test_default_now_es_utc():
    gate = ClockGate(Switch(True))
    [(_, ts)] = gate.submit(obs(clock_mod.time.monotonic()))
    assert ts.tzinfo is UTC
