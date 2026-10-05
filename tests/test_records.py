"""Contrato del payload con pv-backend B1 (POST /api/v1/eventos/ups)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from nut_ups import ADDON_VERSION
from nut_ups.records import Observation, Reading, build_record, iso_ms_utc, parse_float, serialize
from tests.conftest import BOGOTA, DEVICE_SERIAL, EPU1200_OL, utc

NUMERIC = ("battery_voltage", "battery_charge", "input_voltage", "input_frequency", "ups_temperature")
COMMON = ("kind", "device_serial", "device_ts", "ups_status", *NUMERIC, "addon_version")


def rec(kind: str, reading: Reading | None = None, previous: str | None = None, ts=None) -> dict:
    reading = reading or Reading.from_vars(EPU1200_OL)
    o = Observation(kind=kind, reading=reading, mono=0.0, previous_status=previous)
    return build_record(o, device_serial=DEVICE_SERIAL, device_ts=ts or utc(2026, 10, 5, 13, 5, 0))


def test_reading_desde_la_epu1200():
    r = Reading.from_vars(EPU1200_OL)
    assert r == Reading(
        status="OL",
        battery_voltage=27.2,
        battery_charge=100.0,
        input_voltage=121.3,
        input_frequency=60.0,
        ups_temperature=30.0,
    )
    assert not r.on_battery


def test_reading_sin_status_es_none():
    assert Reading.from_vars({"battery.voltage": "27.2"}) is None
    assert Reading.from_vars({"ups.status": "   "}) is None


def test_reading_faltantes_y_basura_son_null():
    r = Reading.from_vars({"ups.status": "OB  DISCHRG", "battery.voltage": "n/a", "input.voltage": "nan"})
    assert r.status == "OB DISCHRG"  # solo se normalizan los espacios
    assert r.on_battery
    assert r.battery_voltage is None and r.input_voltage is None and r.ups_temperature is None


@pytest.mark.parametrize(("raw", "out"), [("27.20", 27.2), (" 0 ", 0.0), ("inf", None), ("", None), (None, None)])
def test_parse_float(raw, out):
    assert parse_float(raw) == out


def test_status_lleva_previous_status_en_orden():
    r = rec("status", previous="OB LB")
    assert list(r) == [
        "kind",
        "device_serial",
        "device_ts",
        "ups_status",
        "previous_status",
        *NUMERIC,
        "addon_version",
    ]
    assert r["previous_status"] == "OB LB"
    assert r["addon_version"] == ADDON_VERSION == "0.1.0-alpha"
    assert r["device_serial"] == DEVICE_SERIAL


def test_primer_status_lleva_previous_null():
    line = serialize(rec("status", previous=None))
    assert '"previous_status":null' in line


@pytest.mark.parametrize("kind", ["sample", "heartbeat"])
def test_sample_y_heartbeat_sin_previous_status(kind):
    r = rec(kind)
    assert tuple(r) == COMMON
    assert r["kind"] == kind


def test_no_hay_campos_fuera_del_contrato():
    # ups.load (siempre 0 en la EPU1200), ni serial/mfr/model de NUT.
    for kind in ("status", "sample", "heartbeat"):
        assert set(rec(kind, previous="OL")) <= set(COMMON) | {"previous_status"}


def test_tipos_del_contrato():
    r = rec("sample", reading=Reading(status="OB", battery_voltage=24.0))
    payload = json.loads(serialize(r))
    assert isinstance(payload["battery_voltage"], float)
    assert payload["battery_charge"] is None
    assert isinstance(payload["ups_status"], str)


@pytest.mark.parametrize(
    "ts",
    [utc(2026, 10, 5, 13, 5, 0, 123456), datetime(2026, 10, 5, 8, 5, 0, 123456, tzinfo=BOGOTA)],
    ids=["utc", "bogota"],
)
def test_device_ts_siempre_en_utc_con_ms(ts):
    assert rec("heartbeat", ts=ts)["device_ts"] == "2026-10-05T13:05:00.123+00:00"


def test_device_ts_naive_se_rechaza():
    with pytest.raises(ValueError):
        iso_ms_utc(datetime(2026, 10, 5, 13, 5))  # noqa: DTZ001


def test_device_ts_parseable_y_aware():
    s = rec("heartbeat", ts=datetime(2026, 10, 5, 13, 5, tzinfo=timezone(timedelta(hours=2))))["device_ts"]
    dt = datetime.fromisoformat(s)
    assert dt.utcoffset() == timedelta(0) and dt.hour == 11


def test_kind_desconocido():
    with pytest.raises(ValueError):
        rec("crossing")


def test_serialize_compacta_y_estable():
    line = serialize(rec("status", previous="OL"))
    assert "\n" not in line and ": " not in line and ", " not in line
    assert line == serialize(json.loads(line))
    assert line.startswith(
        '{"kind":"status","device_serial":"UPS-EXAMPLE-0001","device_ts":"2026-10-05T13:05:00.000+00:00"'
    )


def test_serialize_rechaza_nan():
    with pytest.raises(ValueError):
        serialize({"x": float("nan")})
