"""Фейковый gateway: принимает runner по WebSocket, отвечает `welcome`, пишет входящие кадры."""

import asyncio
import json
import threading
from collections.abc import Callable, Coroutine
from typing import Any, Self, final

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed


@final
class FakeGateway:
    def __init__(
        self,
        *,
        token: str = "gw-secret-token",  # noqa: S107 — тестовый токен
        acked_seq: int = 0,
        host: str = "127.0.0.1",
    ) -> None:
        self._token = token
        self._acked = acked_seq
        self._host = host
        self._server: Server | None = None
        self._ws: ServerConnection | None = None
        self._changed = asyncio.Event()
        self.received: list[dict[str, Any]] = []
        self.connections = 0

    async def __aenter__(self) -> Self:
        self._server = await serve(self._handle, self._host, 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        assert self._server is not None
        port = next(iter(self._server.sockets)).getsockname()[1]
        return f"ws://127.0.0.1:{port}/runner"

    @property
    def acked_seq(self) -> int:
        """Как настоящий gateway: подтверждено всё, что дошло и записано."""
        seqs: list[int] = [m["journal_seq"] for m in self.received if m.get("type") == "event"]
        return max([self._acked, *seqs])

    def events(self) -> list[dict[str, Any]]:
        return [m for m in self.received if m.get("type") == "event"]

    def unique_events(self) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for m in self.events():
            if m["event"]["event_id"] not in seen:
                seen.add(m["event"]["event_id"])
                out.append(m)
        return out

    async def send(self, message: dict[str, Any]) -> None:
        assert self._ws is not None, "runner is not connected"
        await self._ws.send(json.dumps(message, ensure_ascii=False))

    async def drop(self) -> None:
        assert self._ws is not None
        await self._ws.close()

    async def wait_for(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float = 5.0,  # noqa: ASYNC109 — граница теста, внутри asyncio.timeout
    ) -> dict[str, Any]:
        async with asyncio.timeout(timeout):
            while True:
                for message in self.received:
                    if predicate(message):
                        return message
                self._changed.clear()
                await self._changed.wait()

    async def _handle(self, ws: ServerConnection) -> None:
        header = ws.request.headers.get("Authorization") if ws.request else None
        if header != f"Bearer {self._token}":
            await ws.close(code=1008, reason="unauthorized")
            return
        self.connections += 1
        self._ws = ws
        try:
            async for raw in ws:
                message = json.loads(raw)
                self.received.append(message)
                self._changed.set()
                if message.get("type") == "hello":
                    welcome = {"v": 1, "type": "welcome", "acked_seq": self.acked_seq}
                    await ws.send(json.dumps(welcome))
        except ConnectionClosed:
            pass  # runner убит или переподключается: для фейка это штатный обрыв
        finally:
            if self._ws is ws:
                self._ws = None


@final
class ThreadedGateway:
    """FakeGateway в фоновом потоке: синхронный интерфейс для e2e-тестов."""

    def __init__(self, *, host: str = "127.0.0.1", token: str = "gw-secret-token") -> None:  # noqa: S107
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._gw: FakeGateway = self._call(self._create(host, token))

    async def _create(self, host: str, token: str) -> FakeGateway:
        gw = FakeGateway(host=host, token=token)
        await gw.__aenter__()
        return gw

    def _call[T](self, coro: Coroutine[Any, Any, T], timeout: float = 30.0) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    @property
    def url(self) -> str:
        return self._gw.url

    def send(self, message: dict[str, Any]) -> None:
        self._call(self._gw.send(message))

    def wait_for(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 120.0
    ) -> dict[str, Any]:
        return self._call(self._gw.wait_for(predicate, timeout), timeout + 5)

    def events(self, run_id: str) -> list[dict[str, Any]]:
        return [m["event"] for m in self._gw.unique_events() if m["event"]["run_id"] == run_id]

    def close(self) -> None:
        self._call(self._gw.__aexit__(None, None, None))
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()  # иначе ResourceWarning соседнего теста под `-W error`
