"""Minimal Source-RCON client (what Minecraft speaks on rcon.port)."""
import itertools
import re
import socket
import struct
import threading

AUTH, AUTH_RESPONSE, COMMAND, RESPONSE = 3, 2, 2, 0
SENTINEL_TYPE = 100            # unknown type: the server answers it after the real reply
MAX_FRAGMENT = 4096            # Minecraft splits long replies into chunks of this many bytes
COLOR = re.compile("\u00a7.")


class RconError(Exception):
    pass


def encode(req_id, kind, body):
    payload = struct.pack("<ii", req_id, kind) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def decode(data):
    """Parse one packet from bytes. Returns (req_id, kind, body, rest) or None if incomplete."""
    if len(data) < 4:
        return None
    (length,) = struct.unpack("<i", data[:4])
    if len(data) < 4 + length:
        return None
    req_id, kind = struct.unpack("<ii", data[4:12])
    body = data[12:4 + length - 2].decode("utf-8", errors="replace")
    return req_id, kind, body, data[4 + length:]


class Rcon:
    """Thread-safe RCON connection that reconnects on demand."""

    def __init__(self, host, port, password, timeout=8):
        self.addr = (host, port)
        self.password = password
        self.timeout = timeout
        self.sock = None
        self.buf = b""
        self.ids = itertools.count(1)
        self.lock = threading.Lock()

    def _read_packet(self):
        while True:
            pkt = decode(self.buf)
            if pkt:
                req_id, kind, body, self.buf = pkt
                return req_id, kind, body
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RconError("server closed the RCON connection")
            self.buf += chunk

    def _connect(self):
        self.close()
        try:
            self.sock = socket.create_connection(self.addr, self.timeout)
        except OSError as e:
            raise RconError(f"cannot reach RCON at {self.addr[0]}:{self.addr[1]} ({e})") from e
        self.sock.settimeout(self.timeout)
        self.buf = b""
        auth_id = next(self.ids)
        self.sock.sendall(encode(auth_id, AUTH, self.password))
        while True:
            req_id, kind, _ = self._read_packet()
            if kind == AUTH_RESPONSE:
                if req_id == -1:
                    self.close()
                    raise RconError("RCON password rejected")
                return

    def command(self, cmd):
        """Run a console command; returns its reply with colour codes removed."""
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.sock is None:
                        self._connect()
                    # Minecraft reads one packet per socket read, so never pipeline packets.
                    cmd_id = next(self.ids)
                    self.sock.sendall(encode(cmd_id, COMMAND, cmd))
                    req_id, _, body = self._read_packet()
                    while req_id != cmd_id:
                        req_id, _, body = self._read_packet()
                    parts = [body]
                    if len(body.encode()) >= MAX_FRAGMENT:
                        # A full fragment means more may follow; the server answers the
                        # sentinel only after it has sent every fragment of the reply.
                        end_id = next(self.ids)
                        self.sock.sendall(encode(end_id, SENTINEL_TYPE, ""))
                        while True:
                            req_id, _, body = self._read_packet()
                            if req_id == end_id:
                                break
                            if req_id == cmd_id:
                                parts.append(body)
                    return COLOR.sub("", "".join(parts)).strip()
                except (OSError, RconError) as e:
                    self.close()
                    if attempt == 2 or (isinstance(e, RconError) and "password" in str(e)):
                        raise RconError(str(e)) from e

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
