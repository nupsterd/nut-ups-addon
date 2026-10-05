"""Hora real de cada registro (D10): ``device_ts`` solo con el reloj de la Pi sincronizado.

Una Pi sin RTC que vuelve de un corte total arranca con la hora corrida hasta
que sincroniza NTP. Antes de fijar ``device_ts`` se pregunta al Supervisor
(``GET /host/info`` → ``dt_synchronized``; requiere ``hassio_api: true``). Si
no está sincronizado, o la consulta falla, la observación queda RETENIDA en
memoria con su instante monotónico. Cuando el reloj sincroniza, cada retenida
recibe ``device_ts = ahora - (mono_ahora - mono_lectura)``, en orden.

Las retenidas viven solo en memoria: si el add-on se reinicia antes de que el
reloj sincronice, se pierden (no hay forma de fecharlas después).
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import requests

from nut_ups.records import Observation

log = logging.getLogger("nut_ups.clock")

SUPERVISOR_HOST_INFO = "http://supervisor/host/info"
# s6-overlay v3 (imagen base de HA) NO pasa el entorno del contenedor al CMD: el token que
# inyecta el Supervisor queda solo en este archivo. Verificado en amd64-base:3.21.
S6_ENV_DIR = "/run/s6/container_environment"
SUPERVISOR_TIMEOUT = 3.0
MAX_HELD = 20000  # ~14 días de muestras por minuto (meses de latidos)
WARN_INTERVAL = 600

# Devuelve (sincronizado, motivo si no lo está).
SyncCheck = Callable[[], tuple[bool, str]]


def supervisor_token(s6_env_dir: str | None = None) -> str:
    """``SUPERVISOR_TOKEN`` del entorno o, si no está, del entorno guardado por s6-overlay."""
    token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    if token:
        return token
    try:
        with open(os.path.join(s6_env_dir or S6_ENV_DIR, "SUPERVISOR_TOKEN"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def supervisor_sync_check(token: str | None = None, url: str = SUPERVISOR_HOST_INFO) -> SyncCheck:
    token = token if token is not None else supervisor_token()

    def check() -> tuple[bool, str]:
        if not token:
            return False, "sin SUPERVISOR_TOKEN (¿falta hassio_api: true?)"
        try:
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=SUPERVISOR_TIMEOUT)
        except requests.RequestException as exc:
            return False, f"Supervisor no responde ({type(exc).__name__})"
        if resp.status_code != 200:
            return False, f"Supervisor respondió HTTP {resp.status_code}"
        try:
            synced = resp.json()["data"]["dt_synchronized"]
        except (ValueError, KeyError, TypeError):
            return False, "respuesta de /host/info sin data.dt_synchronized"
        if synced is True:
            return True, ""
        return False, "dt_synchronized = false"

    return check


class ClockGate:
    """Fecha las observaciones solo con el reloj sincronizado; retiene las demás (FIFO)."""

    def __init__(
        self,
        check: SyncCheck,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        max_held: int = MAX_HELD,
    ) -> None:
        self._check = check
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._held: deque[Observation] = deque()
        self._max_held = max_held
        self._last_warn: float | None = None
        self._dropped = 0

    @property
    def held_count(self) -> int:
        return len(self._held)

    def submit(self, obs: Observation) -> list[tuple[Observation, datetime]]:
        self._held.append(obs)
        if len(self._held) > self._max_held:
            self._held.popleft()
            self._dropped += 1
        return self.flush()

    def flush(self) -> list[tuple[Observation, datetime]]:
        """Si hay retenidas y el reloj está sincronizado, las devuelve fechadas, en orden."""
        if not self._held:
            return []
        try:
            synced, why = self._check()
        except Exception as exc:  # noqa: BLE001 - cualquier falla = no sincronizado
            synced, why = False, f"{type(exc).__name__}: {exc}"
        if not synced:
            self._warn(why)
            return []
        now_wall = self._now()
        if now_wall.tzinfo is None:
            raise ValueError("now() tiene que devolver un datetime aware")
        now_mono = self._monotonic()
        out = [(obs, now_wall - timedelta(seconds=now_mono - obs.mono)) for obs in self._held]
        if self._last_warn is not None or self._dropped:
            log.info(
                "Reloj sincronizado: %d registro(s) retenido(s) fechado(s) con la hora real%s.",
                len(out),
                f" ({self._dropped} descartado(s) por exceder {self._max_held})" if self._dropped else "",
            )
        self._held.clear()
        self._last_warn = None
        self._dropped = 0
        return out

    def _warn(self, why: str) -> None:
        mono = self._monotonic()
        if self._last_warn is not None and mono - self._last_warn < WARN_INTERVAL:
            return
        self._last_warn = mono
        log.warning(
            "Hora de la Pi NO confirmada (%s): %d registro(s) retenido(s) en memoria hasta que sincronice%s.",
            why,
            len(self._held),
            f"; {self._dropped} descartado(s), los más viejos" if self._dropped else "",
        )
