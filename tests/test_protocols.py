from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest

import websockets
from websockets.exceptions import ConnectionClosed


REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "codex-bg" / "scripts"


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.socket = self.root / "daemon.sock"
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.home = self.root / "home"
        (self.home / "sessions").mkdir(parents=True)
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "CODEX_HOME": str(self.home),
                "CODEX_APP_SERVER_SOCKET": str(self.socket),
                "XDG_RUNTIME_DIR": str(self.runtime),
                "PYTHONUNBUFFERED": "1",
            }
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def start_bridge(self, timeout="2"):
        environment = self.environment | {"CODEX_BRIDGE_RESUME_TIMEOUT": timeout}
        return await asyncio.create_subprocess_exec(
            sys.executable,
            str(SCRIPTS / "jsonl_daemon_bridge.py"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )

    async def stop_process(self, process):
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.wait(), 3)

    async def test_bridge_coalesces_only_identical_resume_requests(self):
        received = []

        async def handler(websocket):
            try:
                async for raw in websocket:
                    request = json.loads(raw)
                    received.append(request)
                    await asyncio.sleep(0.05)
                    await websocket.send(
                        json.dumps({"id": request["id"], "result": {"thread": {"id": "ok"}}})
                    )
            except ConnectionClosed:
                pass

        async with websockets.unix_serve(handler, str(self.socket)):
            process = await self.start_bridge()
            try:
                requests = [
                    {"id": 1, "method": "thread/resume", "params": {"threadId": "t", "path": "/a"}},
                    {"id": 2, "method": "thread/resume", "params": {"path": "/a", "threadId": "t"}},
                    {"id": 3, "method": "thread/resume", "params": {"threadId": "t", "path": "/b"}},
                ]
                for request in requests:
                    process.stdin.write((json.dumps(request) + "\n").encode())
                await process.stdin.drain()
                responses = [
                    json.loads(await asyncio.wait_for(process.stdout.readline(), 3))
                    for _ in requests
                ]
                self.assertEqual({row["id"] for row in responses}, {1, 2, 3})
                self.assertEqual(len(received), 2)
                self.assertEqual({row["params"]["path"] for row in received}, {"/a", "/b"})
                self.assertTrue(all(row["params"]["excludeTurns"] for row in received))
            finally:
                await self.stop_process(process)

    async def test_bridge_expires_primary_and_waiters(self):
        async def handler(websocket):
            try:
                async for _ in websocket:
                    pass
            except ConnectionClosed:
                pass

        async with websockets.unix_serve(handler, str(self.socket)):
            process = await self.start_bridge("0.1")
            try:
                for request_id in (10, 11):
                    request = {
                        "id": request_id,
                        "method": "thread/resume",
                        "params": {"threadId": "t", "path": "/a"},
                    }
                    process.stdin.write((json.dumps(request) + "\n").encode())
                await process.stdin.drain()
                responses = [
                    json.loads(await asyncio.wait_for(process.stdout.readline(), 2))
                    for _ in range(2)
                ]
                self.assertEqual({row["id"] for row in responses}, {10, 11})
                self.assertTrue(all("timed out" in row["error"]["message"] for row in responses))
            finally:
                await self.stop_process(process)

    def prepare_thread(self, connection, session_id):
        rollout = self.home / "sessions" / f"rollout-{session_id}.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": session_id},
                }
            )
            + "\n"
        )
        connection.execute("INSERT INTO threads VALUES (?, ?)", (session_id, str(rollout)))
        return rollout

    async def test_wake_materialization_is_per_thread(self):
        first = "00000000-0000-0000-0000-000000000001"
        second = "00000000-0000-0000-0000-000000000002"
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT)")
        self.prepare_thread(connection, first)
        self.prepare_thread(connection, second)
        connection.commit()
        connection.close()

        guard = self.root / "guard.py"
        guard.write_text("raise SystemExit(0)\n")
        markers = {}
        for session_id in (first, second):
            marker_process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(SCRIPTS / "thread_marker.py"),
                "--session-id",
                session_id,
                "--codex-home",
                str(self.home),
                stdout=asyncio.subprocess.PIPE,
            )
            output, _ = await marker_process.communicate()
            marker_path = self.root / f"{session_id}.marker.json"
            marker_path.write_bytes(output)
            markers[session_id] = marker_path

        loaded = set()
        active_resumes = 0
        max_active_resumes = 0
        resume_counts = {}

        async def handler(websocket):
            nonlocal active_resumes, max_active_resumes
            async for raw in websocket:
                request = json.loads(raw)
                if "id" not in request:
                    continue
                method = request["method"]
                if method == "initialize":
                    result = {}
                elif method == "thread/loaded/list":
                    result = {"data": sorted(loaded)}
                elif method == "thread/resume":
                    session_id = request["params"]["threadId"]
                    self.assertTrue(request["params"]["excludeTurns"])
                    active_resumes += 1
                    max_active_resumes = max(max_active_resumes, active_resumes)
                    resume_counts[session_id] = resume_counts.get(session_id, 0) + 1
                    await asyncio.sleep(0.2)
                    loaded.add(session_id)
                    active_resumes -= 1
                    result = {"thread": {"id": session_id}}
                elif method == "thread/turns/list":
                    result = {"data": []}
                else:
                    raise AssertionError(method)
                await websocket.send(json.dumps({"id": request["id"], "result": result}))

        async def run_wake(session_id):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(SCRIPTS / "wake_thread.py"),
                "--session-id",
                session_id,
                "--prompt",
                "continue",
                "--marker",
                str(markers[session_id]),
                "--probe-only",
                "--codex-home",
                str(self.home),
                "--config-guard",
                str(guard),
                "--socket",
                str(self.socket),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.environment,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 0, stderr.decode())
            return json.loads(stdout)

        async with websockets.unix_serve(handler, str(self.socket)):
            await asyncio.gather(run_wake(first), run_wake(second))
            self.assertEqual(max_active_resumes, 2)
            loaded.clear()
            resume_counts.clear()
            max_active_resumes = 0
            await asyncio.gather(run_wake(first), run_wake(first))
            self.assertEqual(resume_counts[first], 1)
            self.assertEqual(max_active_resumes, 1)

    async def test_wait_active_polls_even_without_completion_notification(self):
        session_id = "00000000-0000-0000-0000-000000000003"
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT)")
        self.prepare_thread(connection, session_id)
        connection.commit()
        connection.close()
        guard = self.root / "guard.py"
        guard.write_text("raise SystemExit(0)\n")
        marker_process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(SCRIPTS / "thread_marker.py"),
            "--session-id",
            session_id,
            "--codex-home",
            str(self.home),
            stdout=asyncio.subprocess.PIPE,
        )
        marker_output, _ = await marker_process.communicate()
        marker_path = self.root / "wait.marker.json"
        marker_path.write_bytes(marker_output)
        turn_reads = 0
        started = False

        async def handler(websocket):
            nonlocal turn_reads, started
            async for raw in websocket:
                request = json.loads(raw)
                if "id" not in request:
                    continue
                method = request["method"]
                if method == "initialize":
                    result = {}
                elif method == "thread/loaded/list":
                    result = {"data": [session_id]}
                elif method == "thread/turns/list":
                    turn_reads += 1
                    status = "inProgress" if turn_reads < 3 else "completed"
                    result = {"data": [{"id": "old-turn", "status": status}]}
                elif method == "turn/start":
                    started = True
                    result = {"turn": {"id": "new-turn"}}
                else:
                    raise AssertionError(method)
                await websocket.send(json.dumps({"id": request["id"], "result": result}))

        async with websockets.unix_serve(handler, str(self.socket)):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(SCRIPTS / "wake_thread.py"),
                "--session-id",
                session_id,
                "--prompt",
                "continue",
                "--marker",
                str(marker_path),
                "--wait-active",
                "--codex-home",
                str(self.home),
                "--config-guard",
                str(guard),
                "--socket",
                str(self.socket),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.environment,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), 6)
        self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertEqual(json.loads(stdout)["action"], "started")
        self.assertTrue(started)
        self.assertGreaterEqual(turn_reads, 3)


if __name__ == "__main__":
    unittest.main()
