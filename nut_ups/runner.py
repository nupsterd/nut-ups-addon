"""Bucle principal: consulta upsd, muestrea, fecha y despacha (auditoría + cola).

Un solo hilo hace todo salvo el envío al backend (``OutboxSender``, hilo aparte).

- Cada ``poll_seconds``: ``LIST VAR <ups_name>``.
- Si upsd no responde (o responde ``ERR``): solo log, sin registros. El
  vigilante del backend detecta la falta de latidos. Reintento con backoff
  5/10/20/40/60 s que vuelve a 5 con la primera lectura buena.
- Una línea INFO por hora resume lecturas, errores, registros por tipo, pendientes
  de la cola y retenidos por hora no sincronizada.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime

from nut_ups.audit import AuditWriter
from nut_ups.clock import ClockGate
from nut_ups.config import Config
from nut_ups.nut import NutClient, NutError
from nut_ups.outbox import Outbox, OutboxSender
from nut_ups.records import Observation, Reading, build_record, serialize
from nut_ups.sampler import Sampler

log = logging.getLogger("nut_ups.runner")

BACKOFF_DELAYS = (5, 10, 20, 40, 60)
DOWN_REPEAT_WARN = 600
SUMMARY_INTERVAL = 3600


class Backoff:
    def __init__(self, delays: tuple[int, ...] = BACKOFF_DELAYS) -> None:
        self._delays = delays
        self._idx = 0

    def next(self) -> int:
        delay = self._delays[min(self._idx, len(self._delays) - 1)]
        self._idx += 1
        return delay

    def reset(self) -> None:
        self._idx = 0


class Runner:
    def __init__(
        self,
        cfg: Config,
        *,
        client: NutClient,
        sampler: Sampler,
        gate: ClockGate,
        audit: AuditWriter,
        outbox: Outbox | None = None,
        sender: OutboxSender | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.sampler = sampler
        self.gate = gate
        self.audit = audit
        self.outbox = outbox
        self.sender = sender
        self._monotonic = monotonic
        self.backoff = Backoff()
        self._down_since: float | None = None
        self._down_last_warn = 0.0
        self._down_attempts = 0
        self._no_status_warned = False
        self.counters: Counter[str] = Counter()
        self._summary_base: Counter[str] = Counter()
        self._next_summary = monotonic() + SUMMARY_INTERVAL

    def poll_once(self) -> float:
        """Una vuelta. Devuelve cuántos segundos esperar hasta la siguiente."""
        try:
            nut_vars = self.client.list_var(self.cfg.ups_name)
        except (OSError, NutError) as exc:
            self.counters["nut_errors"] += 1
            self._on_down(exc)
            self._dispatch(self.gate.flush())
            self._summary()
            return float(self.backoff.next())

        self.counters["polls"] += 1
        self._on_up()
        self.backoff.reset()
        mono = self._monotonic()
        reading = Reading.from_vars(nut_vars)
        if reading is None:
            if not self._no_status_warned:
                log.warning("upsd responde pero sin ups.status para %r: no se reporta nada.", self.cfg.ups_name)
                self._no_status_warned = True
            ready = self.gate.flush()
        else:
            self._no_status_warned = False
            obs = self.sampler.observe(reading, mono)
            ready = self.gate.submit(obs) if obs is not None else self.gate.flush()
        self._dispatch(ready)
        self._summary()
        return float(self.cfg.poll_seconds)

    def _dispatch(self, ready: list[tuple[Observation, datetime]]) -> None:
        for obs, device_ts in ready:
            record = build_record(obs, device_serial=self.cfg.device_serial, device_ts=device_ts)
            line = serialize(record)
            self.audit.write(line)
            if self.outbox is not None:
                try:
                    self.outbox.put(line)
                except sqlite3.Error as exc:
                    # La auditoría ya lo tiene; un disco lleno no debe cortar el muestreo.
                    log.error("No se pudo encolar el registro (%s); queda solo en la auditoría.", exc)
                else:
                    if self.sender is not None:
                        self.sender.notify()
            self.counters[obs.kind] += 1
            if obs.kind == "status":
                log.info(
                    "Estado de la UPS: %s → %s (batería %s V, entrada %s V).",
                    obs.previous_status or "(arranque)",
                    obs.reading.status,
                    obs.reading.battery_voltage,
                    obs.reading.input_voltage,
                )
            else:
                log.debug("Registro %s: %s", obs.kind, line)

    def _on_down(self, exc: BaseException) -> None:
        now = self._monotonic()
        self._down_attempts += 1
        detail = f"{type(exc).__name__}: {str(exc)[:200]}"
        if self._down_since is None:
            self._down_since = now
            self._down_last_warn = now
            hint = " (¿ups_name correcto?)" if isinstance(exc, NutError) and exc.code == "UNKNOWN-UPS" else ""
            log.warning(
                "upsd %s:%d no responde a LIST VAR %s: %s%s. Sin registros hasta que vuelva.",
                self.cfg.nut_host,
                self.cfg.nut_port,
                self.cfg.ups_name,
                detail,
                hint,
            )
        elif now - self._down_last_warn >= DOWN_REPEAT_WARN:
            self._down_last_warn = now
            log.warning(
                "upsd sigue sin responder hace %d min (%d intentos; último: %s).",
                int((now - self._down_since) // 60),
                self._down_attempts,
                detail,
            )

    def _on_up(self) -> None:
        if self._down_since is not None:
            log.info(
                "upsd respondió de nuevo tras %d s sin lecturas (%d intentos).",
                int(self._monotonic() - self._down_since),
                self._down_attempts,
            )
        elif self.counters["polls"] == 1:
            log.info("Conectado a upsd %s:%d (UPS %r).", self.cfg.nut_host, self.cfg.nut_port, self.cfg.ups_name)
        self._down_since = None
        self._down_attempts = 0

    def _summary(self) -> None:
        now = self._monotonic()
        if now < self._next_summary:
            return
        d = self.counters - self._summary_base
        pending = self.outbox.pending_count() if self.outbox is not None else None
        log.info(
            "Resumen última hora: lecturas=%d errores_nut=%d status=%d sample=%d heartbeat=%d "
            "retenidos_reloj=%d pendientes_cola=%s modo=%s",
            d["polls"],
            d["nut_errors"],
            d["status"],
            d["sample"],
            d["heartbeat"],
            self.gate.held_count,
            "n/a (fan-out apagado)" if pending is None else pending,
            self.sampler.mode.value,
        )
        self._summary_base = self.counters.copy()
        self._next_summary = now + SUMMARY_INTERVAL

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                delay = self.poll_once()
            except Exception:
                log.exception("Error inesperado en la vuelta de muestreo; reintento en 30 s.")
                self.client.close()
                delay = 30.0
            stop.wait(delay)
        self.client.close()
