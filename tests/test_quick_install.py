"""Offline regressions for install.sh, quick_install.py, packaging, and P1-8..P1-12."""
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import py_compile
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class QuickInstallAndPackagingTests(unittest.TestCase):
    def test_p1_8_piped_install_sh_ignores_decoy_sh_in_cwd(self):
        script = (ROOT / "install.sh").read_text()
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            (cwd / "sh").write_text("#!/bin/sh\n")
            (cwd / "scripts").mkdir()
            marker = cwd / "hijacked"
            (cwd / "scripts" / "quick_install.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('pwned')\n")
            # Force git unavailable so if piped sh avoids decoy ./sh it fails cleanly before network fetch.
            env = dict(os.environ, PATH="/nonexistent", ATO_INSTALL_PYTHON=sys.executable)
            proc = subprocess.run(["/bin/sh"], input=script, cwd=cwd, env=env,
                                  capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(marker.exists())

    def test_p1_9_quick_install_validation_0755_releases_dir_and_no_duplicate_modules(self):
        from scripts import install as install_mod
        from scripts import quick_install as qi
        from scripts import release_snapshot as rs

        self.assertNotIn("install", sys.modules)
        self.assertNotIn("release_snapshot", sys.modules)
        # Standalone -IB invocation must work without implicit sys.path side effects.
        help_run = subprocess.run([sys.executable, "-IB", str(ROOT / "scripts/quick_install.py"), "--help"],
                                  capture_output=True, text=True)
        self.assertEqual(help_run.returncode, 0)

        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            # Empty/whitespace path arguments must be rejected, never targeting $PWD.
            for argv in (["--skills-dir", ""], ["--bin-dir", "   "], ["--releases-dir", ""]):
                err = io.StringIO()
                with redirect_stderr(err):
                    code = qi.main(argv)
                self.assertEqual(code, 2)
                self.assertIn("non-empty path", err.getvalue())

            # --bin-dir == --skills-dir must be refused.
            same = base / "same"
            same.mkdir(mode=0o755)
            with self.assertRaises(rs.Refused):
                qi.quick_install(ROOT, base / "releases", same, same)

            # Pre-existing 0755 --releases-dir must be accepted (relaxed from strict 0700).
            releases = base / "releases-0755"
            releases.mkdir(mode=0o755)
            os.chmod(releases, 0o755)
            bin_dir = base / "bin"
            called = {}
            def fake_stage(repo, commit, dest, **kwargs):
                called["staged"] = dest
                dest.mkdir(mode=0o700)
                (dest / rs.MANIFEST).write_text("{}")
            def fake_install(root, bdir, sdir):
                return dict(launcher=str(bdir / "ato"), source=str(root), release="a" * 64, skill=None)
            with patch.object(qi, "stage", side_effect=fake_stage), \
                    patch.object(qi, "install", side_effect=fake_install):
                out = qi.quick_install(ROOT, releases, bin_dir)
            self.assertEqual(out["launcher"], str(bin_dir / "ato"))
            self.assertIn("staged", called)

            # Pre-existing group-writable skills_dir/ato must be refused by install().
            skills = base / "skills"
            skills.mkdir(mode=0o755)
            ato_skill_dir = skills / "ato"
            ato_skill_dir.mkdir(mode=0o775)
            os.chmod(ato_skill_dir, 0o775)
            fake_snap = base / "snap"
            (fake_snap / "skills/ato").mkdir(parents=True)
            (fake_snap / "skills/ato/SKILL.md").write_text("# skill\n")
            with patch.object(install_mod, "snapshot_source",
                              return_value=dict(surface=rs.SURFACE, seal="a" * 64, root=str(fake_snap), python=sys.executable)), \
                    patch.object(install_mod, "entry_set", return_value={rs.PUBLIC: b"#!/bin/sh\n"}):
                with self.assertRaises(rs.Refused):
                    install_mod.install(fake_snap, bin_dir, skills)
                # Direct install() call must also reject bin_dir == skills_dir.
                with self.assertRaises(rs.Refused):
                    install_mod.install(fake_snap, bin_dir, bin_dir)

    def test_p1_10_p1_11_p1_12_packaging_scripts_and_advisory_docs(self):
        # P1-10: scripts/derive_pi_acp.py included in pyproject.toml data-files.
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]
        paths = {path for entries in data["data-files"].values() for path in entries}
        self.assertIn("scripts/derive_pi_acp.py", paths)
        self.assertTrue((ROOT / "scripts/derive_pi_acp.py").is_file())

        # P1-11: helper scripts no longer shell out to removed runner.py CLI and compile cleanly.
        for rel in ("scripts/create_acp_process_demo.py", "scripts/verify_pi_session_local.py"):
            path = ROOT / rel
            text = path.read_text()
            self.assertNotIn("runner.py", text)
            py_compile.compile(str(path), doraise=True)

        # P1-12: README and SKILL document advisory CHECKS_FAILED check before reporting success.
        self.assertIn("CHECKS_FAILED", (ROOT / "README.md").read_text())
        skill_text = (ROOT / "skills/ato/SKILL.md").read_text()
        self.assertIn("CHECKS_FAILED", skill_text)
        self.assertNotRegex(skill_text, r"/(?:Users|Volumes)/")


if __name__ == "__main__":
    unittest.main()
