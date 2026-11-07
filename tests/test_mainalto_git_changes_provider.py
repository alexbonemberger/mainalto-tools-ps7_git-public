"""Disposable local repositories only; never contact GitHub."""

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


FILE = Path(__file__).resolve().parents[1] / "tools" / "mainalto_git_changes_provider.py"
spec = importlib.util.spec_from_file_location("changes", FILE)
changes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changes)


def run_git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.DEVNULL).decode().strip()


def item(op, path, old=None, new=None):
    return dict(op=op, path=path,
                expected_sha256=hashlib.sha256(old).hexdigest() if old is not None else None,
                content_base64=base64.b64encode(new).decode() if new is not None else None)


class ChangesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "checkout"
        self.repo.mkdir()
        run_git(self.repo, "init", "-q", "-b", "work/test")
        run_git(self.repo, "config", "user.name", "MainAlto Test")
        run_git(self.repo, "config", "user.email", "mainalto@test.invalid")
        run_git(self.repo, "remote", "add", "origin", "https://github.com/example/public-source.git")
        (self.repo / "keep.txt").write_bytes(b"keep\n")
        (self.repo / "old.txt").write_bytes(b"old\n")
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "base")
        self.base = run_git(self.repo, "rev-parse", "HEAD")
        self.manifest = self.root / "manifest.json"

    def request(self, entries, **kwargs):
        m = dict(schema_version=changes.SCHEMA, operation_id="ps7-to-git-test-001",
                 repository="example/public-source", branch="work/test",
                 base_head=self.base, commit_message="Bounded test changes", changes=entries)
        m.update(kwargs)
        self.manifest.write_text(json.dumps(m), encoding="utf-8")
        return str(self.manifest)

    def test_plan_is_read_only(self):
        path = self.request([item("create", "docs/novo.txt", new=b"ok\n")])
        report = changes.observe(path, str(self.repo), mode="Plan")
        self.assertEqual(report["status"], "READY")
        self.assertEqual(run_git(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertFalse((self.repo / "docs" / "novo.txt").exists())

    def test_one_commit_create_update_delete_and_readback(self):
        path = self.request([item("create", "docs/ação.txt", new="ação\n".encode()),
                             item("update", "keep.txt", old=b"keep\n", new=b"changed\n"),
                             item("delete", "old.txt", old=b"old\n")])
        report = changes.observe(path, str(self.repo), mode="Commit")
        self.assertEqual(report["status"], "COMMITTED")
        self.assertNotEqual(report["new_head"], self.base)
        self.assertEqual((self.repo / "docs/ação.txt").read_text(encoding="utf-8"), "ação\n")
        self.assertEqual((self.repo / "keep.txt").read_text(encoding="utf-8"), "changed\n")
        self.assertFalse((self.repo / "old.txt").exists())
        self.assertEqual(run_git(self.repo, "rev-list", "--count", "HEAD"), "2")
        with self.assertRaisesRegex(changes.Blocked, "BASE_HEAD_MISMATCH"):
            changes.observe(path, str(self.repo), mode="Commit")

    def test_preimage_mismatch(self):
        path = self.request([item("update", "old.txt", old=b"wrong", new=b"new")])
        with self.assertRaisesRegex(changes.Blocked, "PREIMAGE_SHA256_MISMATCH"):
            changes.observe(path, str(self.repo))

    def test_no_op_blocked(self):
        path = self.request([item("update", "old.txt", old=b"old\n", new=b"old\n")])
        with self.assertRaisesRegex(changes.Blocked, "UPDATE_IS_NO_OP"):
            changes.observe(path, str(self.repo))

    def test_branch_and_head_mismatches(self):
        path = self.request([item("create", "a.txt", new=b"x")], branch="work/another")
        with self.assertRaisesRegex(changes.Blocked, "BRANCH_MISMATCH"):
            changes.observe(path, str(self.repo))
        path = self.request([item("create", "a.txt", new=b"x")], base_head="0" * 40)
        with self.assertRaisesRegex(changes.Blocked, "BASE_HEAD_MISMATCH"):
            changes.observe(path, str(self.repo))

    def test_origin_mismatch_and_dirty_checkout(self):
        path = self.request([item("create", "a.txt", new=b"x")], repository="other/another")
        with self.assertRaisesRegex(changes.Blocked, "ORIGIN_REPOSITORY_MISMATCH"):
            changes.observe(path, str(self.repo))
        path = self.request([item("create", "a.txt", new=b"x")])
        (self.repo / "untouched.tmp").write_text("dirty")
        with self.assertRaisesRegex(changes.Blocked, "CHECKOUT_NOT_CLEAN"):
            changes.observe(path, str(self.repo))

    def test_paths_reject_traversal_git_internal_windows_reserved(self):
        for path in ("../bad", "/tmp/bad", ".git/config", "bad\\path", "C:/file", "NUL.txt", "a/*", "doc/../b"):
            with self.subTest(path=path), self.assertRaisesRegex(changes.Blocked, "CHANGE_PATH_INVALID"):
                changes._check_relative_path(path)

    def test_symlink_parent_rejected(self):
        external = self.root / "external"
        external.mkdir()
        try:
            (self.repo / "link").symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation prohibited in this test environment")
        path = self.request([item("create", "link/outside.txt", new=b"x")])
        with self.assertRaises(changes.Blocked):
            changes.observe(path, str(self.repo))
        self.assertFalse((external / "outside.txt").exists())

    def test_duplicate_casefold_and_duplicate_json_keys(self):
        path = self.request([item("create", "A.txt", new=b"a"), item("create", "a.txt", new=b"b")])
        with self.assertRaisesRegex(changes.Blocked, "CHANGE_PATH_COLLISION"):
            changes.observe(path, str(self.repo))
        self.manifest.write_text('{"schema_version":"a","schema_version":"b"}', encoding="utf-8")
        with self.assertRaisesRegex(changes.Blocked, "DUPLICATE_JSON_KEY"):
            changes.observe(str(self.manifest), str(self.repo))

    def test_size_and_missing_content_and_empty_change_set(self):
        path = self.request([item("create", "big.bin", new=b"x" * (1024 * 1024 + 1))])
        with self.assertRaisesRegex(changes.Blocked, "CHANGESET_CONTENT_TOO_LARGE"):
            changes.observe(path, str(self.repo))
        path = self.request([])
        with self.assertRaisesRegex(changes.Blocked, "CHANGES_COUNT_INVALID"):
            changes.observe(path, str(self.repo))

    def test_never_pushes(self):
        path = self.request([item("create", "new.txt", new=b"x")])
        changes.observe(path, str(self.repo), mode="Commit")
        # No remote exists; even a hidden fetch/push would fail this test.
        self.assertEqual(run_git(self.repo, "rev-parse", "HEAD^"), self.base)
        self.assertEqual(run_git(self.repo, "remote", "-v").splitlines()[0].split()[1],
                         "https://github.com/example/public-source.git")

    def test_cli_exactly_one_json_and_blocked_exit(self):
        path = self.request([item("create", "new.txt", new=b"x")])
        cmd = [sys.executable, str(FILE), "--manifest", path, "--checkout", str(self.repo), "--mode", "Plan"]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(proc.stdout.splitlines()), 1)
        self.assertEqual(json.loads(proc.stdout)["status"], "READY")
        proc = subprocess.run(cmd[:-1] + ["Wrong"], capture_output=True, text=True, check=False)
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
