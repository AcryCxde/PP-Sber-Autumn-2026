"""Missing gateway-side receiver for the EXISTING runner v1 WebSocket protocol.

Mount create_runner_router(bridge) in gateway; call bridge.start()/stop() from
its lifespan. The standalone app is a local contract-test/integration candidate.
It does not replace legacy /ws, provision runners, or store artifact bytes.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect

from backend.runner_transport import RunnerTransport, TransportError


class RunnerBridge:
    def __init__(self, transport: RunnerTransport, project_tokens: dict[str, str] | None = None,
                 *, poll_seconds: float = 0.1, heartbeat_timeout: float = 30):
        self.transport = transport
        self._tokens: dict[str, str] = {}
        self._connections: set[str] = set()
        self._monitor: asyncio.Task[None] | None = None
        self.poll_seconds = poll_seconds
        self.heartbeat_timeout = heartbeat_timeout
        for project, token in (project_tokens or {}).items():
            self.register(project, token)

    def register(self, project: str, token: str) -> None:
        """Project-scoped token, added at runtime when the gateway provisions a runner."""
        if not project or not token:
            raise ValueError("Explicit non-empty project-scoped tokens required")
        if any(p != project and hmac.compare_digest(t, token) for p, t in self._tokens.items()):
            raise ValueError("Each project must have a distinct token")
        self._tokens[project] = token

    def revoke(self, project: str) -> None:
        self._tokens.pop(project, None)

    def is_connected(self, project: str) -> bool:
        return project in self._connections

    async def start(self) -> None:
        if self._monitor is None:
            self._monitor = asyncio.create_task(self._watch())

    async def stop(self) -> None:
        if self._monitor:
            self._monitor.cancel()
            await asyncio.gather(self._monitor, return_exceptions=True)
            self._monitor = None

    async def _watch(self) -> None:
        while True:
            self.transport.expire(timeout=self.heartbeat_timeout)
            await asyncio.sleep(1)

    async def handle(self, websocket: WebSocket) -> None:
        # Header checked before accepting; hello binds this credential to ONE project.
        header = websocket.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        authorized = [p for p, expected in self._tokens.items()
                      if hmac.compare_digest(token.encode(), expected.encode())]
        if len(authorized) != 1:
            await websocket.close(code=1008)
            return
        project = authorized[0]
        await websocket.accept()
        registered = False
        tasks: list[asyncio.Task[Any]] = []
        try:
            async with asyncio.timeout(10):
                hello = await websocket.receive_json()
            if (not isinstance(hello, dict) or type(hello.get("v")) is not int or hello["v"] != 1
                    or hello.get("type") != "hello" or hello.get("project_id") != project
                    or type(hello.get("last_seq")) is not int or hello["last_seq"] < 0
                    or not isinstance(hello.get("runner_version"), str)
                    or hello.get("session_id") is not None and not isinstance(hello["session_id"], str)):
                raise TransportError("invalid_hello_or_project")
            if project in self._connections:
                raise TransportError("project_already_connected")
            self._connections.add(project)
            registered = True
            acked = self.transport.hello(project)
            if hello["last_seq"] < acked:
                raise TransportError("journal_regressed")
            await websocket.send_json({"v": 1, "type": "welcome", "acked_seq": acked})
            tasks = [asyncio.create_task(self._receive(websocket, project)),
                     asyncio.create_task(self._commands(websocket, project))]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except WebSocketDisconnect:
            pass  # runner closed the connection itself (container stopped or restarting)
        except (TransportError, ValueError, TimeoutError):
            # Do not log frame bodies, prompts, tokens, SDK session identifiers.
            try:
                await websocket.close(code=1008)
            except Exception:  # peer already gone: uvicorn raises ClientDisconnected
                pass
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if registered:
                self._connections.discard(project)

    async def _receive(self, websocket: WebSocket, project: str) -> None:
        while True:
            frame = await websocket.receive_json()
            if not isinstance(frame, dict) or type(frame.get("v")) is not int or frame["v"] != 1:
                raise TransportError("invalid_frame")
            kind = frame.get("type")
            if kind == "event":
                try:
                    self.transport.ingest(project, frame.get("journal_seq"), frame.get("event"))
                except TransportError as error:
                    # Run already closed by the gateway (heartbeat_lost, timeout): drop the
                    # late row, otherwise every reconnect replays it and the project is stuck.
                    if str(error) != "terminal_run":
                        raise
                    self.transport.skip(project, frame.get("journal_seq"))
            elif kind == "heartbeat":
                self.transport.heartbeat(project, frame)
            elif kind == "command_ack":
                self.transport.ack(project, frame)
            else:
                raise TransportError("unexpected_frame")

    async def _commands(self, websocket: WebSocket, project: str) -> None:
        sent = set()
        while True:
            for command in self.transport.commands(project):
                if command["id"] not in sent:
                    await websocket.send_json(command)
                    sent.add(command["id"])
            await asyncio.sleep(self.poll_seconds)


def create_runner_router(bridge: RunnerBridge, *, path: str = "/v1/runner") -> APIRouter:
    router = APIRouter()
    router.add_api_websocket_route(path, bridge.handle)
    return router


def create_runner_app(transport: RunnerTransport,
                      project_tokens: dict[str, str]) -> FastAPI:
    bridge = RunnerBridge(transport, project_tokens)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await bridge.start()
        try:
            yield
        finally:
            await bridge.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.runner_bridge = bridge
    app.include_router(create_runner_router(bridge))
    return app


def app_from_env() -> FastAPI:
    """uvicorn backend.runner_app:app_from_env --factory --host 127.0.0.1.

    No default token, no HTTP command/receipt endpoint, no implicit model calls.
    Token JSON file must be protected by the deployment's filesystem permissions.
    """
    with open(os.environ["RUNNER_PROJECT_TOKENS_FILE"], encoding="utf-8") as stream:
        tokens = json.load(stream)
    if not isinstance(tokens, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in tokens.items()):
        raise ValueError("Token file must map project IDs to tokens")
    return create_runner_app(RunnerTransport(os.environ["RUNNER_TRANSPORT_DB"]), tokens)
