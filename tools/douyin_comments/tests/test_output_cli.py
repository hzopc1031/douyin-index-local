from argparse import Namespace
import csv
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from dyc.cli import check_env, defaults, parser
from dyc.models import Comment, Thread, Video
from dyc.output import COLUMNS, Log, export, ranking

ROOT = Path(__file__).resolve().parents[1]


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def test_csv_and_private_summary(self):
        video = Video("10000000000000000001", "https://dyc.invalid/video/10000000000000000001")
        texts = ['中文😀\n"引号",逗号', '=SUM(A1:A2)', '+123', '-123', '@cmd', '\ttab', '\rcarriage',
                 '<script>alert(1)</script> [click](javascript:bad) **markdown**']
        for i, text in enumerate(texts, 1):
            item = Comment(str(i), 1, str(i), "", text=text, like_count=0 if i % 2 else None,
                           user_id="99900000000000000001", nickname="CSVONLY_NICKNAME",
                           ip_label="CSVONLY_IP", reply_to_user_id="99900000000000000002",
                           reply_to_nickname="CSVONLY_OTHER", has_image=False if i % 2 else None)
            video.comments[item.comment_id] = item
            video.threads[item.comment_id] = Thread(item.comment_id, 0)
        video.comments["9"] = Comment("9", 2, "1", "1", text="回复", like_count=10)
        export(self.out, "synthetic-run", [video], 2, "partial", 1, 3)
        raw = (self.out / "comments.csv").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
        self.assertEqual(list(rows[0]), COLUMNS)
        self.assertEqual(rows[0]["text"], texts[0])
        for index in range(1, 7):
            self.assertEqual(rows[index]["text"], "'" + texts[index])
        self.assertEqual(rows[0]["video_id"], video.video_id)
        self.assertEqual(rows[0]["like_count"], "0")
        self.assertEqual(rows[1]["like_count"], "")
        self.assertEqual(rows[0]["has_image"], "0")
        self.assertEqual(rows[1]["has_image"], "")
        self.assertTrue(rows[0]["fetched_at"].endswith("+08:00"))
        md = (self.out / "summary.md").read_text()
        self.assertIn("根评论 1", md)
        for identity in ("CSVONLY", "99900000000000000001", "99900000000000000002", "nickname", "user_id", "ip_label"):
            self.assertNotIn(identity, md)
        for path in self.out.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_markdown_html_escaped(self):
        video = Video("1", "https://dyc.invalid/video/1")
        video.comments["1"] = Comment("1", 1, "1", "", text='<script>bad</script> [x](https://dyc.invalid) | **x**')
        export(self.out, "run", [video], 0, "complete", 1)
        md = (self.out / "summary.md").read_text()
        self.assertNotIn("<script>", md)
        self.assertNotIn("[x]", md)
        self.assertIn("&lt;script&gt;", md)

    def test_ranking_ties_and_missing(self):
        items = [Comment("2", 1, "2", "", like_count=1, created_at="2026-01-01"),
                 Comment("1", 1, "1", "", like_count=1, created_at="2026-01-01"),
                 Comment("3", 1, "3", "", like_count=1, created_at="2025-01-01"),
                 Comment("4", 1, "4", "", like_count=None),
                 Comment("5", 1, "5", "", like_count=0)]
        self.assertEqual([c.comment_id for c in ranking(items, 30)], ["3", "1", "2", "5", "4"])

    def test_no_secrets_exported_or_logged(self):
        video = Video("1", "https://dyc.invalid/video/1")
        secrets = "msToken=synthetic-secret a_bogus=synthetic-secret X-Bogus=synthetic-secret cookie=synthetic-secret https://dyc.invalid/path?private=synthetic-secret"
        video.displayed = secrets
        video.comments["1"] = Comment("1", 1, "1", "", text=secrets)
        export(self.out, "run", [video], 0, "complete", 1)
        logger = Log(self.out)
        logger.event(secrets)
        logger.close()
        for path in self.out.iterdir():
            self.assertNotIn("synthetic-secret", path.read_text(encoding="utf-8-sig"))
        json.loads((self.out / "run.json").read_text())


class CLITests(unittest.TestCase):
    def test_wrapper_resolves_urls_file_and_output_from_callers_directory(self):
        with tempfile.TemporaryDirectory(prefix="dyc-wrapper-") as temp:
            root = Path(temp)
            # Invalid synthetic input stops before constructing any browser.
            (root / "urls.txt").write_text("synthetic-invalid-url\n")
            relative_out = root.name + "-out"
            misplaced = ROOT / relative_out
            self.assertFalse(misplaced.exists())
            self.addCleanup(shutil.rmtree, misplaced, True)
            env = {**os.environ, "DYC_PYTHON": sys.executable, "DYC_NO_SCOPE": "1"}
            result = subprocess.run([str(ROOT / "bin/dyc"), "fetch", "--urls-file", "urls.txt",
                                     "--out", relative_out], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            data = json.loads(result.stdout)
            self.assertEqual(data["exit_reason"], "invalid_video_url")
            self.assertEqual(data["out"], str(root / relative_out))
            self.assertTrue((root / relative_out / "comments.csv").exists())
            self.assertFalse(misplaced.exists())

    def test_wrapper_resolves_relative_profile_and_state_flags_and_environment(self):
        with tempfile.TemporaryDirectory(prefix="dyc-wrapper-") as temp:
            root = Path(temp)
            misplaced = ROOT / root.name
            self.assertFalse(misplaced.exists())
            self.addCleanup(shutil.rmtree, misplaced, True)
            for use_env in (False, True):
                with self.subTest(environment=use_env):
                    profile, state = root.name + "/profile", root.name + "/state"
                    env = {**os.environ, "DYC_PYTHON": sys.executable, "DYC_NO_SCOPE": "1"}
                    argv = [str(ROOT / "bin/dyc"), "check-env"]
                    if use_env:
                        env.update(DYC_PROFILE_DIR=profile, DYC_STATE_DIR=state)
                    else:
                        argv += ["--profile-dir", profile, "--state-dir", state]
                    result = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(json.loads(result.stdout)["profile_dir"], str(root / profile))
                    self.assertTrue((root / profile / ".dyc.lock").exists())
                    self.assertTrue((root / state / ".usage.lock").exists())
                    self.assertFalse(misplaced.exists())

    def test_help_everywhere(self):
        for command in ([], ["fetch"], ["login"], ["check-env"], ["selftest"]):
            result = subprocess.run([sys.executable, "-m", "dyc", *command, "--help"],
                                    capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(result.returncode, 0)
            self.assertIn("usage:", result.stdout)

    def test_no_public_fast_or_resume_flag(self):
        for option in ("--fast", "--resume"):
            result = subprocess.run([sys.executable, "-m", "dyc", "fetch", option],
                                    capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(len(result.stdout.splitlines()), 1)
            self.assertEqual(json.loads(result.stdout)["exit_reason"], "invalid_arguments")

    def test_common_arguments_before_or_after_subcommand(self):
        for argv in (["--profile-dir", "/tmp/synthetic-profile", "check-env"],
                     ["check-env", "--profile-dir", "/tmp/synthetic-profile"]):
            self.assertEqual(defaults(parser().parse_args(argv)).profile_dir, Path("/tmp/synthetic-profile"))

    def test_check_env_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            args = defaults(Namespace(profile_dir=temp + "/profile", state_dir=temp + "/state", display=None))
            code, result = check_env(args)
            self.assertEqual(code, 0, result)
            self.assertEqual(result["today_requests"], 0)
            self.assertIsNotNone(result["playwright"])
            Path(args.profile_dir).chmod(0o755)
            code, result = check_env(args)
            self.assertEqual(code, 5)

    def test_wrapper_scope_reexec(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = root / "scope.json"
            scope = root / "systemd-run"
            scope.write_text('#!' + sys.executable + '\nimport json, os, sys\n'
                             'from pathlib import Path\n'
                             'Path(os.environ["SYNTHETIC_SCOPE_RECORD"]).write_text(json.dumps(sys.argv[1:]))\n'
                             'assert os.environ["DYC_IN_SCOPE"] == "1"\n'
                             'os.execv(sys.argv[6], sys.argv[6:])\n')
            manager = root / "systemctl"
            manager.write_text("#!/bin/sh\nexit 0\n")
            scope.chmod(0o700)
            manager.chmod(0o700)
            env = {**os.environ, "PATH": temp + ":" + os.environ["PATH"],
                   "DYC_PYTHON": sys.executable, "SYNTHETIC_SCOPE_RECORD": str(record)}
            env.pop("DYC_NO_SCOPE", None)
            env.pop("DYC_IN_SCOPE", None)
            result = subprocess.run([str(ROOT / "bin/dyc"), "--help"], env=env, cwd=root,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(record.read_text())[:5],
                             ["--user", "--scope", "--quiet", "-p", "MemoryMax=1500M"])
