"""Deterministic repo-create tests: no real gh login or external creation."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "tools" / "mainalto_repo_create_provider.py"
spec = importlib.util.spec_from_file_location("mainalto_repo_create_provider", SOURCE)
repo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repo)


class RepoCreateTests(unittest.TestCase):
    def test_inspect_absent_is_only_ready(self):
        with patch.object(repo, "_authenticated_owner", return_value="alex"), \
             patch.object(repo, "_remote_repo", return_value=("ABSENT", None)), \
             patch.object(repo, "_gh") as gh:
            result = repo.observe("alex/new-private-repo")
            self.assertEqual(result["status"], "READY")
            self.assertFalse(result["attempted_create"])
            gh.assert_not_called()

    def test_existing_repository_never_created(self):
        with patch.object(repo, "_authenticated_owner", return_value="alex"), \
             patch.object(repo, "_remote_repo", return_value=("PRESENT", "PRIVATE")), \
             patch.object(repo, "_gh") as gh:
            result = repo.observe("alex/existing", mode="Create",
                                  confirm_repository="alex/existing")
            self.assertEqual(result["status"], "PRESENT")
            gh.assert_not_called()

    def test_one_create_then_exact_readback(self):
        with patch.object(repo, "_authenticated_owner", return_value="alex"), \
             patch.object(repo, "_remote_repo", side_effect=[("ABSENT", None),
                                                              ("PRESENT", "PRIVATE")]), \
             patch.object(repo, "_gh", return_value=subprocess.CompletedProcess([], 0)) as gh:
            result = repo.observe("alex/new-private", mode="Create",
                                  confirm_repository="alex/new-private")
            self.assertEqual(result["status"], "CREATED")
            self.assertTrue(result["attempted_create"])
            gh.assert_called_once_with("repo", "create", "alex/new-private", "--private", timeout=60)

    def test_missing_confirmation_blocks_before_lookup(self):
        with patch.object(repo, "_authenticated_owner", return_value="alex"), \
             patch.object(repo, "_remote_repo") as reader:
            with self.assertRaisesRegex(repo.Blocked, "EXPLICIT_REPOSITORY_CONFIRMATION_REQUIRED"):
                repo.observe("alex/new", mode="Create")
            reader.assert_not_called()

    def test_other_owner_blocked(self):
        with patch.object(repo, "_authenticated_owner", return_value="alex"):
            with self.assertRaisesRegex(repo.Blocked, "ONLY_AUTHENTICATED_PERSONAL_OWNER_ALLOWED"):
                repo.observe("someone-else/new", mode="Create",
                             confirm_repository="someone-else/new")

    def test_create_uncertain_does_not_retry(self):
        with patch.object(repo, "_authenticated_owner", return_value="alex"), \
             patch.object(repo, "_remote_repo", side_effect=[("ABSENT", None),
                                                              repo.Unknown("GH_REMOTE_INSPECTION_FAILED")]), \
             patch.object(repo, "_gh", side_effect=repo.Unknown("GH_UNAVAILABLE_OR_TIMEOUT")) as gh:
            result = repo.observe("alex/maybe", mode="Create", confirm_repository="alex/maybe")
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertTrue(result["attempted_create"])
            gh.assert_called_once()

    def test_public_readback_after_create_not_claimed(self):
        with patch.object(repo, "_authenticated_owner", return_value="alex"), \
             patch.object(repo, "_remote_repo", side_effect=[("ABSENT", None),
                                                              ("PRESENT", "PUBLIC")]), \
             patch.object(repo, "_gh", return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(repo.observe("alex/new", mode="Create",
                                          confirm_repository="alex/new")["status"], "UNKNOWN")

    def test_command_failed_even_if_private_repository_appears(self):
        with patch.object(repo, "_authenticated_owner", return_value="alex"), \
             patch.object(repo, "_remote_repo", side_effect=[("ABSENT", None),
                                                              ("PRESENT", "PRIVATE")]), \
             patch.object(repo, "_gh", return_value=subprocess.CompletedProcess([], 1)) as gh:
            result = repo.observe("alex/maybe-other-actor", mode="Create",
                                  confirm_repository="alex/maybe-other-actor")
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(result["reason_code"], "CREATE_COMMAND_UNCONFIRMED_REMOTE_PRESENT")
            gh.assert_called_once()

    def test_http_readback_requires_matching_identity(self):
        valid = b'HTTP/2.0 200 OK\r\ncontent-type: application/json\r\n\r\n{"full_name":"alex/repo","visibility":"private"}'
        with patch.object(repo, "_gh", return_value=subprocess.CompletedProcess([], 0, stdout=valid, stderr=b"")):
            self.assertEqual(repo._remote_repo("alex/repo"), ("PRESENT", "PRIVATE"))
            with self.assertRaisesRegex(repo.Unknown, "GH_REPOSITORY_ID_MISMATCH"):
                repo._remote_repo("alex/different")

    def test_non_authoritative_404_is_unknown_if_no_http_evidence(self):
        with patch.object(repo, "_gh", return_value=subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"HTTP 404: not found")):
            with self.assertRaisesRegex(repo.Unknown, "GH_HTTP_STATUS_UNPROVEN"):
                repo._remote_repo("alex/missing")


if __name__ == "__main__":
    unittest.main()
