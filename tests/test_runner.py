from __future__ import annotations

import json
import logging

from nut_ups.audit import AuditWriter
from nut_ups.clock import ClockGate
from nut_ups.nut import NutClient
from nut_ups.outbox import Outbox
from nut_ups.runner import BACKOFF_DELAYS, SUMMARY_INTERVAL, Runner
from nut_ups.sampler import Sampler
from tests.conftest import DEVICE_SERIAL, EPU1200_OL, UPS, FakeClock, list_var_response, make_config, utc


class Ups:
    """Estado mutable de la UPS falsa; ``down`` hace que upsd corte la conexión."""

    def __init__(self) -> None:
        self.vars = dict(EPU1200_OL)
        self.down = False
        self.err: str | None = None

    def handler(self, line: str) -> bytes | None:
        if self.down:
            return None
        if self.err:
            return f"ERR {self.err}\n".encode()
        return list_var_response(UPS, self.vars)


class Sync:
    def __init__(self, synced: bool = True) -> None:
        self.synced = synced

    def __call__(self) -> tuple[bool, str]:
        return self.synced, "" if self.synced else "dt_synchronized = false"


def make_runner(tmp_path, port, clock, *, sync=None, fanout=True, **cfg):
    config = make_config(nut_host="127.0.0.1", nut_port=port, audit_dir=str(tmp_path / "audit"), **cfg)
    audit = AuditWriter(config.audit_dir, 30, now=clock.now)
    outbox = (
        Outbox(tmp_path / "outbox.sqlite", 1000, 30, clock=clock.time, monotonic=clock.monotonic) if fanout else None
    )
    return Runner(
        config,
        client=NutClient("127.0.0.1", port, read_timeout=2),
        sampler=Sampler(config.float_voltage, config.float_confirm_samples),
        gate=ClockGate(sync or Sync(), now=clock.now, monotonic=clock.monotonic),
        audit=audit,
        outbox=outbox,
        monotonic=clock.monotonic,
    )


def drive(runner: Runner, clock: FakeClock, seconds: float) -> list[float]:
    """Corre vueltas avanzando el reloj lo que cada una pide esperar."""
    delays = []
    end = clock.mono + seconds
    while clock.mono < end:
        d = runner.poll_once()
        delays.append(d)
        clock.advance(d)
    return delays


def audit_lines(tmp_path) -> list[str]:
    return [ln for p in sorted((tmp_path / "audit").glob("nut_ups_audit_*.jsonl")) for ln in p.read_text().splitlines()]


def queued(runner: Runner) -> list[str]:
    rows = runner.outbox._conn.execute("SELECT payload FROM pending ORDER BY id").fetchall()
    return [r[0] for r in rows]


def test_corte_de_red_de_punta_a_punta(tmp_path, upsd_factory):
    ups = Ups()
    srv = upsd_factory(ups.handler)
    clock = FakeClock(utc(2026, 10, 5, 13, 0, 0))
    runner = make_runner(tmp_path, srv.port, clock)
    assert drive(runner, clock, 30) == [5.0] * 6
    ups.vars.update({"ups.status": "OB DISCHRG", "battery.voltage": "25.10", "input.voltage": "0"})
    drive(runner, clock, 125)
    ups.vars.update({"ups.status": "OL CHRG", "battery.voltage": "26.40", "input.voltage": "120.8"})
    drive(runner, clock, 5)

    lines = audit_lines(tmp_path)
    assert queued(runner) == lines  # los mismos bytes en la auditoría y en la cola
    recs = [json.loads(ln) for ln in lines]
    assert [(r["kind"], r["ups_status"], r.get("previous_status", "-")) for r in recs] == [
        ("status", "OL", None),
        ("status", "OB DISCHRG", "OL"),
        ("sample", "OB DISCHRG", "-"),
        ("sample", "OB DISCHRG", "-"),
        ("status", "OL CHRG", "OB DISCHRG"),
    ]
    assert [r["device_ts"] for r in recs] == [
        "2026-10-05T13:00:00.000+00:00",
        "2026-10-05T13:00:30.000+00:00",
        "2026-10-05T13:01:30.000+00:00",
        "2026-10-05T13:02:30.000+00:00",
        "2026-10-05T13:02:35.000+00:00",
    ]
    assert recs[1]["battery_voltage"] == 25.1 and recs[1]["input_voltage"] == 0.0
    assert all(r["device_serial"] == DEVICE_SERIAL for r in recs)
    assert srv.connections == 1
    runner.client.close()


def test_upsd_caido_solo_log_con_backoff_y_sin_registros(tmp_path, upsd_factory, caplog):
    ups = Ups()
    srv = upsd_factory(ups.handler)
    clock = FakeClock(utc(2026, 10, 5, 13, 0, 0))
    runner = make_runner(tmp_path, srv.port, clock)
    drive(runner, clock, 5)
    n_antes = len(audit_lines(tmp_path))
    ups.down = True
    with caplog.at_level(logging.INFO):
        delays = [runner.poll_once() for _ in range(7)]
        for d in delays:
            clock.advance(d)
        assert delays == [5, 10, 20, 40, 60, 60, 60] and BACKOFF_DELAYS[-1] == 60
        assert len(audit_lines(tmp_path)) == n_antes  # sin registros
        avisos = [r for r in caplog.records if "no responde" in r.getMessage()]
        assert len(avisos) == 1
        ups.down = False
        assert runner.poll_once() == 5.0
    assert any("respondió de nuevo" in r.getMessage() for r in caplog.records)
    # Volvió con el mismo estado: no hay status nuevo (todavía no vence el latido).
    assert len(audit_lines(tmp_path)) == n_antes
    # El backoff volvió a 5.
    ups.down = True
    assert runner.poll_once() == 5.0
    runner.client.close()


def test_err_de_upsd_es_como_caido(tmp_path, upsd_factory, caplog):
    ups = Ups()
    ups.err = "UNKNOWN-UPS"
    srv = upsd_factory(ups.handler)
    clock = FakeClock()
    runner = make_runner(tmp_path, srv.port, clock)
    with caplog.at_level(logging.WARNING):
        assert runner.poll_once() == 5.0
        assert runner.poll_once() == 10.0
    assert audit_lines(tmp_path) == []
    assert any("ups_name correcto" in r.getMessage() for r in caplog.records)


def test_aviso_repetido_cada_10_min_mientras_siga_caido(tmp_path, caplog):
    clock = FakeClock()
    runner = make_runner(tmp_path, 1, clock)  # puerto 1: conexión rechazada

    class Down:
        def list_var(self, ups):
            raise ConnectionRefusedError("rechazada")

        def close(self):
            pass

    runner.client = Down()
    with caplog.at_level(logging.WARNING):
        drive(runner, clock, 1300)
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("no responde" in m for m in msgs) == 1
    assert sum("sigue sin responder" in m for m in msgs) == 2


def test_hora_no_sincronizada_retiene_y_al_sincronizar_fecha_bien(tmp_path, upsd_factory):
    ups = Ups()
    srv = upsd_factory(ups.handler)
    clock = FakeClock(utc(2026, 10, 5, 10, 0, 0))  # reloj atrasado 3 h
    sync = Sync(False)
    runner = make_runner(tmp_path, srv.port, clock, sync=sync)
    ups.vars["ups.status"] = "OB DISCHRG"
    drive(runner, clock, 65)  # status (mono 1000) + sample (mono 1060)
    assert audit_lines(tmp_path) == [] and queued(runner) == []
    assert runner.gate.held_count == 2
    # NTP corrige: la pared salta +3 h; ese poll vuelve a preguntar y descarga.
    clock.wall = clock.wall.replace(hour=13)
    sync.synced = True
    ups.down = True  # aunque upsd esté caído, las retenidas salen
    runner.poll_once()
    recs = [json.loads(ln) for ln in audit_lines(tmp_path)]
    assert [(r["kind"], r["device_ts"]) for r in recs] == [
        ("status", "2026-10-05T13:00:00.000+00:00"),
        ("sample", "2026-10-05T13:01:00.000+00:00"),
    ]
    assert runner.gate.held_count == 0
    runner.client.close()


def test_sin_ups_status_no_reporta(tmp_path, upsd_factory, caplog):
    ups = Ups()
    del ups.vars["ups.status"]
    srv = upsd_factory(ups.handler)
    clock = FakeClock()
    runner = make_runner(tmp_path, srv.port, clock)
    with caplog.at_level(logging.WARNING):
        drive(runner, clock, 1000)
    assert audit_lines(tmp_path) == []
    assert sum("sin ups.status" in r.getMessage() for r in caplog.records) == 1
    runner.client.close()


def test_fanout_apagado_solo_audita(tmp_path, upsd_factory):
    srv = upsd_factory(Ups().handler)
    clock = FakeClock()
    runner = make_runner(tmp_path, srv.port, clock, fanout=False)
    drive(runner, clock, 5)
    assert len(audit_lines(tmp_path)) == 1
    runner.client.close()


def test_resumen_horario(tmp_path, upsd_factory, caplog):
    srv = upsd_factory(Ups().handler)
    clock = FakeClock()
    runner = make_runner(tmp_path, srv.port, clock)
    with caplog.at_level(logging.INFO):
        drive(runner, clock, SUMMARY_INTERVAL + 5)
    [resumen] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Resumen")]
    assert "lecturas=721" in resumen
    assert "status=1" in resumen and "heartbeat=4" in resumen
    assert "pendientes_cola=5" in resumen and "modo=stable" in resumen
    runner.client.close()


def test_poll_seconds_configurable(tmp_path, upsd_factory):
    srv = upsd_factory(Ups().handler)
    clock = FakeClock()
    runner = make_runner(tmp_path, srv.port, clock, poll_seconds=10)
    assert runner.poll_once() == 10.0
    runner.client.close()


def test_cola_rota_no_corta_el_muestreo(tmp_path, upsd_factory, caplog):
    import sqlite3

    srv = upsd_factory(Ups().handler)
    clock = FakeClock()
    runner = make_runner(tmp_path, srv.port, clock)

    def put(line):
        raise sqlite3.OperationalError("database or disk is full")

    runner.outbox.put = put
    with caplog.at_level(logging.ERROR):
        assert runner.poll_once() == 5.0
    assert len(audit_lines(tmp_path)) == 1
    assert any("solo en la auditoría" in r.getMessage() for r in caplog.records)
    runner.client.close()
