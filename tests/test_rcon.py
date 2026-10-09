"""RCON client against a fake server that behaves like Minecraft's."""
import socket
import threading

import pytest

from mcctl.rcon import AUTH, Rcon, RconError, decode, encode

PASSWORD = "secret-password-123"


class FakeMinecraftRcon(threading.Thread):
    """Mimics Minecraft: reads ONE packet per recv (extra bytes are dropped and the client is
    disconnected), splits replies into 4096-byte fragments, answers unknown types."""

    def __init__(self, replies):
        super().__init__(daemon=True)
        self.replies = replies
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.commands = []

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        authed = False
        with conn:
            while True:
                data = conn.recv(1460)
                if not data:
                    return
                pkt = decode(data)
                if pkt is None or pkt[3]:          # incomplete or pipelined packets: Minecraft bails
                    return
                req_id, kind, body, _ = pkt
                if kind == AUTH:
                    authed = body == PASSWORD
                    conn.sendall(encode(req_id if authed else -1, 2, ""))
                elif not authed:
                    return
                elif kind == 2:
                    self.commands.append(body)
                    reply = self.replies.get(body, f"ran {body}")
                    for i in range(0, max(len(reply), 1), 4096):
                        conn.sendall(encode(req_id, 0, reply[i:i + 4096]))
                else:
                    conn.sendall(encode(req_id, 0, f"Unknown request {kind:x}"))

    def close(self):
        self.sock.close()


@pytest.fixture
def fake():
    srv = FakeMinecraftRcon({"list": "§aThere are 0 of a max of 10 players online:", "help": "x" * 9000})
    srv.start()
    yield srv
    srv.close()


def test_codec_roundtrip():
    pkt = encode(7, 2, "say hi") + b"rest"
    assert decode(pkt) == (7, 2, "say hi", b"rest")
    assert decode(pkt[:5]) is None


def test_command_and_colour_stripping(fake):
    r = Rcon("127.0.0.1", fake.port, PASSWORD)
    assert r.command("list") == "There are 0 of a max of 10 players online:"
    assert r.command("seed") == "ran seed"


def test_long_reply_is_reassembled(fake):
    r = Rcon("127.0.0.1", fake.port, PASSWORD)
    assert r.command("help") == "x" * 9000


def test_bad_password(fake):
    with pytest.raises(RconError, match="password"):
        Rcon("127.0.0.1", fake.port, "wrong").command("list")


def test_unreachable():
    with pytest.raises(RconError):
        Rcon("127.0.0.1", 1, PASSWORD, timeout=0.5).command("list")


def test_threads_share_one_client(fake):
    r = Rcon("127.0.0.1", fake.port, PASSWORD)
    results = []
    threads = [threading.Thread(target=lambda i=i: results.append(r.command(f"echo {i}"))) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == sorted(f"ran echo {i}" for i in range(10))
