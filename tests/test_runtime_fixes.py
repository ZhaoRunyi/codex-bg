from __future__ import annotations

import asyncio
import contextlib
import importlib
import io
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import websockets

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills/codex-bg/scripts"
sys.path.insert(0, str(SCRIPTS))
sync = importlib.import_module("sync_vscode_codex")
coordination = importlib.import_module("runtime_coordination")


class RuntimeFixTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_missing_daemon_is_reported_without_lock_name_collision(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "frontend_scope.py"), "--session-id",
             "00000000-0000-0000-0000-000000000000", "--socket", str(self.root / "missing")],
            env=os.environ | {"CODEX_HOME": str(self.root / "home")},
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(), "shared-daemon-missing")
        self.assertNotIn("TypeError", result.stderr)

    def test_custom_extension_root_and_explicit_override(self):
        home = self.root / "home"
        custom = home / "vscode-server/.vscode-server/extensions"
        custom.mkdir(parents=True)
        command = [sys.executable, "-c",
                   "import sys; sys.path.insert(0, sys.argv[1]); "
                   "import sync_vscode_codex as sync; print(sync.EXTENSION_ROOT)", str(SCRIPTS)]
        env = os.environ | {"CODEX_HOME": str(home)}
        env.pop("CODEX_VSCODE_EXTENSION_ROOT", None)
        first = subprocess.check_output(command, env=env, text=True, timeout=5).strip()
        self.assertEqual(first, str(custom))
        env["CODEX_VSCODE_EXTENSION_ROOT"] = str(self.root / "override")
        overridden = subprocess.check_output(command, env=env, text=True, timeout=5).strip()
        self.assertEqual(overridden, env["CODEX_VSCODE_EXTENSION_ROOT"])

    def test_bootstrap_selects_python_311_and_workspace_uv(self):
        home = self.root / "workspace/.codex"
        home.mkdir(parents=True)
        uvroot = home.parent / ".uv"
        (uvroot / "bin").mkdir(parents=True)
        log = self.root / "uv-calls"
        uv = uvroot / "bin/uv"
        uv.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$UV_CALL_LOG"\n')
        uv.chmod(0o755)
        (uvroot / "env.sh").write_text(f'export PATH="{uvroot}/bin:$PATH"\n')
        env = os.environ | {"CODEX_HOME": str(home), "UV_CALL_LOG": str(log)}
        env.pop("CODEX_BG_BOOTSTRAP_PYTHON", None)
        subprocess.run(["bash", str(SCRIPTS / "bootstrap_runtime.sh")], env=env,
                       capture_output=True, text=True, timeout=5, check=True)
        self.assertIn("venv --python 3.11", log.read_text())
        env["CODEX_BG_BOOTSTRAP_PYTHON"] = "3.13"
        subprocess.run(["bash", str(SCRIPTS / "bootstrap_runtime.sh")], env=env,
                       capture_output=True, text=True, timeout=5, check=True)
        self.assertIn("venv --python 3.13", log.read_text())

    def test_first_install_seeds_package_before_update(self):
        stage = self.root / "stage"
        (stage / "bin").mkdir(parents=True)
        managed = self.root / "managed/codex"
        compat = self.root / "compat.sock"
        compat.symlink_to(self.root / "not-yet-bound.sock")
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[-1] == "start":
                self.assertFalse(compat.is_symlink())
                managed.parent.mkdir()
                managed.write_text("installed")
            return types.SimpleNamespace(returncode=0, stdout="")

        with patch.object(sync, "stage_extension_package", return_value=stage), \
             patch.object(sync, "MANAGED_CLI", managed), \
             patch.object(sync, "COMPAT_SOCKET", compat), \
             patch.object(sync, "cli_version", return_value="new"), \
             patch.object(sync.subprocess, "run", side_effect=run), \
             contextlib.redirect_stdout(io.StringIO()):
            sync.install_extension_package(self.root / "extension", "new")
        self.assertEqual([command[-1] for command in calls], ["start", "stop"])
        self.assertFalse(stage.exists())

    def test_handoff_accepts_current_version_json_without_backend(self):
        fake_cli = self.root / "codex"
        fake_cli.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"status":"running"}\'\n')
        fake_cli.chmod(0o755)
        source = (SCRIPTS / "start_handoff.sh").read_text()
        function = source.split("wait_for_managed_daemon() {", 1)[1].split(
            "\nfind_transport_pid()", 1)[0]
        socket_path = self.root / "daemon.sock"
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(socket_path))
            command = "wait_for_managed_daemon() {" + function + "\nwait_for_managed_daemon\n"
            env = os.environ | {"codex_binary": str(fake_cli), "daemon_socket": str(socket_path)}
            result = subprocess.run(["bash", "-c", command], env=env,
                                    capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_first_install_refuses_wrong_version_and_failed_stop(self):
        for version, stop_code in [("wrong", 0), ("new", 1)]:
            with self.subTest(version=version, stop_code=stop_code):
                stage = self.root / f"stage-{version}"
                (stage / "bin").mkdir(parents=True)
                managed = self.root / "missing-managed"
                with patch.object(sync, "stage_extension_package", return_value=stage), \
                     patch.object(sync, "MANAGED_CLI", managed), \
                     patch.object(sync, "COMPAT_SOCKET", self.root / "absent"), \
                     patch.object(sync, "cli_version", return_value=version), \
                     patch.object(sync.subprocess, "run", side_effect=[
                         types.SimpleNamespace(returncode=0, stdout=""),
                         types.SimpleNamespace(returncode=stop_code, stdout="failed")]):
                    with self.assertRaises(RuntimeError):
                        sync.install_extension_package(self.root / "extension", "new")
                self.assertTrue(stage.exists())

    def test_config_and_upgrade_changes_wait_for_leases_and_active_turns(self):
        extension = self.root / "extension"
        binary = extension / "bin"
        binary.mkdir(parents=True)
        socket_path = self.root / "daemon.sock"
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(socket_path))
            for config_changed in [False, True]:
                with self.subTest(config_changed=config_changed), \
                     patch.object(sync, "installed_extensions", return_value=[extension]), \
                     patch.object(sync, "patch_extension"), \
                     patch.object(sync, "extension_binary_dir", return_value=binary), \
                     patch.object(sync, "cli_version", side_effect=lambda cli:
                                  "new" if cli == binary / "codex" or config_changed else "old"), \
                     patch.object(sync, "ensure_runtime", return_value=(socket_path, self.root / "pid")), \
                     patch.object(sync, "daemon_config_current", return_value=not config_changed), \
                     patch.object(sync, "verified_pid", return_value=123), \
                     patch.object(sync, "write_pid"), \
                     patch.object(sync, "live_threads", return_value=[]), \
                     patch.object(sync, "stop_daemon") as stop, \
                     patch.object(sync, "install_extension_package") as install, \
                     patch.object(sync, "record_guard") as record, \
                     contextlib.redirect_stdout(io.StringIO()):
                    for name in ["frontend-clients.lock", "daemon-operations.lock"]:
                        lease = coordination.acquire_lock(name, exclusive=False)
                        try:
                            with patch.object(sync, "daemon_is_idle", return_value=(True, [])):
                                result = sync.sync_once(True)
                            self.assertTrue(result["deferred"])
                            self.assertFalse(result["restarted"])
                        finally:
                            coordination.release_lock(lease)
                    with patch.object(sync, "daemon_is_idle", return_value=(False, [{"status": "active"}])):
                        self.assertTrue(sync.sync_once(True)["deferred"])
                    stop.assert_not_called()
                    install.assert_not_called()
                    record.assert_not_called()

    def test_idle_config_change_restarts_without_reinstalling_package(self):
        extension = self.root / "extension"
        socket_path = self.root / "daemon.sock"
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(socket_path))
            with patch.object(sync, "installed_extensions", return_value=[extension]), \
                 patch.object(sync, "patch_extension"), \
                 patch.object(sync, "extension_binary_dir", return_value=self.root), \
                 patch.object(sync, "cli_version", return_value="same"), \
                 patch.object(sync, "ensure_runtime", return_value=(socket_path, self.root / "pid")), \
                 patch.object(sync, "daemon_config_current", return_value=False), \
                 patch.object(sync, "daemon_is_idle", return_value=(True, [])), \
                 patch.object(sync, "verified_pid", return_value=123), \
                 patch.object(sync, "write_pid"), \
                 patch.object(sync, "live_threads", return_value=[]), \
                 patch.object(sync, "stop_daemon") as stop, \
                 patch.object(sync, "start_daemon") as start, \
                 patch.object(sync, "install_extension_package") as install, \
                 patch.object(sync, "record_guard") as record:
                result = sync.sync_once(True)
            self.assertTrue(result["restarted"])
            self.assertFalse(result["updated"])
            self.assertFalse(result["deferred"])
            stop.assert_called_once()
            start.assert_called_once()
            install.assert_not_called()
            record.assert_called_once()


class FrontendAliasTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_alias_is_recognized_by_ownership_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            home = root / "home"
            control = home / "app-server-control"
            control.mkdir(parents=True)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            socket_path = root / "daemon.sock"
            tid = "00000000-0000-0000-0000-000000000001"
            alias = root / "scripts-alias"
            alias.symlink_to(SCRIPTS, target_is_directory=True)
            guard = root / "guard.py"
            guard.write_text("raise SystemExit(0)\n")
            env = os.environ | {"CODEX_HOME": str(home), "XDG_RUNTIME_DIR": str(runtime),
                                 "CODEX_APP_SERVER_SOCKET": str(socket_path)}

            async def handler(ws):
                async for raw in ws:
                    request = json.loads(raw)
                    if "id" not in request:
                        continue
                    result = {"data": [tid]} if request["method"] == "thread/loaded/list" else {}
                    await ws.send(json.dumps({"id": request["id"], "result": result}))

            async with websockets.unix_serve(handler, str(socket_path)):
                bridge = await asyncio.create_subprocess_exec(
                    sys.executable, str(alias / "jsonl_daemon_bridge.py"), env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    bridge.stdin.write(b'{"id":1,"method":"initialize","params":{}}\n')
                    await bridge.stdin.drain()
                    await asyncio.wait_for(bridge.stdout.readline(), 5)
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, str(SCRIPTS / "frontend_scope.py"), "--session-id", tid,
                        "--socket", str(socket_path), "--config-guard", str(guard),
                        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    out, err = await asyncio.wait_for(process.communicate(), 5)
                    # Unknown unrelated bundled owners remain a deliberate fail-closed result.
                    if b"bundled-owner-unknown" in out:
                        self.skipTest("live bundled sidebar on host has no test ownership logs")
                    self.assertEqual(process.returncode, 0, err.decode() + out.decode())
                    self.assertEqual(out.strip(), b"ready")
                finally:
                    bridge.stdin.close()
                    await asyncio.wait_for(bridge.wait(), 5)


if __name__ == "__main__":
    unittest.main()
