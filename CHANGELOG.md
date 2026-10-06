# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versionado siguiendo [SemVer](https://semver.org/lang/es/).

## [0.1.0-alpha] - 2026-10-06

Primera versión (B6.4, #51): lee la UPS desde el add-on NUT y reporta estado y
batería al backend (`/eventos/ups`) con la hora real. Probada en hardware con un
corte real en la oficina (6-oct, 2 h 48 min en batería); de esa prueba sale N4.

### Added
- Cliente NUT con `socket` de la stdlib: `LIST VAR <ups_name>` cada `poll_seconds`
  contra el upsd del add-on NUT, conexión persistente y reconexión con backoff
  5/10/20/40/60 s. Si upsd no responde: solo log, sin registros.
- Máquina de muestreo: `status` cuando cambian las banderas significativas de
  `ups.status` (`OL`, `OB`, `LB`, `OFF`, `FSD`; con `previous_status` = el
  `ups_status` del último `status` emitido); `sample` cada 60 s en batería y,
  después de un episodio en batería, también con red hasta la flotación
  (`float_voltage` x `float_confirm_samples` muestras seguidas, máx. 12 h);
  `heartbeat` cada 15 min con red estable. Si arranca con red y la batería debajo
  de `float_voltage`, también entra en recarga (N3).
- `device_ts` en UTC solo con el reloj de la Pi sincronizado (`dt_synchronized`
  del Supervisor, D10): mientras no lo esté, los registros se retienen en memoria
  y se fechan con el reloj monotónico al sincronizar.
- Auditoría JSON Lines diaria con retención, cola SQLite persistente
  (`X-PV-UPS-Token`, retención 30 días, D11) y clasificación de respuestas,
  reusadas de `dahua-ivs-addon` 0.1.1-alpha.
- N4: `CHRG`, `DISCHRG`, `TRIM`, `BOOST`, `RB` y demás banderas no disparan
  `status` (en la EPU1200 `CHRG`/`DISCHRG` parpadean una lectura de 5 s, en `OB`
  y en `OL`); siguen en el `ups_status` crudo de todos los registros. Los cambios
  ignorados van a `debug` y al resumen horario (`banderas_ignoradas`).
- Resumen INFO por hora (lecturas, errores de upsd, registros por tipo, cola,
  modo del muestreo).
- CI con GitHub Actions (ruff + pytest).
