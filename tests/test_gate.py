import socket, time, unittest
import engine as E


class GateTests(unittest.TestCase):
    """Una PS5 con solo Payload Manager (8084) abierto y el resto de puertos mudos no es una PS5 apagada."""
    def setUp(self):
        self.orig = E._CONN_ORIG
        E._GATE.clear(); E._GATE_OK.clear(); E._GATE_PROBE.clear()
        E.GATE_HOST = lambda: "192.168.31.56"

        def fake(addr, timeout=None, *a, **kw):
            if addr[1] == 8084:
                s = socket.socket(); s.close(); return _Fake()
            raise socket.timeout("timed out")
        E._CONN_ORIG = fake

    def tearDown(self):
        E._CONN_ORIG = self.orig
        E.GATE_HOST = None

    def test_mute_port_does_not_block_pm(self):
        for port in (9021, 5905, 2121):
            with self.assertRaises(socket.timeout):
                E._gated_connection(("192.168.31.56", port))
        E._gated_connection(("192.168.31.56", 8084))          # antes: ConsoleDown al momento
        with self.assertRaises(E.ConsoleDown):                 # el mudo sí se contesta al momento
            E._gated_connection(("192.168.31.56", 9021))
        self.assertFalse(E.console_down())

    def test_all_mute_is_down(self):
        E._CONN_ORIG = lambda *a, **k: (_ for _ in ()).throw(socket.timeout("t"))
        with self.assertRaises(socket.timeout):
            E._gated_connection(("192.168.31.56", 8084))
        self.assertTrue(E.console_down())


class _Fake:
    def settimeout(self, t):
        pass
