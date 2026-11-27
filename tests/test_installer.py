import os, tempfile, unittest
import features as F


class InstallerTests(unittest.TestCase):
    """ftpsrv 0.22 publica ftpsrv-ps5.elf (el servidor) y ftpsrv-ps5-install.elf (solo un icono): hay que coger el primero."""
    def test_pick_server_not_installer(self):
        p = next(x for x in F.PAYLOADS if x["id"] == "ftpsrv")
        assets = [{"name": "ftpsrv-ps5-install.elf"}, {"name": "ftpsrv-ps5.elf"}, {"name": "ftpsrv-ps4.elf"}]
        self.assertEqual(F.pick_asset(assets, p["pick"])["name"], "ftpsrv-ps5.elf")
        self.assertEqual(F._pick_elf(["ftpsrv-ps5-install.elf", "ftpsrv-ps5.elf"]), "ftpsrv-ps5.elf")

    def test_cached_installer_ignored(self):
        d = tempfile.mkdtemp(); os.makedirs(os.path.join(d, "payloads"))
        open(os.path.join(d, "payloads", "ftpsrv-v0.22.elf"), "wb").write(b"\x7fELF....sceAppInstUtilAppInstallAll")
        real = F.github_release
        F.github_release = lambda repo: (_ for _ in ()).throw(OSError("sin red"))
        try:
            with self.assertRaises(Exception):          # solo estaba el instalador: no se usa
                F.fetch_payload("ftpsrv", d)
            open(os.path.join(d, "payloads", "ftpsrv-v0.21.1.elf"), "wb").write(b"\x7fELF servidor")
            self.assertTrue(F.fetch_payload("ftpsrv", d)[0].endswith("ftpsrv-v0.21.1.elf"))
        finally:
            F.github_release = real


class IncompleteTests(unittest.TestCase):
    def test_pkg_cut_short_and_partial(self):
        import struct, time
        d = tempfile.mkdtemp()
        p = os.path.join(d, "juego.pkg")
        head = bytearray(0x1000); head[:4] = b"\x7fCNT"; head[0x430:0x438] = struct.pack(">Q", 50000)
        open(p, "wb").write(head)
        old = time.time() - 3600; os.utime(p, (old, old))
        self.assertIn("cortado", F.local_incomplete(p))
        open(p, "ab").write(b"\0" * (50000 - 0x1000)); os.utime(p, (old, old))
        self.assertEqual(F.local_incomplete(p), "")
        q = os.path.join(d, "juego.rar.crdownload"); open(q, "wb").write(b"x")
        self.assertIn("sin terminar", F.local_incomplete(q))


class BfpilotTests(unittest.TestCase):
    """El BFpilot oficial lleva dentro la función de instalar su lanzador y es el programa: no se rechaza."""
    def test_bfpilot_with_installer_marker_kept(self):
        d = tempfile.mkdtemp(); os.makedirs(os.path.join(d, "payloads"))
        open(os.path.join(d, "payloads", "bfpilot-v0.4.4.elf"), "wb").write(b"\x7fELF....sceAppInstUtilAppInstallAll")
        real = F.github_release
        F.github_release = lambda repo: (_ for _ in ()).throw(OSError("sin red"))
        try:
            self.assertTrue(F.fetch_payload("bfpilot", d)[0].endswith("bfpilot-v0.4.4.elf"))
        finally:
            F.github_release = real
