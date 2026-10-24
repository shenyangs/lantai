"""Real Git integration tests: two isolated writers and a bare sync channel."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("cloud_sync", ROOT / "scripts/cloud_sync.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class CloudSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.remote = self.base / "remote.git"
        self.call(self.base, "init", "--bare", str(self.remote))
        self.local = self.base / "local"
        self.call(self.base, "init", "-b", "main", str(self.local))
        self.identity(self.local)
        (self.local / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/worktree_guard.py",
                        self.local / "scripts/worktree_guard.py")
        self.commit(self.local, "base.txt", "base\n")
        self.call(self.local, "remote", "add", "origin", str(self.remote))
        self.call(self.local, "push", "origin", "main", "main:codex/cloud-sync")
        self.call(self.local, "switch", "-c", "codex/cloud-sync")
        self.cloud = self.base / "cloud"
        self.call(self.base, "clone", "-b", "codex/cloud-sync", str(self.remote), str(self.cloud))
        self.identity(self.cloud)
        self.state = self.base / "state"
        self.checks = [[sys.executable, "-c", "raise SystemExit(0)"]]

    def call(self, repo, *args):
        result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
        if result.returncode:
            self.fail(result.stderr)
        return result.stdout.strip()

    def identity(self, repo):
        self.call(repo, "config", "user.name", "Sync test")
        self.call(repo, "config", "user.email", "sync@example.invalid")

    def commit(self, repo, filename, body):
        path = repo / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        self.call(repo, "add", "--", filename, "scripts/worktree_guard.py")
        self.call(repo, "commit", "-m", filename)
        return sync.head(repo)

    def publish(self, repo):
        return sync.publish(repo, self.state, self.checks)

    def test_two_way_round_trip(self):
        first = self.commit(self.local, "from-local.txt", "local checkpoint\n")
        self.assertEqual(self.publish(self.local)["status"], "published")
        self.assertEqual(sync.pull(self.cloud)["head"], first)
        second = self.commit(self.cloud, "from-cloud.txt", "cloud checkpoint\n")
        self.assertEqual(self.publish(self.cloud)["status"], "published")
        self.assertEqual(sync.pull(self.local)["head"], second)
        self.assertEqual((self.local / "from-cloud.txt").read_text(), "cloud checkpoint\n")

    def test_dirty_edit_preserved_without_fetch(self):
        before = sync.head(self.local)
        self.commit(self.cloud, "base.txt", "cloud changed\n")
        self.publish(self.cloud)
        (self.local / "base.txt").write_text("unsaved task work\n")
        with self.assertRaisesRegex(sync.SyncBlocked, "uncommitted"):
            sync.pull(self.local)
        self.assertEqual(sync.head(self.local), before)
        self.assertEqual((self.local / "base.txt").read_text(), "unsaved task work\n")

    def test_untracked_file_preserved(self):
        (self.local / "mine.txt").write_text("do not stage me\n")
        with self.assertRaises(sync.SyncBlocked):
            self.publish(self.local)
        self.assertTrue((self.local / "mine.txt").exists())

    def test_expired_lease_is_not_taken_over(self):
        path = sync.git_path(self.local, "lantai-worktree-lease.json")
        content = '{"owner":"another-task","expires_at":1}'
        path.write_text(content)
        with self.assertRaisesRegex(sync.SyncBlocked, "lease"):
            sync.pull(self.local)
        self.assertEqual(path.read_text(), content)

    def test_git_lock_is_not_deleted(self):
        path = sync.git_path(self.local, "index.lock")
        path.write_text("another git operation")
        with self.assertRaisesRegex(sync.SyncBlocked, "transaction"):
            sync.pull(self.local)
        self.assertTrue(path.exists())

    def test_divergence_saves_candidate_and_keeps_channel(self):
        local_sha = self.commit(self.local, "base.txt", "local version\n")
        cloud_sha = self.commit(self.cloud, "base.txt", "cloud version\n")
        self.publish(self.cloud)
        result = self.publish(self.local)
        self.assertEqual(result["status"], "diverged_saved")
        self.assertEqual(self.call(self.local, "ls-remote", "origin", "refs/heads/codex/cloud-sync").split()[0], cloud_sha)
        self.assertEqual(self.call(self.local, "ls-remote", "origin", "refs/heads/" + result["branch"]).split()[0], local_sha)
        with self.assertRaisesRegex(sync.SyncBlocked, "diverged"):
            sync.pull(self.local)
        self.assertEqual(sync.head(self.local), local_sha)

    def test_failed_validation_does_not_upload(self):
        self.commit(self.local, "new.txt", "not validated\n")
        before = self.call(self.local, "ls-remote", "origin", "refs/heads/codex/cloud-sync")
        with self.assertRaisesRegex(sync.SyncBlocked, "validation 1 failed"):
            sync.publish(self.local, self.state, [[sys.executable, "-c", "raise SystemExit(4)"]])
        self.assertEqual(self.call(self.local, "ls-remote", "origin", "refs/heads/codex/cloud-sync"), before)
        self.assertFalse(sync.git_path(self.local, "lantai-worktree-lease.json").exists())

    def test_competing_push_during_validation_preserves_both_sides(self):
        local_sha = self.commit(self.local, "local.txt", "local checkpoint\n")
        cloud_sha = self.commit(self.cloud, "cloud.txt", "competing checkpoint\n")
        actual_validation = sync.validation

        def concurrent_validation(repo, state_dir, checks):
            receipt = actual_validation(repo, state_dir, checks)
            self.call(self.cloud, "push", "origin", "HEAD:codex/cloud-sync")
            return receipt

        with mock.patch.object(sync, "validation", side_effect=concurrent_validation):
            with self.assertRaisesRegex(sync.SyncBlocked, "git push failed"):
                self.publish(self.local)
        self.assertEqual(sync.head(self.local), local_sha)
        self.assertEqual(self.call(self.local, "ls-remote", "origin", "refs/heads/codex/cloud-sync").split()[0], cloud_sha)
        self.assertFalse(sync.git_path(self.local, "lantai-worktree-lease.json").exists())

    def test_validation_mutation_keeps_lease_and_does_not_upload(self):
        self.commit(self.local, "new.txt", "checkpoint\n")
        before = self.call(self.local, "ls-remote", "origin", "refs/heads/codex/cloud-sync")
        command = [sys.executable, "-c", "from pathlib import Path; Path('new.txt').write_text('changed')"]
        with self.assertRaisesRegex(sync.SyncBlocked, "lease retained"):
            sync.publish(self.local, self.state, [command])
        self.assertEqual(self.call(self.local, "ls-remote", "origin", "refs/heads/codex/cloud-sync"), before)
        self.assertTrue(sync.git_path(self.local, "lantai-worktree-lease.json").exists())

    def test_validation_output_not_recorded(self):
        self.commit(self.local, "new.txt", "checkpoint\n")
        sync.publish(self.local, self.state, [[sys.executable, "-c", "print('secret-test-output')"]])
        for path in self.state.rglob("*.json"):
            self.assertNotIn("secret-test-output", path.read_text())

    def test_frozen_website_rejected_in_both_directions(self):
        self.commit(self.cloud, "website/frozen.html", "must not arrive\n")
        with self.assertRaisesRegex(sync.SyncBlocked, "protected"):
            self.publish(self.cloud)
        self.call(self.cloud, "push", "origin", "HEAD:codex/cloud-sync")
        with self.assertRaisesRegex(sync.SyncBlocked, "protected"):
            sync.pull(self.local)
        self.assertFalse((self.local / "website").exists())

    def test_credentials_path_rejected(self):
        self.commit(self.local, "working.secret/providers/test.json", "encrypted or plaintext\n")
        with self.assertRaisesRegex(sync.SyncBlocked, "protected"):
            self.publish(self.local)

    def test_generated_assets_rejected(self):
        self.commit(self.local, "console/dist/index.html", "generated build\n")
        with self.assertRaisesRegex(sync.SyncBlocked, "protected"):
            self.publish(self.local)

    def test_main_checkout_not_updated(self):
        self.call(self.local, "switch", "main")
        with self.assertRaisesRegex(sync.SyncBlocked, "coordinator"):
            sync.pull(self.local)

    def test_daemon_follows_integrated_main(self):
        self.call(self.cloud, "switch", "-c", "main", "origin/main")
        sha = self.commit(self.cloud, "integrated.txt", "coordinator commit\n")
        self.call(self.cloud, "push", "origin", "main")
        result = sync.once({"repo": str(self.local), "state_dir": str(self.state)})
        self.assertEqual(result["status"], "updated")
        self.assertEqual(sync.head(self.local), sha)

    def test_daemon_does_not_upload_unvalidated_local_commit(self):
        sha = self.commit(self.local, "local-pending.txt", "still needs tests\n")
        before = self.call(self.local, "ls-remote", "origin", "refs/heads/codex/cloud-sync")
        result = sync.once({"repo": str(self.local), "state_dir": str(self.state)})
        self.assertEqual(result["status"], "local_ahead")
        self.assertEqual(sync.head(self.local), sha)
        self.assertEqual(self.call(self.local, "ls-remote", "origin", "refs/heads/codex/cloud-sync"), before)

    def test_daemon_observes_shared_source_without_writing(self):
        (self.cloud / "local-only.txt").write_text("preserve shared source\n")
        result = sync.once({"repo": str(self.local), "state_dir": str(self.state),
                            "observe_sources": [str(self.cloud)]})
        self.assertEqual(result["sources"][0]["status"], "preserved")
        self.assertTrue((self.cloud / "local-only.txt").exists())

    def test_never_push_to_main(self):
        with self.assertRaisesRegex(sync.SyncBlocked, "dedicated"):
            sync.publish(self.local, self.state, self.checks, channel="main")


if __name__ == "__main__":
    unittest.main()
