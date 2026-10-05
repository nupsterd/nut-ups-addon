"""Máquina de muestreo: decide qué lecturas de ``LIST VAR`` se reportan.

Como mucho UN registro por lectura, con esta prioridad:

1. ``status`` en cada cambio de ``ups.status`` (con ``previous_status``). La
   primera lectura tras arrancar también es un ``status``, con
   ``previous_status = null``.
2. ``sample`` cada ``SAMPLE_SECONDS`` (60 s) mientras la UPS esté en batería (``OB``).
3. ``sample`` cada 60 s en la RECARGA: después de un episodio ``OB``, ya con
   red, hasta que ``battery.voltage >= float_voltage`` en
   ``float_confirm_samples`` muestras SEGUIDAS (flotación), o hasta
   ``RECOVERY_MAX_SECONDS`` (12 h). Así el backend puede evaluar la batería
   restablecida (D5 b) con datos reales y no solo con el latido.
4. ``heartbeat`` cada ``HEARTBEAT_SECONDS`` (15 min) con red estable.

Los intervalos se cuentan desde el último registro emitido (de cualquier tipo).

Si el add-on arranca con red y la batería por debajo de ``float_voltage`` (la
Pi se apagó en un corte y volvió con la batería descargada, o el add-on se
reinició en medio de una recarga), también entra en recarga: el episodio
``OB`` anterior se perdió con el proceso.
"""

from __future__ import annotations

import enum
import logging

from nut_ups.records import Observation, Reading

log = logging.getLogger("nut_ups.sampler")

SAMPLE_SECONDS = 60
HEARTBEAT_SECONDS = 15 * 60
RECOVERY_MAX_SECONDS = 12 * 3600


class Mode(enum.Enum):
    STABLE = "stable"
    ON_BATTERY = "on_battery"
    RECOVERY = "recovery"


class Sampler:
    def __init__(
        self,
        float_voltage: float,
        float_confirm_samples: int,
        sample_seconds: float = SAMPLE_SECONDS,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        recovery_max_seconds: float = RECOVERY_MAX_SECONDS,
    ) -> None:
        self._float_voltage = float_voltage
        self._confirm = float_confirm_samples
        self._sample_s = sample_seconds
        self._heartbeat_s = heartbeat_seconds
        self._recovery_max_s = recovery_max_seconds
        self.mode = Mode.STABLE
        self.status: str | None = None
        self._last_emit: float | None = None
        self._recovery_start = 0.0
        self._float_streak = 0

    def observe(self, reading: Reading, mono: float) -> Observation | None:
        if reading.status != self.status:
            previous = self.status
            self.status = reading.status
            self._on_status_change(reading, mono, first=previous is None)
            return self._emit("status", reading, mono, previous)

        since = mono - (self._last_emit if self._last_emit is not None else mono)
        if self.mode is Mode.ON_BATTERY:
            return self._emit("sample", reading, mono) if since >= self._sample_s else None
        if self.mode is Mode.RECOVERY:
            if mono - self._recovery_start >= self._recovery_max_s:
                log.warning(
                    "Recarga: la batería no llegó a flotación (%.2f V x %d muestras) en %d h; "
                    "se vuelve al latido de %d min (última tensión: %s V).",
                    self._float_voltage,
                    self._confirm,
                    int(self._recovery_max_s // 3600),
                    int(self._heartbeat_s // 60),
                    reading.battery_voltage,
                )
                self.mode = Mode.STABLE
            elif since >= self._sample_s:
                self._count_float(reading)
                return self._emit("sample", reading, mono)
            else:
                return None
        return self._emit("heartbeat", reading, mono) if since >= self._heartbeat_s else None

    def _on_status_change(self, reading: Reading, mono: float, *, first: bool) -> None:
        if reading.on_battery:
            self.mode = Mode.ON_BATTERY
            return
        if self.mode is Mode.ON_BATTERY:
            self._start_recovery(mono, "fin del episodio en batería")
        elif first and reading.battery_voltage is not None and reading.battery_voltage < self._float_voltage:
            self._start_recovery(
                mono, f"arranque con la batería en {reading.battery_voltage:.2f} V (< {self._float_voltage:.2f} V)"
            )
        # Un cambio entre estados con red (p. ej. "OL CHRG" → "OL") no corta la recarga.

    def _start_recovery(self, mono: float, why: str) -> None:
        self.mode = Mode.RECOVERY
        self._recovery_start = mono
        self._float_streak = 0
        log.info(
            "Recarga (%s): muestra cada %d s hasta %d muestras seguidas >= %.2f V (máx. %d h).",
            why,
            int(self._sample_s),
            self._confirm,
            self._float_voltage,
            int(self._recovery_max_s // 3600),
        )

    def _count_float(self, reading: Reading) -> None:
        v = reading.battery_voltage
        self._float_streak = self._float_streak + 1 if v is not None and v >= self._float_voltage else 0
        if self._float_streak >= self._confirm:
            log.info(
                "Batería en flotación (%d muestras seguidas >= %.2f V): fin de la recarga, latido cada %d min.",
                self._confirm,
                self._float_voltage,
                int(self._heartbeat_s // 60),
            )
            self.mode = Mode.STABLE

    def _emit(self, kind: str, reading: Reading, mono: float, previous: str | None = None) -> Observation:
        self._last_emit = mono
        return Observation(kind=kind, reading=reading, mono=mono, previous_status=previous)
