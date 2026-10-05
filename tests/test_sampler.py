from __future__ import annotations

import logging

from nut_ups.records import Reading
from nut_ups.sampler import HEARTBEAT_SECONDS, RECOVERY_MAX_SECONDS, SAMPLE_SECONDS, Mode, Sampler

POLL = 5


def r(status: str, volts: float | None = 27.2) -> Reading:
    return Reading(status=status, battery_voltage=volts)


class Sim:
    """Corre el sampler a ``POLL`` s por lectura y junta (segundo, kind, status, previous)."""

    def __init__(self, float_voltage: float = 27.0, confirm: int = 5) -> None:
        self.s = Sampler(float_voltage, confirm)
        self.t = 0.0
        self.out: list[tuple[float, str, str, str | None]] = []

    def run(self, seconds: float, reading: Reading) -> list[tuple[float, str, str, str | None]]:
        nuevos = []
        end = self.t + seconds
        while self.t < end:
            obs = self.s.observe(reading, self.t)
            if obs is not None:
                nuevos.append((self.t, obs.kind, obs.reading.status, obs.previous_status))
            self.t += POLL
        self.out += nuevos
        return nuevos

    def kinds(self, items) -> list[str]:
        return [k for _, k, _, _ in items]


def test_constantes_del_diseno():
    assert (SAMPLE_SECONDS, HEARTBEAT_SECONDS, RECOVERY_MAX_SECONDS) == (60, 900, 12 * 3600)


def test_arranque_emite_status_con_previous_null_y_luego_latidos():
    sim = Sim()
    out = sim.run(3600, r("OL"))
    assert out[0] == (0.0, "status", "OL", None)
    assert [(t, k) for t, k, _, _ in out[1:]] == [(900.0, "heartbeat"), (1800.0, "heartbeat"), (2700.0, "heartbeat")]
    assert sim.s.mode is Mode.STABLE


def test_corte_completo_ol_ob_lb_ol_flotacion():
    sim = Sim()
    sim.run(100, r("OL"))  # arranque: status
    # Se corta la red.
    ob = sim.run(300, r("OB DISCHRG", 25.4))
    assert ob[0][1:] == ("status", "OB DISCHRG", "OL")
    assert [t for t, k, _, _ in ob[1:]] == [ob[0][0] + 60 * i for i in range(1, 5)]
    assert set(sim.kinds(ob[1:])) == {"sample"}
    assert sim.s.mode is Mode.ON_BATTERY
    # Batería baja: cambio de estado, sigue en batería.
    lb = sim.run(125, r("OB DISCHRG LB", 22.9))
    assert lb[0][1:] == ("status", "OB DISCHRG LB", "OB DISCHRG")
    assert sim.kinds(lb[1:]) == ["sample", "sample"]
    # Vuelve la red: recarga, muestra por minuto debajo de la flotación.
    back = sim.run(600, r("OL CHRG", 26.1))
    assert back[0][1:] == ("status", "OL CHRG", "OB DISCHRG LB")
    assert sim.s.mode is Mode.RECOVERY
    assert sim.kinds(back[1:]) == ["sample"] * 9
    # Llega a flotación: hacen falta 5 muestras SEGUIDAS >= 27.0 V.
    flot = sim.run(600, r("OL", 27.2))
    assert flot[0][1:] == ("status", "OL", "OL CHRG")
    assert sim.kinds(flot[1:]) == ["sample"] * 5  # la 5.ª cierra la recarga
    assert sim.s.mode is Mode.STABLE
    # Después, solo latido cada 15 min desde el último registro.
    ultimo = flot[-1][0]
    after = sim.run(1800, r("OL", 27.2))
    assert [(t - ultimo, k) for t, k, _, _ in after] == [(900.0, "heartbeat"), (1800.0, "heartbeat")]


def test_flotacion_exige_muestras_seguidas():
    sim = Sim(confirm=3)
    sim.run(10, r("OL"))
    sim.run(10, r("OB"))
    sim.run(5, r("OL", 26.0))
    assert sim.s.mode is Mode.RECOVERY
    sim.run(120, r("OL", 27.1))  # 2 muestras >= 27.0
    sim.run(60, r("OL", 26.9))  # cae: la racha vuelve a 0
    sim.run(120, r("OL", 27.1))  # 2 más: todavía no
    assert sim.s.mode is Mode.RECOVERY
    sim.run(60, r("OL", 27.1))  # la 3.ª seguida
    assert sim.s.mode is Mode.STABLE


def test_tension_desconocida_no_cuenta_como_flotacion():
    sim = Sim(confirm=2)
    sim.run(10, r("OL"))
    sim.run(10, r("OB"))
    sim.run(5, r("OL", 26.0))
    sim.run(600, r("OL", None))
    assert sim.s.mode is Mode.RECOVERY


def test_recarga_termina_a_las_12_h_sin_flotacion(caplog):
    sim = Sim()
    sim.run(10, r("OL"))
    sim.run(10, r("OB"))
    sim.run(5, r("OL", 25.0))
    inicio = sim.t - POLL
    with caplog.at_level(logging.WARNING):
        out = sim.run(RECOVERY_MAX_SECONDS + 1800, r("OL", 25.0))
    samples = [t for t, k, _, _ in out if k == "sample"]
    heartbeats = [t for t, k, _, _ in out if k == "heartbeat"]
    assert len(samples) == RECOVERY_MAX_SECONDS // 60 - 1
    assert max(samples) < inicio + RECOVERY_MAX_SECONDS
    assert heartbeats and min(heartbeats) >= inicio + RECOVERY_MAX_SECONDS
    assert sim.s.mode is Mode.STABLE
    assert any("no llegó a flotación" in rec.getMessage() for rec in caplog.records)


def test_nuevo_corte_durante_la_recarga_vuelve_a_batería_y_reinicia():
    sim = Sim(confirm=2)
    sim.run(10, r("OL"))
    sim.run(10, r("OB"))
    sim.run(65, r("OL", 27.5))  # 1 muestra en flotación (racha 1 de 2)
    sim.run(10, r("OB", 25.0))
    assert sim.s.mode is Mode.ON_BATTERY
    sim.run(5, r("OL", 27.5))
    assert sim.s.mode is Mode.RECOVERY
    sim.run(60, r("OL", 27.5))  # la racha arrancó de cero: 1 de 2
    assert sim.s.mode is Mode.RECOVERY


def test_cambio_entre_estados_con_red_no_corta_la_recarga():
    sim = Sim()
    sim.run(10, r("OL"))
    sim.run(10, r("OB"))
    sim.run(5, r("OL CHRG", 26.0))
    sim.run(5, r("OL", 26.0))
    assert sim.s.mode is Mode.RECOVERY


def test_arranque_con_bateria_baja_entra_en_recarga():
    sim = Sim()
    out = sim.run(300, r("OL", 25.9))
    assert out[0][1:] == ("status", "OL", None)
    assert sim.kinds(out[1:]) == ["sample"] * 4
    assert sim.s.mode is Mode.RECOVERY


def test_arranque_en_bateria():
    sim = Sim()
    out = sim.run(125, r("OB DISCHRG", 25.0))
    assert out[0][1:] == ("status", "OB DISCHRG", None)
    assert sim.kinds(out[1:]) == ["sample", "sample"]


def test_cambios_repetidos_un_status_por_cambio_y_nada_si_se_repite():
    sim = Sim()
    seq = ["OL", "OL", "OB", "OB", "OL", "OB", "OB", "OL", "OL"]
    out = []
    for status in seq:
        out += sim.run(POLL, r(status))
    assert [(k, s, p) for _, k, s, p in out] == [
        ("status", "OL", None),
        ("status", "OB", "OL"),
        ("status", "OL", "OB"),
        ("status", "OB", "OL"),
        ("status", "OL", "OB"),
    ]


def test_un_registro_por_lectura_como_maximo():
    s = Sampler(27.0, 5)
    s.observe(r("OB"), 0)
    # Vence la muestra y cambia el estado en la misma lectura: sale solo el status.
    obs = s.observe(r("OB LB"), 60)
    assert obs.kind == "status"
    assert s.observe(r("OB LB"), 61) is None
    assert s.observe(r("OB LB"), 120).kind == "sample"
