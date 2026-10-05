"""Fixtures compartidas: reloj falso, config de prueba y un upsd falso en loopback."""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nut_ups.config import Config

BOGOTA = ZoneInfo("America/Bogota")

BACKEND_SECRET = "Backend-Token-no-loguear-9920"
DEVICE_SERIAL = "UPS-EXAMPLE-0001"
UPS = "epu1200"


def make_config(**overrides: object) -> Config:
    base: dict[str, object] = {
        "device_serial": DEVICE_SERIAL,
        "audit_dir": "/tmp/no-usado",
    }
    base.update(overrides)
    return Config.from_dict(base)


class FakeClock:
    """Reloj monotónico + de pared controlables; ``sleep`` avanza ambos y registra."""

    def __init__(self, start_wall: datetime | None = None) -> None:
        self.mono = 1000.0
        self.wall = start_wall or datetime(2026, 9, 23, 8, 15, 34, 120000, tzinfo=BOGOTA)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.mono

    def now(self) -> datetime:
        return self.wall

    def time(self) -> float:
        return self.wall.timestamp()

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall = self.wall + timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def list_var_response(ups: str, nut_vars: dict[str, str]) -> bytes:
    lines = [f"BEGIN LIST VAR {ups}"]
    for name, value in nut_vars.items():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'VAR {ups} {name} "{escaped}"')
    lines.append(f"END LIST VAR {ups}")
    return ("\n".join(lines) + "\n").encode()


# Variables verificadas en la EPU1200 de la oficina (NUT 2.8.5), en flotación. Valores de
# ejemplo salvo battery.voltage (27.2) y ups.load (siempre 0). No hay ups.serial/mfr/model.
EPU1200_OL = {
    "battery.charge": "100",
    "battery.voltage": "27.20",
    "input.frequency": "60.0",
    "input.voltage": "121.3",
    "ups.load": "0",
    "ups.status": "OL",
    "ups.temperature": "30.0",
}


class FakeUpsd:
    """upsd falso en 127.0.0.1. ``handler(línea) -> bytes | None``; ``None`` cierra la conexión."""

    def __init__(self, handler: Callable[[str], bytes | None]) -> None:
        self.handler = handler
        self.commands: list[str] = []
        self.connections = 0
        self._srv = socket.create_server(("127.0.0.1", 0))
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._srv.settimeout(0.1)
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rb") as rf:
            for raw in rf:
                line = raw.decode().rstrip("\n")
                self.commands.append(line)
                out = self.handler(line)
                if out is None:
                    return
                conn.sendall(out)

    def close(self) -> None:
        self._stop.set()
        self._srv.close()
        self._thread.join(timeout=2)


@pytest.fixture
def upsd_factory() -> Iterator[Callable[[Callable[[str], bytes | None]], FakeUpsd]]:
    servers: list[FakeUpsd] = []

    def factory(handler: Callable[[str], bytes | None]) -> FakeUpsd:
        srv = FakeUpsd(handler)
        servers.append(srv)
        return srv

    yield factory
    for srv in servers:
        srv.close()
