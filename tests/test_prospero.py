"""prosperostore.net: solo se usa con la firma buena y cada archivo con su huella; se junta sin repetir."""
import hashlib, json, os, shutil, subprocess, tempfile, unittest

os.environ.setdefault("HOME", tempfile.mkdtemp())
import app as A

D = "/home/claude/e2e/pro"


@unittest.skipUnless(os.path.isfile(D + "/api/v1/manifest.sig") and shutil.which("openssl"), "sin catálogo de prueba")
class ProsperoTests(unittest.TestCase):
    def setUp(self):
        self.keys = A.PRO_KEYS
        A.PRO_KEYS = [bytes.fromhex(open(D + "/pub.hex").read().strip())]
        self.tmp = tempfile.mkdtemp()
        self.p = A.Prospero(self.tmp)
        self.files = {}
        self.p._get = lambda path, limit=0: self.files.get(path) or open(D + "/api/v1/" + path, "rb").read()

    def tearDown(self):
        A.PRO_KEYS = self.keys

    def test_ok_and_merge(self):
        apps = self.p.fetch()
        self.assertGreater(len(apps), 200)
        items = [{"id": "PPSA99264", "name": "X", "repo": "lucasliet/BattleShip-remix", "soon": False, "when": ""},
                 {"id": "PPSA99264", "name": "Otro", "repo": "", "soon": False, "when": ""}]
        out = A.pro_merge([dict(x) for x in items], apps)
        self.assertTrue(out[0].get("pro") and out[0]["pro"]["sha256"])
        names = " ".join(x["name"].lower() for x in out)
        self.assertNotIn("stremio", names); self.assertNotIn("nuvio", names)
        self.assertTrue(all(x.get("repo") for x in out[2:]))

    def test_bad_signature(self):
        self.files["manifest.sig"] = b"\0" * 64
        with self.assertRaises(Exception):
            self.p.fetch()

    def test_tampered_index(self):
        raw = open(D + "/api/v1/index.json", "rb").read()
        self.files["index.json"] = raw.replace(b"BattleShip", b"BattleShop")
        with self.assertRaises(Exception):
            self.p.fetch()

    def test_tampered_app_dropped(self):
        raw = open(D + "/api/v1/apps/PPSA99264.json", "rb").read()
        self.files["apps/PPSA99264.json"] = raw.replace(b"lucasliet", b"malo")
        self.assertNotIn("PPSA99264", self.p.fetch())

    def test_older_sequence_refused(self):
        self.p.fetch()
        st = json.load(open(self.p.state_path)); st["seq"] = 999
        json.dump(st, open(self.p.state_path, "w")); self.p.apps = {}
        with self.assertRaises(Exception):
            self.p.fetch()

    def test_update_by_content_version(self):
        A.upd_items = lambda: {"PPSA99264": {"have": "01.000.001"}}
        x = A.recomp_decorate([{"id": "PPSA99264", "pro": {"cv": "01.000.050"}}])[0]
        self.assertTrue(x["upd"]); self.assertEqual(x["have"], "01.000.001")
        x = A.recomp_decorate([{"id": "PPSA99264", "pro": {"cv": ""}}])[0]
        self.assertFalse(x["upd"])
