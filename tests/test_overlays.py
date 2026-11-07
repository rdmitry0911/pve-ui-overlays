#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Sandbox tests (--root): no dpkg database, no real PVE. Needs a stock
index.html.tpl of a supported pve-manager in tests/fixtures or STOCK_TPL=path."""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import pve_ui_overlays as M  # noqa: E402

STOCK = Path(os.environ.get("STOCK_TPL", HERE / "fixtures" / "index.html.tpl"))
VERSION = os.environ.get("STOCK_VERSION", "9.2.21")
QSM_MARKER = b"<!-- qsm-pve-direct PVE 9 console overlay; package-managed, do not edit -->\n"


class Sandbox:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pve-ui-overlays-test."))
        self.root = self.dir / "root"
        for rel in (M.TEMPLATE.parent, M.JS_DIR, M.SHARE / "overlays.d", M.SHARE / "manifest.d", M.VAR, M.ETC / "overlays.d"):
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(STOCK, self.root / M.TEMPLATE)
        (self.root / M.MANIFEST).write_text(f"{VERSION}\t{M.sha256(STOCK.read_bytes())}\n")
        self.m = M.Manager(self.root, version_fn=lambda: VERSION)

    def script(self, name, body=b"/* x */\n"):
        (self.root / M.JS_DIR / name).write_bytes(body)

    def descriptor(self, ident, where=M.SHARE / "overlays.d", **extra):
        d = {"id": ident, "scripts": [f"/pve2/js/{ident}.js"], **extra}
        (self.root / where / f"{ident}.json").write_text(json.dumps(d))
        self.script(f"{ident}.js", f"/* {ident} */\n".encode())
        return d

    def template(self):
        return (self.root / M.TEMPLATE).read_bytes()

    def cleanup(self):
        shutil.rmtree(self.dir)


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)

    def test_no_overlays_leaves_stock(self):
        r = self.sb.m.reconcile()
        self.assertEqual(r.state, "inactive")
        self.assertEqual(self.sb.template(), STOCK.read_bytes())
        self.assertEqual(self.sb.m.status().state, "inactive")

    def test_order_independent_and_idempotent(self):
        self.sb.descriptor("bbb", order=10)
        self.sb.descriptor("aaa", order=20)
        r = self.sb.m.reconcile()
        self.assertEqual((r.state, r.overlays), ("active", ["bbb", "aaa"]))
        first = self.sb.template()
        self.assertIn(b"<!-- overlay bbb -->", first)
        self.assertLess(first.index(b"/pve2/js/bbb.js"), first.index(b"/pve2/js/aaa.js"))
        self.assertLess(first.index(b"pvemanagerlib.js"), first.index(M.BEGIN))
        self.assertFalse(self.sb.m.reconcile().changed)
        self.assertEqual(self.sb.m.status().state, "active")
        # a second sandbox with the opposite install order renders the same bytes
        other = Sandbox(); self.addCleanup(other.cleanup)
        other.descriptor("aaa", order=20); other.descriptor("bbb", order=10)
        other.m.reconcile()
        self.assertEqual(other.template(), first)

    def test_remove_last_overlay_restores_stock(self):
        self.sb.descriptor("one")
        self.sb.m.reconcile()
        self.assertNotEqual(self.sb.template(), STOCK.read_bytes())
        (self.sb.root / M.SHARE / "overlays.d" / "one.json").unlink()
        r = self.sb.m.reconcile()
        self.assertEqual(r.state, "inactive")
        self.assertEqual(self.sb.template(), STOCK.read_bytes())
        self.assertIsNone(self.sb.m.div.owner(self.sb.root / M.TEMPLATE))

    def test_disable_enable_and_runtime_register(self):
        self.sb.descriptor("pkg")
        self.sb.script("rt.js")
        self.sb.m.register({"id": "rt", "scripts": ["/pve2/js/rt.js"], "order": 1})
        self.assertEqual(self.sb.m.status().overlays, ["rt", "pkg"])
        self.sb.m.set_enabled("pkg", False)
        self.assertEqual(self.sb.m.status().overlays, ["rt"])
        self.assertIn("pkg", self.sb.m.status().skipped)
        self.sb.m.set_enabled("pkg", True)
        self.assertTrue(self.sb.m.unregister("rt"))
        self.assertEqual(self.sb.m.status().overlays, ["pkg"])
        self.assertFalse(self.sb.m.unregister("rt"))

    def test_globals_literal_file_keys_default(self):
        cfg = self.sb.root / "etc" / "x" / "ui.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"port": 1234, "users": ["root@pam"], "secret": "<no>"}))
        self.sb.descriptor("g", globals={
            "G_LIT": {"value": {"a": "<b>&"}},
            "G_FILE": {"file": "/etc/x/ui.json", "keys": ["port", "missing"], "default": {"port": 1, "extra": True}},
            "G_ABSENT": {"file": "/etc/x/none.json", "keys": ["port"]},
            "G_ABSENT_DEFAULT": {"file": "/etc/x/none.json", "default": {"port": 9}},
        })
        self.sb.m.reconcile()
        t = self.sb.template()
        self.assertIn(b'window.G_LIT={"a":"\\u003cb\\u003e\\u0026"};', t)
        self.assertIn(b'window.G_FILE={"port":1234,"extra":true};', t)
        self.assertNotIn(b"users", t)
        self.assertNotIn(b"secret", t)
        self.assertNotIn(b"G_ABSENT=", t)
        self.assertIn(b'window.G_ABSENT_DEFAULT={"port":9};', t)

    def test_invalid_descriptor_and_missing_script_are_skipped(self):
        self.sb.descriptor("good")
        (self.sb.root / M.SHARE / "overlays.d" / "bad.json").write_text('{"id": "bad", "scripts": []}')
        (self.sb.root / M.SHARE / "overlays.d" / "noscript.json").write_text(json.dumps({"id": "noscript", "scripts": ["/pve2/js/none.js"]}))
        r = self.sb.m.reconcile()
        self.assertEqual(r.overlays, ["good"])
        self.assertIn("bad", r.skipped)
        self.assertIn("noscript", r.skipped)

    def test_unsupported_pve_restores_stock(self):
        self.sb.descriptor("one")
        self.sb.m.reconcile()
        self.sb.m.version_fn = lambda: "9.9.9"
        r = self.sb.m.reconcile()
        self.assertEqual(r.state, "disabled-unsupported-pve")
        self.assertEqual(self.sb.template(), STOCK.read_bytes())
        self.assertEqual(self.sb.m.status().state, "disabled-unsupported-pve")
        self.sb.m.version_fn = lambda: VERSION
        self.assertEqual(self.sb.m.reconcile().state, "active")

    def test_manifest_dir_rows_extend_allow_list(self):
        (self.sb.root / M.MANIFEST).write_text("1.0\t" + "0" * 64 + "\n")
        (self.sb.root / M.SHARE / "manifest.d" / "client.sha256").write_text(f"{VERSION}\t{M.sha256(STOCK.read_bytes())}\n")
        self.sb.descriptor("one")
        self.assertEqual(self.sb.m.reconcile().state, "active")

    def _legacy_qsm(self):
        """Simulate an older qsm-pve-direct that diverted and rendered the template itself."""
        self.sb.script("qsm-pve-direct-console.js", b"/* qsm */\n")
        stock = STOCK.read_bytes()
        rendered = stock.replace(M.ANCHOR, M.ANCHOR + QSM_MARKER
                                 + b'    <script type="text/javascript" src="/pve2/js/qsm-pve-direct-console.js?ver=[% version %]-qsm-overlay-0123456789abcdef"></script>\n', 1)
        (self.sb.root / M.TEMPLATE).write_bytes(rendered)
        Path(f"{self.sb.root / M.TEMPLATE}.qsm-pve-direct-orig").write_bytes(stock)
        self.sb.m.div.state.parent.mkdir(parents=True, exist_ok=True)
        self.sb.m.div.state.write_text("qsm-pve-direct\n")
        return rendered

    def test_alongside_legacy_qsm(self):
        legacy = self._legacy_qsm()
        self.sb.descriptor("qdev-host")
        # a new-style qsm descriptor for a script the legacy render already has must not be duplicated
        self.sb.descriptor("qsm-pve-direct", order=40, scripts=["/pve2/js/qsm-pve-direct-console.js"])
        r = self.sb.m.reconcile()
        self.assertEqual((r.state, r.overlays), ("alongside-legacy", ["qdev-host"]))
        self.assertIn("qsm-pve-direct", r.skipped)
        t = self.sb.template()
        self.assertEqual(t.count(b"qsm-pve-direct-console.js"), 1)
        self.assertIn(QSM_MARKER, t)
        self.assertIn(b"/pve2/js/qdev-host.js", t)
        self.assertEqual(self.sb.m.status().state, "alongside-legacy")
        self.assertFalse(self.sb.m.reconcile().changed)
        # legacy qsm re-renders (drops our block): reconcile puts it back
        (self.sb.root / M.TEMPLATE).write_bytes(legacy)
        self.assertEqual(self.sb.m.status().state, "stale")
        self.assertTrue(self.sb.m.reconcile().changed)
        self.assertEqual(self.sb.template(), t)
        # remove gives qsm its own render back
        self.sb.m.remove()
        self.assertEqual(self.sb.template(), legacy)
        # legacy qsm uninstalls (restores stock, drops diversion): we take ownership
        (self.sb.root / M.TEMPLATE).write_bytes(STOCK.read_bytes())
        Path(f"{self.sb.root / M.TEMPLATE}.qsm-pve-direct-orig").unlink()
        self.sb.m.div.state.unlink()
        r = self.sb.m.reconcile()
        self.assertEqual((r.state, r.overlays), ("active", ["qsm-pve-direct", "qdev-host"]))
        self.assertEqual(self.sb.m.div.owner(self.sb.root / M.TEMPLATE), M.PACKAGE)

    def test_takeover_of_legacy_qdev_host_pve(self):
        stock = STOCK.read_bytes()
        block = (b"    <!-- qdev-host-pve PVE 9 hardware overlay begin; package-managed, do not edit -->\n"
                 b'    <script type="text/javascript" src="/pve2/js/qdev-host-hardware.js?ver=[% version %]-qdev-overlay-0123456789abcdef"></script>\n'
                 b"    <!-- qdev-host-pve PVE 9 hardware overlay end -->\n")
        (self.sb.root / M.TEMPLATE).write_bytes(stock.replace(M.ANCHOR, M.ANCHOR + block, 1))
        Path(f"{self.sb.root / M.TEMPLATE}.qdev-host-pve-orig").write_bytes(stock)
        self.sb.m.div.state.parent.mkdir(parents=True, exist_ok=True)
        self.sb.m.div.state.write_text("qdev-host-pve\n")
        self.sb.descriptor("qdev-host")
        r = self.sb.m.reconcile()
        self.assertEqual((r.state, r.overlays), ("active", ["qdev-host"]))
        self.assertEqual(self.sb.m.div.owner(self.sb.root / M.TEMPLATE), M.PACKAGE)
        self.assertNotIn(b"qdev-host-pve PVE 9 hardware overlay", self.sb.template())
        self.assertFalse(Path(f"{self.sb.root / M.TEMPLATE}.qdev-host-pve-orig").exists())

    def test_foreign_diversion_is_refused(self):
        self.sb.m.div.state.parent.mkdir(parents=True, exist_ok=True)
        self.sb.m.div.state.write_text("someone-else\n")
        self.sb.descriptor("one")
        with self.assertRaises(M.UnsafeState):
            self.sb.m.reconcile()
        self.assertEqual(self.sb.m.status().state, "foreign-diversion")

    def test_cli(self):
        self.sb.descriptor("one", description="first overlay")
        cli = [sys.executable, str(HERE.parent / "pve_ui_overlays.py"), "--root", str(self.sb.root), "--package-version", VERSION]
        out = subprocess.run(cli + ["reconcile"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("active; overlays: one", out.stdout)
        out = subprocess.run(cli + ["--json", "status"], capture_output=True, text=True)
        self.assertEqual(json.loads(out.stdout)["state"], "active")
        out = subprocess.run(cli + ["list"], capture_output=True, text=True)
        self.assertIn("first overlay", out.stdout)
        out = subprocess.run(cli + ["check", str(self.sb.root / M.SHARE / "overlays.d" / "one.json")], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "one: ok")
        out = subprocess.run(cli + ["template-hash"], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), f"{VERSION}\t{M.sha256(STOCK.read_bytes())}")
        out = subprocess.run(cli + ["render"], capture_output=True)
        self.assertEqual(out.stdout, self.sb.template())
        out = subprocess.run(cli + ["remove"], capture_output=True, text=True)
        self.assertEqual(self.sb.template(), STOCK.read_bytes())


if __name__ == "__main__":
    if not STOCK.exists():
        sys.exit(f"no stock template fixture at {STOCK}; set STOCK_TPL")
    unittest.main(verbosity=1)
