"""Host-side publishing bridge for the local backend's ``expose_port``.

A public request arrives at ``/p/{public_code}/...``; the bridge parks it and
hands it to the connector inside the sandbox, which long-polls ``/connect/
{connector_code}/next``, replays the request against ``localhost:<port>`` and
posts the response back. Nothing here reaches into the sandbox - the sandbox
reaches out, over the same broker every other request uses.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import secrets
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
MAX_BODY_BYTES = 8 * 1024 * 1024


@dataclass
class Pending:
    future: asyncio.Future[dict[str, Any]]


class Bridge:
    def __init__(self, *, request_timeout_s: float) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.pending: dict[str, Pending] = {}
        self.request_timeout_s = request_timeout_s
        self.online_at: float | None = None

    async def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        identifier = str(request["id"])
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[identifier] = Pending(future=future)
        await self.queue.put(request)
        try:
            return await asyncio.wait_for(future, timeout=self.request_timeout_s)
        finally:
            self.pending.pop(identifier, None)

    def finish(self, identifier: str, result: dict[str, Any]) -> bool:
        pending = self.pending.get(identifier)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(result)
        return True


def create_app(
    *,
    public_code: str,
    connector_code: str,
    request_timeout_s: float = 180.0,
) -> FastAPI:
    app = FastAPI(title="Web publishing bridge", docs_url=None, redoc_url=None)
    bridge = Bridge(request_timeout_s=request_timeout_s)

    def check(value: str, expected: str) -> None:
        if not secrets.compare_digest(value, expected):
            raise HTTPException(status_code=404, detail="not found")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/connect/{code}/online", status_code=202)
    async def online(code: str) -> dict[str, str]:
        check(code, connector_code)
        bridge.online_at = asyncio.get_running_loop().time()
        return {"status": "ready"}

    @app.get("/connect/{code}/next")
    async def next_request(code: str, wait: float = 25.0) -> Response:
        check(code, connector_code)
        wait = min(max(wait, 0.1), 30.0)
        try:
            item = await asyncio.wait_for(bridge.queue.get(), timeout=wait)
        except asyncio.TimeoutError:
            return Response(status_code=204)
        return JSONResponse(item)

    @app.post("/connect/{code}/complete/{identifier}", status_code=202)
    async def complete(code: str, identifier: str, request: Request) -> dict[str, str]:
        check(code, connector_code)
        try:
            result = await request.json()
        except ValueError as error:
            raise HTTPException(status_code=400, detail="invalid response") from error
        if not isinstance(result, dict) or not bridge.finish(identifier, result):
            raise HTTPException(status_code=404, detail="not found")
        return {"status": "recorded"}

    async def publish(code: str, path: str, request: Request) -> Response:
        check(code, public_code)
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="payload too large")
        identifier = secrets.token_hex(12)
        item = {
            "id": identifier,
            "method": request.method,
            "path": "/" + path,
            "query": request.url.query,
            "headers": {
                name: value
                for name, value in request.headers.items()
                if name.lower() not in _HOP_HEADERS
            },
            "body": base64.b64encode(body).decode("ascii"),
        }
        try:
            result = await bridge.submit(item)
        except asyncio.TimeoutError as error:
            raise HTTPException(status_code=504, detail="service did not respond") from error
        try:
            status = int(result.get("status", 502))
            response_body = base64.b64decode(str(result.get("body", "")), validate=True)
            headers = {
                str(name): str(value)
                for name, value in dict(result.get("headers", {})).items()
                if str(name).lower() not in _HOP_HEADERS
            }
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=502, detail="invalid service response") from error
        return Response(content=response_body, status_code=status, headers=headers)

    @app.api_route("/p/{code}", methods=_METHODS)
    async def publish_root(code: str, request: Request) -> Response:
        return await publish(code, "", request)

    @app.api_route("/p/{code}/{path:path}", methods=_METHODS)
    async def publish_path(code: str, path: str, request: Request) -> Response:
        return await publish(code, path, request)

    app.state.bridge = bridge
    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex_modal.sandboxes.bridge")
    parser.add_argument("--public-code", required=True)
    parser.add_argument("--connector-code", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--request-timeout", type=float, default=180.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    app = create_app(
        public_code=args.public_code,
        connector_code=args.connector_code,
        request_timeout_s=args.request_timeout,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
