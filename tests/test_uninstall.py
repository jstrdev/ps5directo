"""Desinstalar desde la Biblioteca: zftpd (1.7.0+) primero, el ayudante de PS5Upload si deja entrar desde la red."""
import json, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import features as F

TOK = "ab" * 32


class FakeZ(BaseHTTPRequestHandler):
    new = True
    calls = []

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path == "/":
            return self._send(200, '<meta name="csrf-token" content="c0ffee">', "text/html")
        if self.path == "/api/status":
            return self._send(200, "{}")
        if self.path.startswith("/api/admin/games/uninstall"):
            return self._send(400, '{"error":"Missing query string"}') if FakeZ.new else self._send(404, "{}")
        self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n)
        if self.path.startswith("/api/admin/games/uninstall?id=") and FakeZ.new:
            FakeZ.calls.append((self.path, self.headers.get("X-CSRF-Token")))
            return self._send(200, '{"ok":true,"message":"Uninstalled","id":"PPSA01234"}')
        self._send(404, "{}")


class FakeU(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        b = body.encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/token":
            return self._send(200, json.dumps({"token": TOK, "version": "12.6.37"}))
        self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = json.loads(self.rfile.read(n) or b"{}")
        if self.headers.get("x-ps5u-token") != TOK:
            return self._send(401, '{"error":"token"}')
        FakeU.calls.append((self.path, body, self.headers.get("Host")))
        if body.get("title_id") == "PPSA00002":
            return self._send(409, '{"error":"game_running"}')
        self._send(200, '{"sony_uninstall_rc":0,"still_present":false}')


def serve(h):
    s = ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


class ZftpdTests(unittest.TestCase):
    def setUp(self):
        self.s = serve(FakeZ); F.Zftpd.PORT = self.s.server_port; FakeZ.calls.clear()

    def tearDown(self):
        self.s.shutdown(); F.Zftpd.PORT = 8888; FakeZ.new = True

    def test_probe_and_uninstall(self):
        z = F.Zftpd("127.0.0.1")
        self.assertTrue(z.has_uninstall())
        self.assertTrue(z.uninstall("PPSA01234")["ok"])
        self.assertEqual(FakeZ.calls, [("/api/admin/games/uninstall?id=PPSA01234", "c0ffee")])

    def test_old_version(self):
        FakeZ.new = False
        self.assertFalse(F.Zftpd("127.0.0.1").has_uninstall())


class Ps5uTests(unittest.TestCase):
    def setUp(self):
        self.s = serve(FakeU); F.Ps5uHelper.PORT = self.s.server_port; FakeU.calls.clear()

    def tearDown(self):
        self.s.shutdown(); F.Ps5uHelper.PORT = 9180

    def test_token_host_and_uninstall(self):
        u = F.Ps5uHelper("127.0.0.1")
        self.assertTrue(u.alive())
        self.assertFalse(u.uninstall("PPSA01234")["still_present"])
        path, body, host = FakeU.calls[0]
        self.assertEqual((path, body), ("/api/apps/uninstall", {"title_id": "PPSA01234"}))
        self.assertEqual(host, "127.0.0.1:%d" % self.s.server_port)   # su comprobación de Host pide ip:puerto

    def test_refusal_code(self):
        with self.assertRaises(F.Ps5uRefused) as c:
            F.Ps5uHelper("127.0.0.1").uninstall("PPSA00002")
        self.assertEqual((c.exception.code, c.exception.http), ("game_running", 409))

    def test_not_reachable(self):
        F.Ps5uHelper.PORT = 1                     # nada escucha: sin LAN activada pasa esto
        self.assertFalse(F.Ps5uHelper("127.0.0.1").alive())


if __name__ == "__main__":
    unittest.main()
