# NUT UPS (Portería Virtual) — Home Assistant Add-on

Add-on de Home Assistant OS que corre en la Pi de cada sitio, lee el estado y la
batería de la UPS desde el **add-on NUT** (upsd) y lo reenvía al backend de
Portería Virtual (#51: batería de la UPS a la central con la hora real).

**Es un mensajero:** no evalúa umbrales ni arma alarmas. Decide **cuándo** hay
algo que reportar (cambio de estado significativo, muestras mientras dura un corte y la
recarga, latido con red estable), lo fecha con la hora real, lo escribe en la
auditoría local y, si hay backend configurado, lo reenvía con una cola
persistente. La evaluación de la batería (E/R hacia la central) la hace el backend.

```
 ┌──────────┐ USB ┌───────────────┐  TCP 3493  ┌──────────────────────────┐  POST JSON   ┌────────────┐
 │ UPS      │ ──▶ │ add-on NUT    │ ─────────▶ │ este add-on (Pi)         │ ───────────▶ │ pv-backend │
 │ EPU1200  │     │ upsd          │  LIST VAR  │ muestreo → registro      │  cola SQLite │ /eventos/  │
 └──────────┘     └───────────────┘            │ → auditoría JSON Lines   │              │ ups        │
                                               └──────────────────────────┘              └────────────┘
```

## Cómo funciona

1. Cada `poll_seconds` (5 s) manda `LIST VAR <ups_name>` al upsd del add-on NUT
   (`a0d7b954-nut:3493`, red interna del Supervisor; no pide login). Cliente con
   `socket` de la stdlib, conexión persistente.
2. Si upsd no responde, responde `ERR …` o la respuesta no respeta el protocolo:
   **solo log, sin registros**. Reconecta con backoff 5 / 10 / 20 / 40 / 60 s
   (tope 60) que vuelve a 5 con la primera lectura buena. Un WARNING al caer, otro
   cada 10 min mientras siga caído y un INFO al volver. El vigilante del backend
   detecta la falta de latidos.
3. **Máquina de muestreo** (como mucho un registro por lectura):

   | Situación | Registro | Cadencia |
   |---|---|---|
   | Cambian las banderas **significativas** de `ups.status` (`OL`, `OB`, `LB`, `OFF`, `FSD`; también la primera lectura tras arrancar) | `status` con `previous_status` = `ups_status` del último `status` emitido (`null` la primera vez) | en el momento |
   | En batería (`OB` en `ups.status`) | `sample` | cada 60 s |
   | Recarga: con red después de un episodio en batería | `sample` | cada 60 s, hasta `float_confirm_samples` muestras **seguidas** con `battery.voltage >= float_voltage`, o 12 h como máximo |
   | Con red estable | `heartbeat` | cada 15 min |

   Las cadencias se cuentan desde el último registro emitido. Las demás
   banderas (`CHRG`, `DISCHRG`, `TRIM`, `BOOST`, `RB`…) no generan `status` (ver
   [Operación](#operación-lo-medido-en-la-ups-de-la-oficina)) pero viajan siempre en el
   `ups_status` crudo de cada registro. Un cambio entre estados con red no corta
   la recarga; un nuevo corte la reinicia. Si el add-on **arranca** con red y `battery.voltage < float_voltage`
   (la Pi se apagó en el corte y volvió con la batería descargada), también entra
   en recarga.
4. **Hora real (D10).** Antes de fijar `device_ts` pregunta al Supervisor
   (`GET /host/info` → `dt_synchronized`). Si es `false` o la consulta falla, el
   registro queda **retenido en memoria** con el instante del reloj monotónico;
   cuando el reloj sincroniza, cada retenido recibe
   `device_ts = ahora − (mono_ahora − mono_lectura)`, en orden. `device_ts` sale
   siempre en UTC. Los retenidos no sobreviven a un reinicio del add-on (tope:
   20 000 en memoria, se descartan los más viejos).
5. Cada registro se serializa **una sola vez**: esa misma cadena va a la
   auditoría y a la cola, y el POST la reenvía siempre byte a byte igual.
6. Una línea INFO por hora resume lecturas, errores de upsd, registros por tipo,
   cambios de banderas no significativas ignorados (`banderas_ignoradas`),
   retenidos por la hora, pendientes de la cola y el modo del muestreo.

## Requisitos

- Add-on **NUT** (`a0d7b954_nut`) instalado y andando, con la UPS declarada
  (probado: NUT 2.8.5, add-on 0.18.1, UPS `epu1200`). `shutdown_host` apagado (si
  no, NUT apaga la Pi en `LB`).
- La UPS dada de alta en el backend con el mismo `device_serial` que se configura
  acá (NUT no expone `ups.serial` en la EPU1200: es un identificador elegido al
  dar de alta).

## Instalación en Home Assistant

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Agregar `https://github.com/nupsterd/nut-ups-addon`.
3. Instalar **NUT UPS (Portería Virtual)**. La imagen se construye en la Pi
   (no hay imagen prebuilt): tarda unos minutos la primera vez.
4. En **Configuration** completar `device_serial` (activar *Show unused optional
   configuration options* para ver `backend_url` / `backend_secret`). Guardar y
   verificar desde el add-on SSH, con el secreto enmascarado:
   `ha apps info <slug> --raw-json | jq '.data.options | .backend_secret="***"'`.
5. En **Info**: activar **Start on boot** y **Watchdog**, y arrancar.
6. En **Log** tiene que aparecer `Registro status solo si cambian las banderas FSD LB OB OFF OL (N4).`,
   `Conectado a upsd a0d7b954-nut:3493 (UPS 'epu1200')` y
   `Estado de la UPS: (arranque) → OL (…)`.

## Opciones

| Opción | Tipo (schema) | Default | Descripción |
|---|---|---|---|
| `device_serial` | `str` | — | Identificador de la UPS en el backend. Obligatoria. |
| `backend_url` | `str?` | vacío | **Endpoint COMPLETO** (`https://api.nupster.io/api/v1/eventos/ups`). No se concatena ningún path. Vacío = fan-out apagado (solo auditoría, sin cola). |
| `backend_secret` | `password?` | vacío | Token del header `X-PV-UPS-Token`. Nunca se imprime. |
| `backend_timeout_seconds` | `int(1,30)` | `5` | Timeout de cada POST. |
| `nut_host` | `str` | `a0d7b954-nut` | Host del upsd (hostname del add-on NUT en la red del Supervisor). |
| `nut_port` | `port` | `3493` | Puerto del upsd. |
| `ups_name` | `match(^[A-Za-z0-9_.-]+$)` | `epu1200` | Nombre de la UPS en NUT. |
| `poll_seconds` | `int(1,60)` | `5` | Cada cuánto se consulta upsd. |
| `float_voltage` | `float(1,100)` | `27.0` | Tensión de flotación que cierra la recarga (batería de 24 V nominales: 27,2 V en flotación). |
| `float_confirm_samples` | `int(1,60)` | `5` | Muestras seguidas (una por minuto) a `>= float_voltage` para cerrar la recarga. |
| `outbox_max_records` | `int(1000,1000000)` | `100000` | Tope de la cola; al superarlo se descarta el registro **más viejo**. |
| `outbox_max_age_days` | `int(1,90)` | `30` | Registros más viejos que esto se descartan de la cola (D11). |
| `audit_dir` | `str` | `/config` | Carpeta de la auditoría (`/config` = `/addon_configs/<slug>/`). |
| `audit_retention_days` | `int(1,365)` | `30` | Días de auditoría que se conservan. |
| `log_level` | `list(debug\|info\|warning)` | `info` | En `info` se loguea cada cambio de estado significativo; muestras, latidos y cambios de banderas ignorados van a `debug` (los registros, además, a la auditoría). |

## Auditoría local

`<audit_dir>/nut_ups_audit_YYYYMMDD.jsonl` (fecha local de la Pi), una línea JSON
por registro, flush por línea. Cada línea es **exactamente** el cuerpo que va (o
iría) en el POST. Los archivos con más de `audit_retention_days` días se borran al
arrancar y en cada cambio de día.

## Contrato del POST (pv-backend B1)

```
POST <backend_url>                      (https://api.nupster.io/api/v1/eventos/ups)
Content-Type: application/json
X-PV-UPS-Token: <backend_secret>        (se omite si backend_secret está vacío)

<un registro JSON por request>
```

```json
{
  "kind": "status",
  "device_serial": "UPS-EXAMPLE-0001",
  "device_ts": "2026-10-05T13:05:00.123+00:00",
  "ups_status": "OB DISCHRG",
  "previous_status": "OL",
  "battery_voltage": 25.1,
  "battery_charge": 92.0,
  "input_voltage": 0.0,
  "input_frequency": 60.0,
  "ups_temperature": 30.0,
  "addon_version": "0.1.0-alpha"
}
```

(En el cable va compacto, en una línea, sin espacios.)

| Campo | Tipo | Origen / nota |
|---|---|---|
| `kind` | `"status"` \| `"sample"` \| `"heartbeat"` | máquina de muestreo |
| `device_serial` | str | opción `device_serial` |
| `device_ts` | ISO-8601 en UTC con ms | instante de la lectura, con el reloj sincronizado (D10) |
| `ups_status` | str | `ups.status` crudo (solo se colapsan espacios repetidos) |
| `previous_status` | str \| null | **solo en `kind=status`**: `ups_status` crudo del último `status` emitido (no de la última lectura); `null` en el primero tras arrancar |
| `battery_voltage` | float \| null | `battery.voltage` |
| `battery_charge` | float \| null | `battery.charge` (estimada por el driver: informativa) |
| `input_voltage` | float \| null | `input.voltage` |
| `input_frequency` | float \| null | `input.frequency` |
| `ups_temperature` | float \| null | `ups.temperature` |
| `addon_version` | str | constante |

`null` si la variable falta o no es un número finito. `ups.load` **no** se envía
(en la EPU1200 vale siempre 0). Dos registros nunca comparten `device_ts`: salen
de lecturas distintas, separadas al menos `poll_seconds`.

### Respuestas del backend y comportamiento de la cola

| Respuesta | Acción del add-on |
|---|---|
| 2xx con `{"status": "received"}` o `{"status": "duplicate"}` | Borra el registro de la cola. |
| 2xx con `{"status": "device_unknown"}` | **ERROR DE CONFIGURACIÓN**: pausa el envío, no descarta nada, ERROR y reintento cada 10 min. |
| 2xx con otro `status` o sin JSON | ERROR DE CONFIGURACIÓN (igual que arriba). |
| 401, 403 | ERROR DE CONFIGURACIÓN (token). |
| 404, 405 | ERROR DE CONFIGURACIÓN (URL: tiene que ser el endpoint completo). |
| 400, 422 (y otros 4xx no listados) | Mueve ESE registro a la tabla `failed` (con código y motivo) y sigue con el siguiente. |
| 408, 429, 5xx, timeout, error de red | Reintenta el MISMO registro con backoff 5 s → 10 → 20 → 40 → 80 → 160 → 300 s (tope 5 min), conservando el orden. |

Cola SQLite en `/data/outbox.sqlite` (WAL, `synchronous=FULL`), FIFO, un solo hilo
de envío; sobrevive a reinicios del add-on y de la Pi. Mismo contrato y misma
clasificación que `dahua-ivs-addon`.

## Operación: lo medido en la UPS de la oficina

Corte real del 6-oct-2026 (EPU1200 de la oficina, 2 h 48 min en batería):

- **Parpadeo de `CHRG` / `DISCHRG`.** La UPS los prende y apaga durante UNA
  lectura (5 s), tanto en batería como con red: p. ej. `OB` → `OB CHRG` → `OB`
  a las 11:50:53 / 11:50:58. Hubo 6 parpadeos así; con un `status` por cada
  cambio de `ups.status` habrían sido 12 registros `status` sin información.
  Desde N4 solo disparan `status` los cambios del conjunto `{OL, OB, LB, OFF,
  FSD}` (tokens separados por espacio, nunca substring). `CHRG`, `DISCHRG`,
  `TRIM`, `BOOST`, `RB` y cualquier otra bandera quedan en el `ups_status` crudo
  de la muestra o el latido siguiente, y el backend puede compararlas.
- Cada cambio ignorado se loguea en `debug` (`cambio de banderas no
  significativas: OB → OB CHRG`) y se cuenta en `banderas_ignoradas` del resumen
  horario. Para verlos en vivo: `log_level: debug`.
- **Recarga.** El cargador sube la tensión despacio: las 5 muestras seguidas
  `>= 27.0 V` (`float_voltage` x `float_confirm_samples`) solo se dan con la
  batería llena, así que la recarga se cierra cuando corresponde (N2: sin cambios).

## Límites conocidos

- Si la Pi está apagada, no hay lecturas: la UPS no guarda historial accesible por NUT.
- Los registros retenidos por hora no sincronizada viven en memoria: un reinicio
  del add-on antes de que sincronice los pierde.
- La recarga termina por tensión de flotación (ver arriba); el criterio de
  "batería restablecida" lo decide el backend con estas muestras.
- `RB` (cambiar batería), `OVER`, `BYPASS`, `ALARM`, etc. no generan `status`:
  llegan al backend en el `ups_status` crudo de la siguiente muestra o latido
  (con red estable, hasta 15 min después).
- `battery.charge` es una estimación del driver a partir de la tensión.

## Troubleshooting

| Síntoma en el log | Causa probable | Qué hacer |
|---|---|---|
| `upsd a0d7b954-nut:3493 no responde …` | Add-on NUT detenido o el driver no ve la UPS | Revisar el log del add-on NUT. Desde el add-on SSH: `printf 'LIST VAR epu1200\n' \| nc -w 3 a0d7b954-nut 3493`. |
| `… ERR UNKNOWN-UPS (¿ups_name correcto?)` | `ups_name` no coincide con el de NUT | Corregir `ups_name` (`printf 'LIST UPS\n' \| nc -w 3 a0d7b954-nut 3493`). |
| `… ERR DATA-STALE` / `DRIVER-NOT-CONNECTED` | El driver perdió la UPS (USB) | Revisar el cable USB y reiniciar el add-on NUT. |
| `Hora de la Pi NO confirmada (…)` | NTP todavía no sincronizó tras el arranque | Esperar; si persiste, revisar la red/NTP del host. Con `sin SUPERVISOR_TOKEN` falta `hassio_api`. |
| `ERROR DE CONFIGURACIÓN … device_unknown` | La UPS no está dada de alta con ese `device_serial` | Darla de alta en el backend. Nada se pierde: la cola espera. |
| `ERROR DE CONFIGURACIÓN … (HTTP 404)` | `backend_url` es la URL base | Poner el endpoint completo (`…/api/v1/eventos/ups`). |
| `ERROR DE CONFIGURACIÓN … (HTTP 401)` | `backend_secret` incorrecto | Corregirlo y reiniciar. |
| `Recarga: la batería no llegó a flotación … en 12 h` | Batería degradada o cargador con otra tensión de flotación | Revisar la batería y `float_voltage`. |

## Desarrollo / tests

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements-dev.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python -m pytest -q
```

CI (GitHub Actions): ruff + pytest en cada PR y en `main`. Módulos (`nut_ups/`):
`config`, `nut` (cliente), `records` (contrato), `sampler` (muestreo), `clock`
(D10), `runner` (bucle), `outbox`, `audit`, `main`. Los tests del cliente usan un
upsd falso en loopback.
