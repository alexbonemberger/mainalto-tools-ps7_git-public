"""Offline integration tests with disposable local bare Git remotes."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "tools" / "mainalto_git_publish_provider.py"
spec = importlib.util.spec_from_file_location("mainalto_git_publish_provider", SOURCE)
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True).stdout.strip()


class GitPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ma-ps7git-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / "upstream.git"
        self.checkout = self.root / "source with espaço"
        git(self.root, "init", "--bare", str(self.remote))
        git(self.root, "init", "-b", "work/mainalto-test", str(self.checkout))
        git(self.checkout, "config", "user.name", "Disposable Test")
        git(self.checkout, "config", "user.email", "disposable@example.invalid")
        (self.checkout / "README.md").write_text("safe test\n", encoding="utf-8")
        git(self.checkout, "add", "README.md")
        git(self.checkout, "commit", "-m", "test commit")
        git(self.checkout, "remote", "add", "origin", str(self.remote))
        self.head = git(self.checkout, "rev-parse", "HEAD")

    def observe(self, mode="Inspect", **kwargs):
        return provider.publish(self.checkout, "example/safe-repo",
                                "work/mainalto-test", self.head, mode=mode,
                                fixture_remote=self.remote, **kwargs)

    def test_inspect_then_single_push_and_repeat_is_idempotent(self):
        inspection = self.observe(allow_create_branch=True)
        self.assertEqual((inspection["status"], inspection["attempted_push"]),
                         ("READY", False))
        sent = self.observe("Publish", allow_create_branch=True)
        self.assertEqual(sent["status"], "VERIFIED")
        self.assertTrue(sent["attempted_push"])
        self.assertEqual(sent["remote_head"], self.head)
        again = self.observe("Publish", allow_create_branch=True)
        self.assertEqual(again["reason_code"], "REMOTE_ALREADY_MATCHED")
        self.assertFalse(again["attempted_push"])
        self.assertEqual(git(self.checkout, "status", "--porcelain"), "")

    def test_missing_remote_branch_not_created_implicitly(self):
        result = self.observe("Publish")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["attempted_push"])
        self.assertEqual(result["reason_code"], "REMOTE_BRANCH_ABSENT")

    def test_dirty_checkout_blocks(self):
        (self.checkout / "new.txt").write_text("uncommitted", encoding="utf-8")
        with self.assertRaisesRegex(provider.Blocked, "UNCOMMITTED_CHANGES"):
            self.observe("Publish", allow_create_branch=True)

    def test_wrong_head_blocks(self):
        with self.assertRaisesRegex(provider.Blocked, "LOCAL_HEAD_MISMATCH"):
            provider.publish(self.checkout, "example/safe-repo", "work/mainalto-test",
                             "0" * 40, fixture_remote=self.remote)

    def test_wrong_branch_blocks(self):
        with self.assertRaisesRegex(provider.Blocked, "LOCAL_BRANCH_MISMATCH"):
            provider.publish(self.checkout, "example/safe-repo", "main", self.head,
                             fixture_remote=self.remote)

    def test_remote_mismatch_blocks(self):
        with self.assertRaisesRegex(provider.Blocked, "REMOTE_MISMATCH"):
            provider.publish(self.checkout, "example/safe-repo", "work/mainalto-test",
                             self.head)

    def test_unavailable_remote_is_unknown_not_proven_absent(self):
        missing = self.root / "unreachable.git"
        git(self.checkout, "remote", "set-url", "origin", str(missing))
        with self.assertRaisesRegex(provider.TransportUncertain,
                                    "REMOTE_INSPECTION_UNAVAILABLE"):
            provider.publish(self.checkout, "example/safe-repo", "work/mainalto-test",
                             self.head, fixture_remote=missing)

    def test_invalid_branch_and_repository_block(self):
        for name in ("../outside", "-force", "refs//main", "a.lock", "a@{b"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(provider.Blocked, "BRANCH_INVALID"):
                    provider.publish(self.checkout, "example/safe-repo", name, self.head)
        with self.assertRaisesRegex(provider.Blocked, "REPOSITORY_INVALID"):
            provider.publish(self.checkout, "https://other.host/repo", "main", self.head)

    def test_cli_reports_one_json_on_stdout_for_blocked(self):
        res = subprocess.run([sys.executable, str(SOURCE), "--checkout",
                              str(self.checkout), "--repository", "example/safe-repo",
                              "--branch", "work/mainalto-test", "--expected-head",
                              self.head], capture_output=True, text=True)
        self.assertEqual(res.returncode, 2)
        self.assertEqual(len(res.stdout.strip().splitlines()), 1)
        data = json.loads(res.stdout)
        self.assertEqual(data["status"], "BLOCKED")
        self.assertEqual(data["reason_code"], "REMOTE_MISMATCH")

    def test_one_shot_failed_push_remains_unknown_without_replay(self):
        # Both local and remote commits exist, but the local commit is not
        # a fast-forward of the divergent remote branch.
        self.observe("Publish", allow_create_branch=True)
        git(self.checkout, "reset", "--hard", self.head)
        (self.checkout / "more.txt").write_text("second", encoding="utf-8")
        git(self.checkout, "add", "more.txt")
        git(self.checkout, "commit", "-m", "remote newer")
        newer = git(self.checkout, "rev-parse", "HEAD")
        git(self.checkout, "push", "origin", "HEAD:refs/heads/work/mainalto-test")
        git(self.checkout, "reset", "--hard", self.head)
        (self.checkout / "different.txt").write_text("different", encoding="utf-8")
        git(self.checkout, "add", "different.txt")
        git(self.checkout, "commit", "-m", "local diverged")
        self.head = git(self.checkout, "rev-parse", "HEAD")
        result = self.observe("Publish")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertTrue(result["attempted_push"])
        self.assertEqual(result["remote_head"], newer)


if __name__ == "__main__":
    unittest.main()
