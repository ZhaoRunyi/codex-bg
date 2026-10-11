from __future__ import annotations

import importlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest


REPO = pathlib.Path(__file__).resolve().parents[1]
SKILL = REPO / "skills" / "codex-bg"
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

bridge = importlib.import_module("jsonl_daemon_bridge")
coordination = importlib.import_module("runtime_coordination")
marker = importlib.import_module("thread_marker")
sync = importlib.import_module("sync_vscode_codex")


class EnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.original = os.environ.copy()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.home = self.root / "codex-home"
        self.home.mkdir()
        os.environ["CODEX_HOME"] = str(self.home)
        os.environ["CODEX_WORKSPACE_ROOT"] = str(self.root)
        os.environ["XDG_RUNTIME_DIR"] = str(self.root / "runtime")
        (self.root / "runtime").mkdir(mode=0o700)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original)
        self.temporary.cleanup()


class BridgeCatalogTests(EnvironmentTest):
    def make_catalog(self, *, minimal: bool = False):
        rollout = self.root / "rollout.jsonl"
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {}}) + "\n")
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        if minimal:
            connection.execute(
                "CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, model_provider TEXT)"
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?)",
                ("thread-1", str(rollout), str(self.root), "openai"),
            )
        else:
            connection.execute(
                "CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, model_provider TEXT, "
                "archived INTEGER, source TEXT, created_at INTEGER, updated_at INTEGER, recency_at INTEGER)"
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, 0, 'vscode', 1, 2, 3)",
                ("thread-1", str(rollout), str(self.root), "openai"),
            )
        connection.commit()
        connection.close()
        return rollout

    def test_schema_variants_and_rollout_lookup(self):
        rollout = self.make_catalog(minimal=True)
        self.assertEqual(bridge.rollout_path_for_thread("thread-1"), str(rollout))
        rows = bridge.synthetic_thread_rows(set())
        self.assertEqual([row["id"] for row in rows], ["thread-1"])

    def test_pagination_cursor_is_preserved_and_duplicates_are_filtered(self):
        self.make_catalog()
        injected = set()
        first = json.dumps(
            {"id": 1, "result": {"data": [], "nextCursor": "daemon-cursor"}}
        )
        augmented = json.loads(
            bridge.augment_thread_list_response(first, {"limit": 10}, injected)
        )
        self.assertEqual(augmented["result"]["nextCursor"], "daemon-cursor")
        self.assertEqual(injected, {"thread-1"})
        later = json.dumps(
            {
                "id": 2,
                "result": {
                    "data": [{"id": "thread-1"}, {"id": "thread-2"}],
                    "nextCursor": "next",
                },
            }
        )
        filtered = json.loads(
            bridge.augment_thread_list_response(later, {"cursor": "daemon-cursor"}, injected)
        )
        self.assertEqual([row["id"] for row in filtered["result"]["data"]], ["thread-2"])
        self.assertEqual(filtered["result"]["nextCursor"], "next")

    def test_resume_key_includes_all_effective_parameters(self):
        first = bridge.canonical_resume_key(
            {"threadId": "t", "path": "/a", "excludeTurns": True}
        )
        same = bridge.canonical_resume_key(
            {"excludeTurns": True, "path": "/a", "threadId": "t"}
        )
        different = bridge.canonical_resume_key(
            {"threadId": "t", "path": "/b", "excludeTurns": True}
        )
        self.assertEqual(first, same)
        self.assertNotEqual(first, different)


class MarkerTests(EnvironmentTest):
    def test_reads_newest_user_message_from_tail(self):
        path = self.root / "rollout.jsonl"
        with path.open("w") as handle:
            for index in range(5000):
                handle.write(json.dumps({"type": "noise", "payload": {"index": index}}) + "\n")
            handle.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "newest"},
                    }
                )
                + "\n"
            )
        self.assertEqual(marker.newest_user_message(path)["message"], "newest")


class CoordinationTests(EnvironmentTest):
    def test_frontend_lease_blocks_update_exclusive_lock(self):
        frontend = coordination.acquire_lock("frontend-clients.lock", exclusive=False)
        try:
            updater = coordination.acquire_lock(
                "frontend-clients.lock", exclusive=True, blocking=False
            )
            self.assertIsNone(updater)
        finally:
            coordination.release_lock(frontend)


class ExtensionPatchTests(EnvironmentTest):
    def test_semantic_patch_is_idempotent_and_backs_up_both_files(self):
        extension = self.root / "openai.chatgpt-1.2.3-linux-x64"
        (extension / "out").mkdir(parents=True)
        binary = extension / "bin" / "linux-x86_64"
        binary.mkdir(parents=True)
        (binary / "codex").write_text("binary")
        (binary / "codex-package.json").write_text("{}")
        package = {
            "contributes": {
                "configuration": {
                    "properties": {"chatgpt.cliExecutable": {"scope": "window"}}
                }
            }
        }
        (extension / "package.json").write_text(json.dumps(package))
        (extension / "out" / "extension.js").write_text(
            'let z=aa.join(Bb(),"ipc");cc.mkdirSync(z,{mode:448,recursive:!0});'
            'let q=rr("cliExecutable");if(q&&q.trim().length>0)return q;let w=Ss(e)'
        )
        original_backups = sync.EXTENSION_BACKUPS
        original_wrapper = sync.WRAPPER
        sync.EXTENSION_BACKUPS = self.root / "backups"
        sync.WRAPPER = pathlib.Path("/opt/codex/shared_codex.sh")
        try:
            self.assertTrue(sync.patch_extension(extension))
            self.assertFalse(sync.patch_extension(extension))
            sync.WRAPPER = pathlib.Path("/srv/codex/shared_codex.sh")
            self.assertTrue(sync.patch_extension(extension))
            self.assertFalse(sync.patch_extension(extension))
        finally:
            sync.EXTENSION_BACKUPS = original_backups
            sync.WRAPPER = original_wrapper
        patched = (extension / "out" / "extension.js").read_text()
        self.assertIn("CODEX_VSCODE_IPC_DIR", patched)
        self.assertIn("/srv/codex/shared_codex.sh", patched)
        backup = self.root / "backups" / "1.2.3-linux-x64"
        self.assertTrue((backup / "extension.js").is_file())
        self.assertTrue((backup / "package.json").is_file())


class ShellContractTests(EnvironmentTest):
    def test_handoff_dry_run_needs_no_live_transport(self):
        result = subprocess.run(
            [
                "bash",
                str(SCRIPTS / "start_handoff.sh"),
                "--session-id",
                "00000000-0000-0000-0000-000000000000",
                "--workdir",
                str(self.root),
                "--dry-run",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("auto-discover-on-launch", result.stdout)


if __name__ == "__main__":
    unittest.main()
