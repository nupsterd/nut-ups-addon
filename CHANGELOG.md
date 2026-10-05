# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versionado siguiendo [SemVer](https://semver.org/lang/es/).

## [0.1.0-alpha] - sin publicar

Primera versión (B6.4 / A1, #51). Pendiente: verificación en hardware (corte real
de red OB → LB → OL en la Pi de la oficina) antes del tag (A2).

### Added
- Cliente NUT con `socket` de la stdlib: `LIST VAR <ups_name>` cada `poll_seconds`
  contra el upsd del add-on NUT, conexión persistente y reconexión con backoff
  5/10/20/40/60 s. Si upsd no responde: solo log, sin registros.
- Máquina de muestreo: `status` en cada cambio de `ups.status` (con
  `previous_status`); `sample` cada 60 s en batería y, después de un episodio en
  batería, también con red hasta la flotación (`float_voltage` x
  `float_confirm_samples` muestras seguidas, máx. 12 h); `heartbeat` cada 15 min
  con red estable.
- `device_ts` en UTC solo con el reloj de la Pi sincronizado (`dt_synchronized`
  del Supervisor, D10): mientras no lo esté, los registros se retienen en memoria
  y se fechan con el reloj monotónico al sincronizar.
- Auditoría JSON Lines diaria con retención, cola SQLite persistente
  (`X-PV-UPS-Token`, retención 30 días, D11) y clasificación de respuestas,
  reusadas de `dahua-ivs-addon` 0.1.1-alpha.
- CI con GitHub Actions (ruff + pytest).
