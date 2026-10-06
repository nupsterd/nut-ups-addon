"""Configuración del add-on, leída de ``/data/options.json`` (la escribe el Supervisor).

Los nombres, tipos y defaults de ``Config`` son los mismos que ``options`` de
``config.yaml`` (un test lo verifica cargando el YAML).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from typing import Any

OPTIONS_PATH = "/data/options.json"
LOG_LEVELS = ("debug", "info", "warning")
# ups_name va dentro de un comando de texto de NUT: nada de espacios, comillas ni saltos de línea.
_UPS_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class Config:
    device_serial: str
    backend_url: str = ""
    # repr=False: un log de la config o un traceback nunca imprime secretos.
    backend_secret: str = field(default="", repr=False)
    backend_timeout_seconds: int = 5
    nut_host: str = "a0d7b954-nut"
    nut_port: int = 3493
    ups_name: str = "epu1200"
    poll_seconds: int = 5
    float_voltage: float = 27.0
    float_confirm_samples: int = 5
    outbox_max_records: int = 100000
    outbox_max_age_days: int = 30
    audit_dir: str = "/config"
    audit_retention_days: int = 30
    log_level: str = "info"

    @classmethod
    def from_dict(cls, opts: dict[str, Any]) -> Config:
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in opts or opts[f.name] is None:
                continue
            value = opts[f.name]
            if f.type == "int":
                kwargs[f.name] = int(value)
            elif f.type == "float":
                kwargs[f.name] = float(value)
            else:
                kwargs[f.name] = str(value)
        return cls(**kwargs)

    @classmethod
    def from_options_json(cls, path: str = OPTIONS_PATH) -> Config:
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    @property
    def backend_enabled(self) -> bool:
        return bool(self.backend_url.strip())

    def validate(self) -> list[str]:
        """Errores de configuración que impiden arrancar (lista vacía = OK)."""
        errores: list[str] = []
        if not self.device_serial.strip():
            errores.append("device_serial está vacío (es el serial con el que la UPS está dada de alta)")
        if not self.nut_host.strip():
            errores.append("nut_host está vacío")
        if not _UPS_NAME_RE.match(self.ups_name):
            errores.append(f"ups_name inválido: {self.ups_name!r} (solo letras, números, '_', '.', '-')")
        if not 1 <= self.nut_port <= 65535:
            errores.append(f"nut_port fuera de rango: {self.nut_port}")
        if not 1 <= self.poll_seconds <= 60:
            errores.append(f"poll_seconds fuera de rango (1-60): {self.poll_seconds}")
        if self.float_voltage <= 0:
            errores.append(f"float_voltage debe ser > 0: {self.float_voltage}")
        if self.float_confirm_samples < 1:
            errores.append(f"float_confirm_samples debe ser >= 1: {self.float_confirm_samples}")
        if self.log_level not in LOG_LEVELS:
            errores.append(f"log_level inválido: {self.log_level!r}")
        return errores

    def describe(self) -> dict[str, Any]:
        """Config para el log de arranque: los secretos solo como configurado/vacío."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "backend_secret":
                value = "configurado" if value else "vacío"
            elif f.name == "backend_url" and not value:
                value = "(fan-out apagado)"
            out[f.name] = value
        return out
