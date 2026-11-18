import json
import os
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chatdemo_archive as ca  # noqa: E402

BIN = os.path.join(ROOT, "bin", "chat-demo-history-repair")
BUILD = os.path.join(ROOT, "examples", "build_fixtures.py")
SPECS = os.path.join(ROOT, "examples", "in")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="cdhr-")
        subprocess.run([sys.executable, BUILD, "--out", cls.tmp, "--spec", SPECS],
                       check=True, capture_output=True)
        cls.fx = staticmethod(lambda name: os.path.join(cls.tmp, name + ".zip"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_cli(self, *args):
        proc = subprocess.run([sys.executable, BIN] + list(args),
                              capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr

    def codes(self, path):
        return [e["code"] for e in ca.audit(path)["errors"]]


class TestAudit(Base):
    def test_clean_archive_passes(self):
        report = ca.audit(self.fx("clean-v3"))
        self.assertTrue(report["ok"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["format_version"], 3)
        self.assertEqual(report["sessions"], 1)
        self.assertEqual(report["messages"], 2)
        self.assertEqual(report["images"], 1)

    def test_manifest_must_be_first(self):
        self.assertIn("E_MANIFEST_NOT_FIRST", self.codes(self.fx("manifest-last")))

    def test_nfd_names_are_flagged(self):
        codes = self.codes(self.fx("nfd-image"))
        self.assertEqual(codes.count("E_NAME_NOT_NFC"), 2)

    def test_dangling_image_is_an_error_and_orphan_is_a_warning(self):
        report = ca.audit(self.fx("dangling-image"))
        self.assertIn("E_IMAGE_MISSING", [e["code"] for e in report["errors"]])
        self.assertIn("W_IMAGE_ORPHAN", [w["code"] for w in report["warnings"]])
        self.assertFalse(report["ok"])

    def test_broken_tree_reports_order_and_orphan(self):
        codes = self.codes(self.fx("broken-tree"))
        self.assertIn("E_TREE_PARENT_ORDER", codes)
        self.assertIn("E_TREE_ORPHAN", codes)

    def test_non_zip_is_a_usage_error(self):
        path = os.path.join(self.tmp, "not-a-zip.zip")
        with open(path, "wb") as fh:
            fh.write(b"not a zip")
        rc, out, _ = self.run_cli(path, "--json")
        self.assertEqual(rc, 1)
        self.assertIn("E_ARCHIVE_OPEN", json.loads(out)["errors"][0]["code"])


class TestRepair(Base):
    def test_fix_reorders_manifest(self):
        out = os.path.join(self.tmp, "fixed-order.zip")
        rc, _, _ = self.run_cli(self.fx("manifest-last"), "--fix", out)
        self.assertEqual(rc, 1)  # the input had findings even though the rewrite is clean
        self.assertTrue(ca.audit(out)["ok"])
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(zf.namelist()[0], "manifest.json")

    def test_fix_normalises_names_and_references_together(self):
        out = os.path.join(self.tmp, "fixed-nfd.zip")
        self.run_cli(self.fx("nfd-image"), "--fix", out)
        report = ca.audit(out)
        self.assertTrue(report["ok"], report["errors"])
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            self.assertTrue(all(unicodedata.normalize("NFC", n) == n for n in names))
            session = json.loads(zf.read("chat_sessions/s1.json"))
            path = session["messages"][1]["fragments"][1]["files"][0]["export_path"]
            self.assertEqual(path, unicodedata.normalize("NFC", path))
            self.assertIn(path, names)

    def test_fix_refuses_content_level_errors(self):
        out = os.path.join(self.tmp, "must-not-exist.zip")
        rc, _, err = self.run_cli(self.fx("broken-tree"), "--fix", out)
        self.assertEqual(rc, 2)
        self.assertIn("refusing to fix", err)
        self.assertFalse(os.path.exists(out))


class TestCliContract(Base):
    def test_missing_argument_is_exit_2(self):
        rc, _, err = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_unknown_flag_is_exit_2(self):
        rc, _, _ = self.run_cli(self.fx("clean-v3"), "--nope")
        self.assertEqual(rc, 2)

    def test_missing_file_is_exit_2(self):
        rc, _, err = self.run_cli("/nonexistent/nope.zip")
        self.assertEqual(rc, 2)
        self.assertIn("no such file", err)

    def test_json_output_is_parseable_and_stable(self):
        _, first, _ = self.run_cli(self.fx("clean-v3"), "--json")
        _, second, _ = self.run_cli(self.fx("clean-v3"), "--json")
        self.assertEqual(first, second)
        payload = json.loads(first)
        for key in ("ok", "entries", "sessions", "messages", "images", "errors", "warnings", "repairs"):
            self.assertIn(key, payload)

    def test_exit_codes_distinguish_clean_and_dirty(self):
        self.assertEqual(self.run_cli(self.fx("clean-v3"))[0], 0)
        self.assertEqual(self.run_cli(self.fx("manifest-last"))[0], 1)

    def test_core_does_not_open_sockets(self):
        # audit() takes a path and reads bytes only; guard against accidental network use.
        source = open(os.path.join(ROOT, "chatdemo_archive.py"), encoding="utf-8").read()
        for forbidden in ("socket", "urllib", "http.client", "requests"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()