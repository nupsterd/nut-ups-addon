"""Add-on de Home Assistant: estado y batería de una UPS vía NUT (upsd).

Es un MENSAJERO: consulta ``LIST VAR <ups_name>`` al upsd del add-on NUT cada
``poll_seconds``, decide cuándo hay algo que reportar (cambio de ``ups.status``,
muestra periódica en batería o en la recarga, latido en red estable), lo audita
en disco y, si hay backend configurado, lo encola en una cola SQLite persistente
que lo reenvía por POST. No evalúa umbrales ni arma alarmas: eso lo hace el
backend (#51).
"""

ADDON_VERSION = "0.1.0-alpha"
