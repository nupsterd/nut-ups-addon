"""Lectura de la UPS, registros del contrato con el backend y su serialización única.

El registro se serializa UNA sola vez (``serialize``) y esa misma cadena va a la
auditoría y a la cola: el POST reenvía siempre exactamente los mismos bytes.

Contrato (``POST <backend_url>``, header ``X-PV-UPS-Token``)::

    kind             "status" | "sample" | "heartbeat"
    device_serial    str (opción de config)
    device_ts        ISO-8601 en UTC con milisegundos ("...T13:05:00.123+00:00")
    ups_status       str, ups.status crudo ("OL", "OB DISCHRG", "OB LB"...)
    previous_status  str | null — SOLO en kind=status (null en el primero tras arrancar)
    battery_voltage, battery_charge, input_voltage, input_frequency,
    ups_temperature  float | null
    addon_version    str
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from nut_ups import ADDON_VERSION

KINDS = ("status", "sample", "heartbeat")

# Variable de NUT → campo del registro. ``ups.load`` NO se usa: en la EPU1200 vale siempre 0.
NUMERIC_VARS = {
    "battery.voltage": "battery_voltage",
    "battery.charge": "battery_charge",
    "input.voltage": "input_voltage",
    "input.frequency": "input_frequency",
    "ups.temperature": "ups_temperature",
}


def parse_float(value: str | None) -> float | None:
    """Número de NUT → float; ``None`` si falta, no es número o no es finito."""
    if value is None:
        return None
    try:
        out = float(value.strip())
    except ValueError:
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class Reading:
    """Una respuesta de ``LIST VAR`` reducida a lo que se reporta."""

    status: str
    battery_voltage: float | None = None
    battery_charge: float | None = None
    input_voltage: float | None = None
    input_frequency: float | None = None
    ups_temperature: float | None = None

    @classmethod
    def from_vars(cls, nut_vars: dict[str, str]) -> Reading | None:
        """``None`` si falta ``ups.status`` (sin estado no hay nada que reportar)."""
        status = " ".join(nut_vars.get("ups.status", "").split())
        if not status:
            return None
        nums = {field: parse_float(nut_vars.get(var)) for var, field in NUMERIC_VARS.items()}
        return cls(status=status, **nums)

    @property
    def flags(self) -> frozenset[str]:
        return frozenset(self.status.split())

    @property
    def on_battery(self) -> bool:
        return "OB" in self.flags


@dataclass(frozen=True)
class Observation:
    """Algo para reportar, con el instante monotónico de la lectura (D10)."""

    kind: str
    reading: Reading
    mono: float
    previous_status: str | None = None


def iso_ms_utc(dt: datetime) -> str:
    """ISO 8601 en UTC con milisegundos. Rechaza datetimes naive."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("device_ts tiene que ser aware")
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def build_record(obs: Observation, *, device_serial: str, device_ts: datetime) -> dict[str, Any]:
    if obs.kind not in KINDS:
        raise ValueError(f"kind desconocido: {obs.kind!r}")
    r = obs.reading
    record: dict[str, Any] = {
        "kind": obs.kind,
        "device_serial": device_serial,
        "device_ts": iso_ms_utc(device_ts),
        "ups_status": r.status,
    }
    if obs.kind == "status":
        record["previous_status"] = obs.previous_status
    record.update(
        {
            "battery_voltage": r.battery_voltage,
            "battery_charge": r.battery_charge,
            "input_voltage": r.input_voltage,
            "input_frequency": r.input_frequency,
            "ups_temperature": r.ups_temperature,
            "addon_version": ADDON_VERSION,
        }
    )
    return record


def serialize(record: dict[str, Any]) -> str:
    """Serialización única y estable (una línea, sin espacios, orden de inserción)."""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
