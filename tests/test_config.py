from __future__ import annotations

import json
import logging
from dataclasses import fields
from pathlib import Path

import pytest
import requests
import yaml

from nut_ups import ADDON_VERSION
from nut_ups.config import Config
from nut_ups.main import build, startup
from nut_ups.outbox import Outbox, OutboxSender
from tests.conftest import BACKEND_SECRET, DEVICE_SERIAL, make_config

ROOT = Path(__file__).resolve().parent.parent


def _yaml() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def test_config_yaml_tiene_las_mismas_opciones_que_config():
    y = _yaml()
    nombres = [f.name for f in fields(Config)]
    assert list(y["options"]) == nombres
    assert list(y["schema"]) == nombres
    defaults = Config.from_dict(y["options"])
    for f in fields(Config):
        if f.name == "device_serial":
            continue  # obligatoria, sin default en la clase
        assert getattr(defaults, f.name) == f.default, f.name
        assert type(getattr(defaults, f.name)) is type(f.default), f.name


def test_config_yaml_metadatos():
    y = _yaml()
    assert y["slug"] == "nut_ups"
    assert y["name"] == "NUT UPS (Portería Virtual)"
    assert y["version"] == ADDON_VERSION == "0.1.0-alpha"
    assert y["boot"] == "auto" and y["startup"] == "services" and y["init"] is False
    assert y["hassio_api"] is True  # D10: /host/info → dt_synchronized
    assert "image" not in y  # build local en la Pi
    assert y["map"] == ["addon_config:rw"]
    s = y["schema"]
    assert s["log_level"] == "list(debug|info|warning)"
    assert s["backend_secret"] == "password?"
    assert s["device_serial"] == "str"  # obligatoria
    assert s["nut_port"] == "port"
    assert s["float_voltage"] == "float(1,100)"


def test_defaults_del_diseno():
    c = make_config()
    assert (c.nut_host, c.nut_port, c.ups_name) == ("a0d7b954-nut", 3493, "epu1200")
    assert (c.poll_seconds, c.float_voltage, c.float_confirm_samples) == (5, 27.0, 5)
    assert c.outbox_max_age_days == 30  # D11
    assert c.backend_enabled is False


def test_from_options_json(tmp_path):
    p = tmp_path / "options.json"
    opts = {**_yaml()["options"], "device_serial": DEVICE_SERIAL, "float_voltage": 26, "poll_seconds": "10"}
    p.write_text(json.dumps(opts))
    cfg = Config.from_options_json(str(p))
    assert cfg.float_voltage == 26.0 and isinstance(cfg.float_voltage, float)
    assert cfg.poll_seconds == 10
    assert cfg.validate() == []


@pytest.mark.parametrize(
    ("overrides", "fragmento"),
    [
        ({"device_serial": "  "}, "device_serial"),
        ({"nut_host": ""}, "nut_host"),
        ({"ups_name": "epu 1200"}, "ups_name"),
        ({"ups_name": "epu\nLOGOUT"}, "ups_name"),
        ({"nut_port": 0}, "nut_port"),
        ({"poll_seconds": 0}, "poll_seconds"),
        ({"float_voltage": 0}, "float_voltage"),
        ({"float_confirm_samples": 0}, "float_confirm_samples"),
        ({"log_level": "trace"}, "log_level"),
    ],
)
def test_validacion(overrides, fragmento):
    errores = make_config(**overrides).validate()
    assert len(errores) == 1 and fragmento in errores[0]


def test_config_valida():
    assert make_config().validate() == []


def test_arranque_con_config_invalida_sale_con_error(caplog):
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as ei:
        startup(make_config(device_serial=""))
    assert ei.value.code == 1
    assert any("device_serial" in r.getMessage() for r in caplog.records)


def test_arranque_sin_supervisor_token_avisa(monkeypatch, caplog, tmp_path):
    from nut_ups import clock

    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(clock, "S6_ENV_DIR", str(tmp_path))
    with caplog.at_level(logging.ERROR):
        startup(make_config())
    assert any("SUPERVISOR_TOKEN" in r.getMessage() for r in caplog.records)


def test_arranque_informa_las_banderas_significativas(monkeypatch, caplog):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "x")
    with caplog.at_level(logging.INFO):
        startup(make_config())
    assert "Registro status solo si cambian las banderas FSD LB OB OFF OL (N4)." in caplog.messages


def test_secretos_nunca_en_el_log(tmp_path, caplog, monkeypatch):
    """backend_secret no aparece en ningún log (solo configurado/vacío)."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "x")
    cfg = make_config(
        backend_url="https://api.example.test/api/v1/eventos/ups",
        backend_secret=BACKEND_SECRET,
        audit_dir=str(tmp_path / "audit"),
        log_level="debug",
    )
    assert BACKEND_SECRET not in repr(cfg)
    with caplog.at_level(logging.DEBUG):
        startup(cfg)
        runner, sender = build(cfg, outbox_path=str(tmp_path / "outbox.sqlite"))
        assert sender is not None and runner.outbox is not None
        ob = Outbox(tmp_path / "o2.sqlite", 1000, 30)
        ob.put("{}")

        class R401:
            status_code = 401
            text = '{"detail":"token invalido"}'

        OutboxSender(ob, cfg.backend_url, BACKEND_SECRET, 5, post=lambda *a: R401()).process_one()
        OutboxSender(
            ob, cfg.backend_url, BACKEND_SECRET, 5, post=lambda *a: (_ for _ in ()).throw(requests.ConnectionError("x"))
        ).process_one()
    texto = caplog.text
    assert "backend_secret = configurado" in texto
    assert f"device_serial = {DEVICE_SERIAL}" in texto
    assert BACKEND_SECRET not in texto


def test_build_sin_backend(tmp_path):
    runner, sender = build(make_config(audit_dir=str(tmp_path)), outbox_path=str(tmp_path / "o.sqlite"))
    assert sender is None and runner.outbox is None
    assert not (tmp_path / "o.sqlite").exists()
