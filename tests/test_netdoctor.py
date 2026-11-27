import unittest
import netdoctor as N


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.orig = (N.adapter_for, N.local_ip_for)
        N.local_ip_for = lambda h: "192.168.31.138"

    def tearDown(self):
        N.adapter_for, N.local_ip_for = self.orig

    def ad(self, **kw):
        d = {"name": "", "desc": "", "wifi": None, "mbps": None, "signal": None, "band": "", "radio": ""}
        d.update(kw)
        N.adapter_for = lambda ip: d

    def test_vpn_first(self):
        self.ad(name="NordLynx", desc="NordLynx Tunnel", wifi=False)
        f = N.findings("192.168.31.56", 3, [4, 5])
        self.assertEqual(f[0]["id"], "vpn")

    def test_wifi_weak_and_bufferbloat(self):
        self.ad(name="Wi-Fi", desc="Intel Wi-Fi 6", wifi=True, mbps=866, signal=40, band="5 GHz", radio="Wi-Fi 5")
        f = {x["id"]: x for x in N.findings("192.168.31.56", 4, [180, 200, 220])}
        self.assertEqual(f["link"]["level"], "warn"); self.assertIn("señal", f["link"]["text"])
        self.assertEqual(f["load"]["level"], "warn")

    def test_cable_100(self):
        self.ad(name="Ethernet", desc="Realtek PCIe GbE", wifi=False, mbps=100)
        f = {x["id"]: x for x in N.findings("192.168.31.56", 2, [3])}
        self.assertEqual(f["link"]["level"], "warn")

    def test_windows_parse(self):
        N.adapter_for = self.orig[0]
        real_run, real_os = N._run, N.os.name
        outs = {"powershell": '{"Name":"Wi-Fi","InterfaceDescription":"Intel(R) Wi-Fi 6 AX201","LinkSpeed":"866.7 Mbps","MediaType":"Native 802.11","PhysicalMediaType":"Native 802.11"}',
                "netsh": "    Tipo de radio          : 802.11ac\n    Banda                  : 5 GHz\n    Canal                  : 36\n    Velocidad de recepción (Mbps)  : 866.7\n    Velocidad de transmisión (Mbps) : 780\n    Señal                  : 92%\n"}
        N._run = lambda args, timeout=8: outs[args[0]]
        N.os.name = "nt"
        try:
            a = N.adapter_for("192.168.31.138")
        finally:
            N._run, N.os.name = real_run, real_os
        self.assertTrue(a["wifi"]); self.assertEqual(a["signal"], 92); self.assertEqual(a["band"], "5 GHz")
        self.assertEqual(a["radio"], "Wi-Fi 5"); self.assertAlmostEqual(a["mbps"], 866.7)
