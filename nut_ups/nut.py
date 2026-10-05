"""Cliente mínimo del protocolo de red de NUT (upsd, TCP 3493), solo con la stdlib.

Usa una sola consulta, ``LIST VAR <ups>``, que en el add-on NUT de HA no pide
login. Respuesta esperada (una línea por variable, valores entre comillas con
``\\"`` y ``\\\\`` escapados)::

    BEGIN LIST VAR epu1200
    VAR epu1200 battery.voltage "27.20"
    VAR epu1200 ups.status "OL"
    END LIST VAR epu1200

o una línea ``ERR <CÓDIGO>`` (``UNKNOWN-UPS``, ``DATA-STALE``,
``DRIVER-NOT-CONNECTED``...). La conexión queda abierta entre consultas; ante
cualquier error se cierra y la próxima consulta reconecta.
"""

from __future__ import annotations

import socket
from typing import BinaryIO

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
MAX_LINE = 4096
MAX_VARS = 1000


class NutError(Exception):
    """upsd respondió ``ERR <código>`` o algo que no respeta el protocolo."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}{': ' + detail if detail else ''}")
        self.code = code


def split_words(line: str) -> list[str]:
    """Separa una línea de NUT en palabras; las comillas agrupan y ``\\`` escapa."""
    words: list[str] = []
    cur: list[str] = []
    in_quotes = False
    has_word = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            has_word = True
            i += 2
            continue
        if ch == '"':
            in_quotes = not in_quotes
            has_word = True
        elif ch == " " and not in_quotes:
            if has_word:
                words.append("".join(cur))
                cur, has_word = [], False
        else:
            cur.append(ch)
            has_word = True
        i += 1
    if in_quotes:
        raise NutError("PROTOCOL", f"comillas sin cerrar: {line[:200]!r}")
    if has_word:
        words.append("".join(cur))
    return words


class NutClient:
    def __init__(
        self,
        host: str,
        port: int,
        connect_timeout: float = CONNECT_TIMEOUT,
        read_timeout: float = READ_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._sock: socket.socket | None = None
        self._rfile: BinaryIO | None = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        self.close()
        sock = socket.create_connection((self.host, self.port), timeout=self._connect_timeout)
        sock.settimeout(self._read_timeout)
        self._sock = sock
        self._rfile = sock.makefile("rb")

    def close(self) -> None:
        if self._rfile is not None:
            try:
                self._rfile.close()
            except OSError:
                pass
            self._rfile = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _readline(self) -> str:
        assert self._rfile is not None
        raw = self._rfile.readline(MAX_LINE + 1)
        if not raw:
            raise ConnectionError("upsd cerró la conexión")
        if len(raw) > MAX_LINE:
            raise NutError("PROTOCOL", "línea demasiado larga")
        return raw.decode("utf-8", errors="replace").rstrip("\r\n")

    def list_var(self, ups: str) -> dict[str, str]:
        """``LIST VAR <ups>`` → ``{variable: valor}``. Conecta si hace falta.

        Ante ``OSError`` o ``NutError`` cierra la conexión y relanza.
        """
        try:
            if self._sock is None:
                self.connect()
            assert self._sock is not None
            self._sock.sendall(f"LIST VAR {ups}\n".encode("ascii"))
            return self._read_list(ups)
        except (OSError, NutError):
            self.close()
            raise

    def _read_list(self, ups: str) -> dict[str, str]:
        first = self._readline()
        if first.startswith("ERR "):
            words = split_words(first)
            raise NutError(words[1] if len(words) > 1 else "UNKNOWN", " ".join(words[2:]))
        if split_words(first) != ["BEGIN", "LIST", "VAR", ups]:
            raise NutError("PROTOCOL", f"inicio inesperado: {first[:200]!r}")
        out: dict[str, str] = {}
        while True:
            line = self._readline()
            words = split_words(line)
            if words == ["END", "LIST", "VAR", ups]:
                return out
            if line.startswith("ERR "):
                raise NutError(words[1] if len(words) > 1 else "UNKNOWN")
            if len(words) != 4 or words[0] != "VAR" or words[1] != ups:
                raise NutError("PROTOCOL", f"línea inesperada: {line[:200]!r}")
            if len(out) >= MAX_VARS:
                raise NutError("PROTOCOL", f"más de {MAX_VARS} variables")
            out[words[2]] = words[3]
