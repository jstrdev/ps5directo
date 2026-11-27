import time
"""Pruebas de pldsafe con un FTP simulado: cortes de conexion, archivos truncados, rename que falla, hashes
distintos y cierres inesperados a mitad de operacion. Se ejecutan con:  python -m unittest discover tests"""

import ftplib
import os
import shutil
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pldsafe as P  # noqa: E402


def make_elf(payload=b"", entry_off=0):
    """ELF x86-64 minimo con un PT_LOAD que cubre todo el archivo."""
    body = payload or os.urandom(3000)
    ph_off = 64
    total = 64 + 56 + len(body)
    vaddr = 0x400000
    eh = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    eh += struct.pack("<HHIQQQIHHHHHH", 2, 0x3E, 1, vaddr + 120 + entry_off, ph_off, 0, 0, 64, 56, 1, 64, 0, 0)
    ph = struct.pack("<IIQQQQQQ", 1, 5, 0, vaddr, vaddr, total, total, 0x1000)
    return eh + ph + body


class Crash(BaseException):
    """Simula que el programa se cierra de golpe (no es Exception: nada lo captura)."""


class FakeFS:
    def __init__(self):
        self.files = {}
        self.dirs = {"/", "/data"}


class FakeFTP:
    """Lo justo de ftplib.FTP. Fallos que se pueden activar:
    drop_after_stor: corta la conexion tras N STOR; truncate_stor: guarda solo N bytes; corrupt_stor: cambia un byte;
    rename_overwrite=False: no deja renombrar encima de algo que existe; rename_fail: falla todo rename;
    exdev: rename entre volumenes distintos falla; drop_on_rename: corta en el N-esimo rename (tras hacerlo)."""

    def __init__(self, fs, **faults):
        self.fs = fs
        self.f = faults
        self.closed = False
        self.counters = fs.__dict__.setdefault("counters", {"stor": 0, "rename": 0, "retr": 0})

    def _alive(self):
        if self.closed:
            raise EOFError("conexión cerrada")

    def _vol(self, p):
        return p.split("/")[1] + ("/" + p.split("/")[2] if p.startswith("/mnt/") else "")

    def storbinary(self, cmd, fp, blocksize=8192):
        self._alive()
        path = cmd[5:]
        data = fp.read()
        self.counters["stor"] += 1
        parent = path.rsplit("/", 1)[0] or "/"
        if parent not in self.fs.dirs:
            raise ftplib.error_perm("550 no existe la carpeta")
        if self.f.get("truncate_stor") is not None:
            data = data[:self.f["truncate_stor"]]
        if self.f.get("corrupt_stor"):
            data = data[:100] + bytes([data[100] ^ 0xFF]) + data[101:]
        self.fs.files[path] = data
        if self.f.get("drop_after_stor") and self.counters["stor"] >= self.f["drop_after_stor"]:
            self.closed = True
            raise EOFError("conexión perdida")

    def retrbinary(self, cmd, cb, blocksize=8192, rest=None):
        self._alive()
        path = cmd[5:]
        self.counters["retr"] += 1
        if self.f.get("drop_on_retr") and self.counters["retr"] == self.f["drop_on_retr"]:
            self.closed = True
            raise OSError("conexión reiniciada")
        if path not in self.fs.files:
            raise ftplib.error_perm("550 No such file")
        d = self.fs.files[path]
        for i in range(0, len(d), blocksize):
            cb(d[i:i + blocksize])

    def rename(self, a, b):
        self._alive()
        self.counters["rename"] += 1
        if self.f.get("rename_fail"):
            raise ftplib.error_perm("550 rename failed")
        if a not in self.fs.files:
            raise ftplib.error_perm("550 no existe")
        if self._vol(a) != self._vol(b):
            raise ftplib.error_perm("550 Cross-device link (EXDEV)")
        if b in self.fs.files and self.f.get("rename_overwrite") is False:
            raise ftplib.error_perm("550 File exists")
        self.fs.files[b] = self.fs.files.pop(a)
        if self.f.get("drop_on_rename") and self.counters["rename"] == self.f["drop_on_rename"]:
            self.closed = True
            raise EOFError("conexión perdida")

    def delete(self, p):
        self._alive()
        if p not in self.fs.files:
            raise ftplib.error_perm("550 no existe")
        del self.fs.files[p]

    def mkd(self, p):
        self._alive()
        if p in self.fs.dirs:
            raise ftplib.error_perm("550 existe")
        self.fs.dirs.add(p)
        return p

    def rmd(self, p):
        self._alive()
        if any(k.startswith(p + "/") for k in self.fs.files):
            raise ftplib.error_perm("550 no está vacía")
        self.fs.dirs.discard(p)

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True


class FakePM:
    def __init__(self, fs, base="/data/pldmgr/payloads/local"):
        self.fs = fs
        self.base = base
        self.items = []
        self.conf = {"AUTOLOAD_LIST": "", "AUTOLOAD_ENABLED": True}
        self.fail_upload = None       # "before" | "after" | "corrupt"
        self.fail_set = None          # "before" | "after" | "wrong"

    def payloads(self):
        return [dict(x) for x in self.items]

    def upload(self, filename, data):
        if self.fail_upload == "before":
            raise OSError("timed out")
        slot = "local/" + filename.rsplit(".", 1)[0]
        path = "%s/%s/%s" % (self.base, filename.rsplit(".", 1)[0], filename)
        self.fs.files[path] = data if self.fail_upload != "corrupt" else data[:-10]
        self.items.append({"slot": slot, "filename": filename, "path": path, "name": filename, "kind": "local"})
        if self.fail_upload == "after":
            raise OSError("connection reset")

    def delete(self, slot):
        it = next(x for x in self.items if x["slot"] == slot)
        self.items.remove(it)
        self.fs.files.pop(it["path"], None)

    def config(self):
        return dict(self.conf)

    def set_config(self, conf):
        if self.fail_set == "before":
            raise OSError("timed out")
        if self.fail_set == "wrong":
            self.fail_set = None              # solo la primera vez
            self.conf["AUTOLOAD_LIST"] = "algo,raro"
            return
        self.conf.update(conf)
        if self.fail_set == "after":
            raise OSError("connection reset")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fs = FakeFS()
        self.faults = {}
        self.store = P.Store(self.tmp)
        self.busy = ""
        self.safe = P.Safe(self.store, connect=lambda: FakeFTP(self.fs, **self.faults), busy=lambda: self.busy)
        self.target = "/data/ps5_autoloader/kstuff.elf"
        self.fs.dirs |= {"/data/ps5_autoloader"}
        self.old = make_elf(b"A" * 2000)
        self.new = make_elf(b"B" * 2500)
        self.fs.files[self.target] = self.old

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install(self, **kw):
        kw.setdefault("allow_experimental", True)
        return self.safe.ftp_install(self.target, self.new, {"name": "kstuff-lite", "version": "1.11"}, **kw)

    def assertClean(self):
        self.assertEqual(self.store.journal(), {})
        self.assertFalse([k for k in self.fs.files if "/.pldtx/" in k], "quedan temporales en la consola")


class ElfTests(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(P.elf_check(make_elf())["ok"])

    def test_truncated(self):
        r = P.elf_check(make_elf()[:-500])
        self.assertFalse(r["ok"])
        self.assertIn("cortado", " ".join(r["errors"]))

    def test_not_elf(self):
        self.assertFalse(P.elf_check(b"PK\x03\x04" + b"\0" * 200)["ok"])

    def test_wrong_arch(self):
        d = bytearray(make_elf())
        struct.pack_into("<H", d, 18, 0xB7)      # aarch64
        self.assertFalse(P.elf_check(bytes(d))["ok"])

    def test_entry_outside(self):
        self.assertFalse(P.elf_check(make_elf(entry_off=10 ** 7))["ok"])


class ClassifyTests(unittest.TestCase):
    def test_stable_in_name_is_still_experimental(self):
        c = P.classify("", "", "kstuff-1.15-dr-test3-stable.elf", "13.60")
        self.assertTrue(c["experimental"])
        self.assertTrue(c["critical"])
        self.assertEqual(c["family"], "kstuff-dr")
        self.assertEqual(c["level"], "experimental")

    def test_beta(self):
        self.assertTrue(P.classify("ShadowMountPlus", "1.7beta12", "")["experimental"])

    def test_normal(self):
        c = P.classify("ftpsrv", "0.14", "ftpsrv.elf")
        self.assertEqual(c["level"], "normal")

    def test_unknown(self):
        self.assertEqual(P.classify("", "", "algo_raro.elf")["level"], "desconocido")

    def test_firmware(self):
        c = P.classify("etaHEN", "2.1", "etaHEN.elf", "13.60")
        self.assertFalse(c["fw_ok"])


class AutoloadTests(unittest.TestCase):
    items = {"s/klog": {"filename": "klogsrv.elf"}, "s/kdr": {"filename": "kstuff-1.15-dr-test3-stable.elf"},
             "s/smp": {"filename": "shadowmountplus.elf", "version": "1.7beta12"},
             "s/ftp": {"filename": "ftpsrv.elf"}, "s/kl": {"filename": "kstuff-lite-1.11.elf"},
             "s/eta": {"filename": "etaHEN-2.1.elf"}, "s/sw": {"filename": "sentinel_warden.elf"}}

    def codes(self, lst, **kw):
        r = P.autoload_check(P.parse_autoload(lst), self.items, **kw)
        return {i["code"] for i in r["issues"]}, r

    def test_three_seconds_is_too_little(self):
        codes, r = self.codes("s/klog,!3000,s/kdr,!3000,s/smp")
        self.assertIn("delay_before", codes)
        self.assertIn("delay_after", codes)
        self.assertIn("experimental", codes)
        fixed = P.parse_autoload(P.join_autoload(r["fixed"]))
        codes2, _ = self.codes(P.join_autoload(fixed))
        self.assertNotIn("delay_before", codes2)
        self.assertNotIn("delay_after", codes2)

    def test_good_list(self):
        codes, _ = self.codes("s/klog,!5000,s/kdr,!5000,s/smp,s/ftp")
        self.assertFalse(codes & {"delay_before", "delay_after", "dup_slot", "kpatch", "order"})

    def test_duplicates_and_conflicts(self):
        codes, r = self.codes("s/kl,!5000,s/kdr,!5000,s/smp,s/smp,s/sw")
        self.assertIn("kpatch", codes)
        self.assertIn("dup_slot", codes)
        self.assertIn("conflict", codes)                 # sentinel_warden con ShadowMountPlus 1.7
        self.assertEqual(sum(1 for e in r["fixed"] if e["key"] == "s/smp"), 1)

    def test_order(self):
        codes, _ = self.codes("s/smp,!5000,s/kdr,!5000,s/ftp")
        self.assertIn("order", codes)

    def test_order_smp_then_kstuff_lite(self):
        # lo que pide el README de ShadowMountPlus: él primero y kstuff(-lite) 3 s después
        codes, _ = self.codes("s/smp,!3000,s/kl,!3000,s/ftp")
        self.assertNotIn("order", codes)
        codes, r = self.codes("s/kl,!3000,s/smp,s/ftp")
        self.assertIn("order", codes)
        keys = [e["key"] for e in r["fixed"] if e.get("key")]
        self.assertLess(keys.index("s/smp"), keys.index("s/kl"))

    def test_kstuff_lite_on_1360(self):
        self.assertTrue(P.classify("kstuff-lite", "1.11", "kstuff-lite-1.11.elf", "13.60")["fw_ok"])

    def test_firmware_and_etahen(self):
        codes, _ = self.codes("s/eta,!5000,s/kdr", fw="13.60")
        self.assertIn("fw", codes)
        self.assertIn("conflict", codes)

    def test_running(self):
        iss = P.running_check([{"name": "kstuff-lite.elf"}, {"name": "kstuff-1.15-dr-test3.elf"}])
        self.assertIn("kpatch_running", {i["code"] for i in iss})
        iss = P.running_check([{"name": "ftpsrv.elf"}, {"name": "ftpsrv.elf"}])
        self.assertIn("dup_running", {i["code"] for i in iss})


class FtpInstallTests(Base):
    def test_ok(self):
        r = self.install()
        self.assertTrue(r["ok"])
        self.assertEqual(self.fs.files[self.target], self.new)
        self.assertClean()
        self.assertTrue(self.store.copy_ok(P.sha256(self.old)), "falta la copia de la versión anterior")
        ops = [x["op"] for x in self.store.read_log()]
        self.assertIn("instalar", ops)

    def test_same_file(self):
        self.fs.files[self.target] = self.new
        self.assertTrue(self.install()["same"])

    def test_invalid_elf_never_touches(self):
        with self.assertRaises(P.SafeError):
            self.safe.ftp_install(self.target, self.new[:-300], {"name": "kstuff-lite"})
        self.assertEqual(self.fs.files[self.target], self.old)
        self.assertEqual(self.fs.counters["stor"], 0) if hasattr(self.fs, "counters") else None

    def test_experimental_needs_confirm(self):
        with self.assertRaises(P.NeedsConfirm):
            self.safe.ftp_install(self.target, self.new, {"name": "kstuff-1.15-dr-test3-stable"})
        self.assertEqual(self.fs.files[self.target], self.old)

    def test_blocked_while_busy(self):
        self.busy = "1 transferencia"
        with self.assertRaises(P.SafeError) as cm:
            self.install()
        self.assertIn("transferencia", str(cm.exception))
        self.assertEqual(self.fs.files[self.target], self.old)

    def test_truncated_upload(self):
        self.faults["truncate_stor"] = 1000
        with self.assertRaises(P.SafeError):
            self.install()
        self.assertEqual(self.fs.files[self.target], self.old)
        self.assertClean()

    def test_wrong_hash_upload(self):
        self.faults["corrupt_stor"] = True
        with self.assertRaises(P.SafeError):
            self.install()
        self.assertEqual(self.fs.files[self.target], self.old)
        self.assertClean()

    def test_rename_fails_completely(self):
        self.faults["rename_fail"] = True
        with self.assertRaises(P.SafeError) as cm:
            self.install()
        self.assertIn("no escribo encima", str(cm.exception))
        self.assertEqual(self.fs.files[self.target], self.old)
        self.assertClean()

    def test_rename_no_overwrite_uses_two_steps(self):
        self.faults["rename_overwrite"] = False
        r = self.install()
        self.assertEqual(r["mode"], "dos_pasos")
        self.assertEqual(self.fs.files[self.target], self.new)
        self.assertClean()

    def test_staging_same_volume(self):
        self.assertEqual(P.stage_root("/data/pldmgr/x.elf"), "/data/ps5directo/.pldtx")
        self.assertEqual(P.stage_root("/mnt/usb0/ps5_autoloader/x.elf"), "/mnt/usb0/ps5directo/.pldtx")
        self.assertIsNone(P.stage_root("/system/x.elf"))

    def test_usb_target(self):
        t = "/mnt/usb0/ps5_autoloader/kstuff.elf"
        self.fs.dirs |= {"/mnt", "/mnt/usb0", "/mnt/usb0/ps5_autoloader"}
        self.fs.files[t] = self.old
        self.safe.ftp_install(t, self.new, {"name": "kstuff-lite"}, allow_experimental=True)
        self.assertEqual(self.fs.files[t], self.new)

    def test_disconnect_after_upload_recovers(self):
        self.faults["drop_after_stor"] = 1
        with self.assertRaises(P.Interrupted) as cm:
            self.install()
        self.assertIn("Recuperación", str(cm.exception))
        # al recuperar con una conexión nueva y buena: no se cambió nada, todo limpio
        self.faults.clear()
        self.safe.recover()
        self.assertEqual(self.fs.files[self.target], self.old)
        self.assertClean()

    def test_disconnect_during_final_rename_two_steps(self):
        # se corta justo después de apartar la anterior: el destino queda vacío
        self.faults.update(rename_overwrite=False, drop_on_rename=2)
        with self.assertRaises(P.Interrupted):
            self.install()
        self.faults.clear()
        self.safe.recover()
        self.assertIn(self.fs.files.get(self.target), (self.old, self.new))
        self.assertClean()

    def test_disconnect_on_verify_readback(self):
        self.faults["drop_on_retr"] = 3          # 1: leer anterior, 2: comprobar temporal, 3: comprobar final
        with self.assertRaises(P.Interrupted):
            self.install()
        self.faults.clear()
        self.safe.recover()
        self.assertEqual(self.fs.files[self.target], self.new)    # ya estaba puesta y se comprueba
        self.assertClean()


class CrashTests(Base):
    """Cierre inesperado de PS5 Directo en cada punto: al volver a abrir, recover() deja todo coherente."""

    def crash_at(self, point, **faults):
        self.faults.update(faults)

        def hook(p, tx=None):
            if p == point:
                raise Crash()
        safe = P.Safe(self.store, connect=lambda: FakeFTP(self.fs, **self.faults), busy=lambda: "", hook=hook)
        with self.assertRaises(Crash):
            safe.ftp_install(self.target, self.new, {"name": "kstuff-lite"}, allow_experimental=True)
        self.assertTrue(self.store.journal(), "el diario debía quedar abierto")
        # «reinicio»: objetos nuevos sobre la misma carpeta
        store2 = P.Store(self.tmp)
        safe2 = P.Safe(store2, connect=lambda: FakeFTP(self.fs), busy=lambda: "")
        res = safe2.recover()
        self.assertEqual(len(res), 1)
        self.store = store2
        return list(res.values())[0]

    def test_crash_before_upload(self):
        r = self.crash_at("preparado")
        self.assertIn("sin cambios", r)
        self.assertEqual(self.fs.files[self.target], self.old)
        self.assertClean()

    def test_crash_after_upload(self):
        self.crash_at("subido")
        self.assertEqual(self.fs.files[self.target], self.old)
        self.assertClean()

    def test_crash_before_switch(self):
        self.crash_at("antes_de_cambiar")
        self.assertEqual(self.fs.files[self.target], self.old)
        self.assertClean()

    def test_crash_between_two_renames(self):
        r = self.crash_at("apartado", rename_overwrite=False)
        self.assertIn("deshecho", r)
        self.assertEqual(self.fs.files[self.target], self.old)
        self.assertClean()

    def test_crash_after_switch_before_verify(self):
        r = self.crash_at("cambiado")
        self.assertIn("terminado", r)
        self.assertEqual(self.fs.files[self.target], self.new)
        self.assertClean()

    def test_crash_and_old_copy_gone_from_console_uses_pc_copy(self):
        self.faults["rename_overwrite"] = False

        def hook(p, tx=None):
            if p == "apartado":
                del self.fs.files[tx["aside"]]       # la copia apartada en la consola se ha perdido
                raise Crash()
        safe = P.Safe(self.store, connect=lambda: FakeFTP(self.fs, **self.faults), hook=hook)
        with self.assertRaises(Crash):
            safe.ftp_install(self.target, self.new, {"name": "kstuff-lite"}, allow_experimental=True)
        P.Safe(P.Store(self.tmp), connect=lambda: FakeFTP(self.fs)).recover()
        self.assertEqual(self.fs.files[self.target], self.old)

    def test_conflict_left_alone(self):
        def hook(p, tx=None):
            if p == "cambiado":
                self.fs.files[self.target] = make_elf(b"Z" * 100)   # otro programa lo cambió
                raise Crash()
        safe = P.Safe(self.store, connect=lambda: FakeFTP(self.fs), hook=hook)
        with self.assertRaises(Crash):
            safe.ftp_install(self.target, self.new, {"name": "kstuff-lite"}, allow_experimental=True)
        res = P.Safe(P.Store(self.tmp), connect=lambda: FakeFTP(self.fs)).recover()
        self.assertIn("conflicto", list(res.values())[0])
        self.assertEqual(self.fs.files[self.target], make_elf(b"Z" * 100))

    def test_recover_while_console_offline_keeps_journal(self):
        def hook(p, tx=None):
            if p == "subido":
                raise Crash()
        safe = P.Safe(self.store, connect=lambda: FakeFTP(self.fs), hook=hook)
        with self.assertRaises(Crash):
            safe.ftp_install(self.target, self.new, {"name": "kstuff-lite"}, allow_experimental=True)

        def down():
            raise OSError("sin conexión")
        res = P.Safe(P.Store(self.tmp), connect=down).recover()
        self.assertTrue(list(res.values())[0].startswith("pendiente"))
        self.assertTrue(P.Store(self.tmp).journal())


class PmTests(Base):
    def setUp(self):
        Base.setUp(self)
        self.pm = FakePM(self.fs)

    def test_install_verified(self):
        r = self.safe.pm_install(self.pm, "kstuff-lite_1.11.elf", self.new, {"name": "kstuff-lite"})
        self.assertTrue(r["ok"])
        self.assertEqual(self.fs.files[r["path"]], self.new)
        self.assertEqual(self.store.journal(), {})

    def test_install_corrupt_is_removed(self):
        self.pm.fail_upload = "corrupt"
        with self.assertRaises(P.SafeError):
            self.safe.pm_install(self.pm, "kstuff-lite_1.11.elf", self.new, {"name": "kstuff-lite"})
        self.assertEqual(self.pm.items, [])

    def test_upload_timeout_but_saved(self):
        self.pm.fail_upload = "after"
        r = self.safe.pm_install(self.pm, "kstuff-lite_1.11.elf", self.new, {"name": "kstuff-lite"})
        self.assertTrue(r["ok"])

    def test_never_replaces_existing_name(self):
        self.safe.pm_install(self.pm, "x.elf", make_elf(b"1" * 500), {"name": "ftpsrv"})
        r = self.safe.pm_install(self.pm, "x.elf", make_elf(b"2" * 500), {"name": "ftpsrv"})
        self.assertNotEqual(r["filename"], "x.elf")
        self.assertEqual(len(self.pm.items), 2)

    def test_experimental_pm(self):
        with self.assertRaises(P.NeedsConfirm):
            self.safe.pm_install(self.pm, "kstuff-1.15-dr-test3-stable.elf", self.new, {})

    def test_autoload_change_and_restore(self):
        self.pm.items = [{"slot": "a", "filename": "ftpsrv.elf"}, {"slot": "b", "filename": "bfpilot.elf"}]
        self.pm.conf["AUTOLOAD_LIST"] = "a"
        r = self.safe.pm_set_autoload(self.pm, "a,!2000,b")
        self.assertEqual(self.pm.conf["AUTOLOAD_LIST"], "a,!2000,b")
        self.safe.pm_restore_config(self.pm, r["backup"])
        self.assertEqual(self.pm.conf["AUTOLOAD_LIST"], "a")

    def test_autoload_experimental_not_added_silently(self):
        self.pm.items = [{"slot": "k", "filename": "kstuff-1.15-dr-test3-stable.elf"}]
        with self.assertRaises(P.NeedsConfirm):
            self.safe.pm_set_autoload(self.pm, "k")
        self.assertEqual(self.pm.conf["AUTOLOAD_LIST"], "")
        self.safe.pm_set_autoload(self.pm, "!5000,k,!5000", allow_experimental=True)
        self.assertEqual(self.pm.conf["AUTOLOAD_LIST"], "!5000,k,!5000")

    def test_autoload_wrong_result_rolls_back(self):
        self.pm.items = [{"slot": "a", "filename": "ftpsrv.elf"}]
        self.pm.conf["AUTOLOAD_LIST"] = "a"
        self.pm.fail_set = "wrong"
        with self.assertRaises(P.SafeError):
            self.safe.pm_set_autoload(self.pm, "a,!1000")
        self.pm.fail_set = None
        self.assertEqual(self.pm.conf["AUTOLOAD_LIST"], "a")
        self.assertEqual(self.store.journal(), {})

    def test_autoload_timeout_but_saved(self):
        self.pm.items = [{"slot": "a", "filename": "ftpsrv.elf"}]
        self.pm.fail_set = "after"
        r = self.safe.pm_set_autoload(self.pm, "a")
        self.assertTrue(r["ok"])

    def test_autoload_crash_recovered(self):
        self.pm.items = [{"slot": "a", "filename": "ftpsrv.elf"}]

        def hook(p, tx=None):
            if p == "cfg_guardado":
                raise Crash()
        safe = P.Safe(self.store, connect=lambda: FakeFTP(self.fs), hook=hook)
        with self.assertRaises(Crash):
            safe.pm_set_autoload(self.pm, "a")
        res = P.Safe(P.Store(self.tmp), connect=lambda: FakeFTP(self.fs)).recover(pm=self.pm)
        self.assertEqual(list(res.values())[0], "terminado")


class LaunchTests(Base):
    def test_unknown_never_retried_automatically(self):
        def boom():
            e = OSError("timed out after send")
            e.unknown = True
            raise e
        with self.assertRaises(P.UnknownLaunch):
            self.safe.launch("ftpsrv", boom, {"name": "ftpsrv"})
        calls = []
        with self.assertRaises(P.UnknownLaunch):
            self.safe.launch("ftpsrv", lambda: calls.append(1) or "ok", {"name": "ftpsrv"}, auto=True)
        self.assertEqual(calls, [])
        with self.assertRaises(P.UnknownLaunch):         # tampoco a mano sin confirmarlo
            self.safe.launch("ftpsrv", lambda: calls.append(1) or "ok", {"name": "ftpsrv"})
        self.assertEqual(self.safe.launch("ftpsrv", lambda: calls.append(1) or "ok", {"name": "ftpsrv"}, force=True),
                         "ok")
        self.assertEqual(calls, [1])
        self.assertFalse(self.safe.launch_blocked("ftpsrv"))

    def test_not_sent_is_not_unknown(self):
        def refused():
            e = OSError("connection refused")
            e.not_sent = True
            raise e
        with self.assertRaises(OSError):
            self.safe.launch("bfpilot", refused, {"name": "BFpilot"})
        self.assertFalse(self.safe.launch_blocked("bfpilot"))

    def test_critical_blocked_when_busy(self):
        self.busy = "2 transferencias"
        with self.assertRaises(P.SafeError):
            self.safe.launch("k", lambda: "ok", {"name": "kstuff-lite"})

    def test_experimental_not_autolaunched(self):
        with self.assertRaises(P.NeedsConfirm):
            self.safe.launch("k", lambda: "ok", {"filename": "kstuff-1.15-dr-test3-stable.elf"}, auto=True)


class LogTests(Base):
    def test_log_has_hashes_paths_versions(self):
        self.install()
        rec = [x for x in self.store.read_log() if x["op"] == "instalar"][-1]
        for k in ("sha256", "destino", "version", "fecha", "anterior"):
            self.assertIn(k, rec)

    def test_errors_logged(self):
        self.faults["truncate_stor"] = 10
        with self.assertRaises(P.SafeError):
            self.install()
        self.assertTrue(any(x.get("resultado") == "error" for x in self.store.read_log()))


if __name__ == "__main__":
    unittest.main()


class SameBootTests(Base):
    """Un parche del sistema (kstuff…) no se carga dos veces en el mismo arranque."""
    def test_second_kpatch_refused_until_console_restarts(self):
        meta = {"name": "kstuff", "filename": "kstuff.elf", "fw": "13.60"}
        self.assertEqual(self.safe.launch("a", lambda: "ok", meta), "ok")
        with self.assertRaises(P.SameBoot):
            self.safe.launch("b", lambda: "ok", {"name": "kstuff-lite", "filename": "kstuff-lite.elf", "fw": "13.60"})
        self.assertEqual(self.safe.launch("c", lambda: "ok", {"name": "ftpsrv", "filename": "ftpsrv.elf"}), "ok")
        self.safe.clear_launch(None, "reinicio")
        self.assertEqual(self.safe.launch("b", lambda: "ok", meta), "ok")

    def test_boot_after_send_allows(self):
        meta = {"name": "kstuff", "filename": "kstuff.elf", "fw": "13.60"}
        self.safe.launch("a", lambda: "ok", meta)
        self.safe.boot_epoch = lambda: time.time() + 1000        # la consola se encendió después de esa carga
        self.assertEqual(self.safe.launch("a", lambda: "ok", meta), "ok")
        self.safe.boot_epoch = lambda: time.time() - 3600        # encendida antes: mismo arranque
        with self.assertRaises(P.SameBoot):
            self.safe.launch("a", lambda: "ok", meta)
        self.assertEqual(self.safe.launch("a", lambda: "ok", meta, force=True), "ok")
