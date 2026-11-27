import unittest
import nanodns as N

DEF = """[general]
log=/data/nanodns/nanodns.log
debug=0
quiet=0
bind=127.0.0.1
bind6=::1

[upstream]
server=1.1.1.1
server=8.8.8.8
server=77.77.88.88
timeout_ms=1500

[overrides]
*.playstation.com=0.0.0.0
# *.example.com=192.168.0.10

[exceptions]
feature.api.playstation.com
# auth.api.playstation.net
"""


class NanoTests(unittest.TestCase):
    def test_parse(self):
        p = N.parse(DEF)
        self.assertEqual(p["servers"], ["1.1.1.1", "8.8.8.8", "77.77.88.88"])
        self.assertTrue(p["old_yandex"]); self.assertEqual(p["timeout_ms"], 1500)
        self.assertEqual((p["overrides"], p["exceptions"]), (1, 1))

    def test_fix_yandex_keeps_rest(self):
        t = N.fix_yandex(DEF)
        self.assertIn("server=77.88.8.8", t); self.assertNotIn("77.77.88.88", t)
        self.assertIn("# *.example.com=192.168.0.10", t); self.assertIn("timeout_ms=1500", t)
        self.assertEqual(t.count("\n"), DEF.count("\n"))

    def test_set_general(self):
        t = N.set_general(DEF, "quiet", "1")
        self.assertIn("quiet=1", t); self.assertNotIn("quiet=0", t)
        t2 = N.set_general(DEF.replace("bind6=::1\n", ""), "bind6", "off")
        self.assertIn("bind6=off\n\n[upstream]", t2)

    def test_crlf_kept(self):
        t = N.set_general(DEF.replace("\n", "\r\n"), "quiet", "1")
        self.assertNotIn("\n", t.replace("\r\n", ""))

    def test_check(self):
        self.assertTrue(N.check(DEF))
        self.assertEqual(N.check(N.fix_yandex(DEF)), [])
        self.assertTrue(N.check(N.set_servers(DEF, [])))
