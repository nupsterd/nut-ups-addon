"""Entry point del add-on: ``python3 -m nut_ups.main``."""

from __future__ import annotations

import logging
import signal
import sys
import threading

from nut_ups import ADDON_VERSION
from nut_ups.audit import AuditWriter
from nut_ups.clock import ClockGate, supervisor_sync_check, supervisor_token
from nut_ups.config import OPTIONS_PATH, Config
from nut_ups.nut import NutClient
from nut_ups.outbox import OUTBOX_PATH, Outbox, OutboxSender
from nut_ups.runner import Runner
from nut_ups.sampler import Sampler

log = logging.getLogger("nut_ups")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.getLogger().setLevel(level.upper())
    # urllib3 en DEBUG imprime cada request; no aporta y ensucia el log.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def load_config(path: str = OPTIONS_PATH) -> Config:
    try:
        return Config.from_options_json(path)
    except FileNotFoundError:
        logging.basicConfig(level="INFO")
        log.error("%s no existe. ¿Está corriendo dentro del add-on?", path)
        sys.exit(1)
    except (KeyError, TypeError, ValueError) as exc:
        logging.basicConfig(level="INFO")
        log.error("Configuración inválida en %s: %s", path, exc)
        sys.exit(1)


def startup(cfg: Config) -> None:
    """Valida y loguea la config. Sale con código 1 si es inválida."""
    setup_logging(cfg.log_level)
    errores = cfg.validate()
    for err in errores:
        log.error("Configuración inválida: %s", err)
    if errores:
        sys.exit(1)
    log.info("NUT UPS (Portería Virtual) %s", ADDON_VERSION)
    for key, value in cfg.describe().items():
        log.info("  %s = %s", key, value)
    if not supervisor_token():
        log.error(
            "Sin SUPERVISOR_TOKEN: no se puede confirmar la hora de la Pi y los registros quedan "
            "retenidos en memoria (¿falta hassio_api: true en config.yaml?)."
        )


def build(cfg: Config, outbox_path: str = OUTBOX_PATH) -> tuple[Runner, OutboxSender | None]:
    audit = AuditWriter(cfg.audit_dir, cfg.audit_retention_days)
    audit.purge()
    outbox: Outbox | None = None
    sender: OutboxSender | None = None
    if cfg.backend_enabled:
        outbox = Outbox(outbox_path, cfg.outbox_max_records, cfg.outbox_max_age_days)
        sender = OutboxSender(outbox, cfg.backend_url, cfg.backend_secret, cfg.backend_timeout_seconds)
        log.info(
            "Fan-out al backend ACTIVO: %s (%d registros pendientes en la cola).",
            cfg.backend_url,
            outbox.pending_count(),
        )
    else:
        log.info("Fan-out al backend APAGADO (backend_url vacío): solo auditoría local.")
    runner = Runner(
        cfg,
        client=NutClient(cfg.nut_host, cfg.nut_port),
        sampler=Sampler(cfg.float_voltage, cfg.float_confirm_samples),
        gate=ClockGate(supervisor_sync_check()),
        audit=audit,
        outbox=outbox,
        sender=sender,
    )
    return runner, sender


def main() -> None:
    cfg = load_config()
    startup(cfg)
    stop = threading.Event()

    def on_signal(signum: int, _frame: object) -> None:
        log.info("Señal %s recibida: saliendo (la cola persiste en disco).", signum)
        stop.set()
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    runner, sender = build(cfg)
    if sender is not None:
        threading.Thread(target=sender.run, args=(stop,), name="outbox-sender", daemon=True).start()
    try:
        runner.run(stop)
    finally:
        # La cola SQLite no se cierra acá: el hilo de envío puede estar usándola y
        # cada commit ya quedó en disco (WAL + synchronous=FULL).
        runner.audit.close()


if __name__ == "__main__":
    main()
