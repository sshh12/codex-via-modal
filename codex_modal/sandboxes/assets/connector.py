"""Platform-side publishing connector.

Runs *inside* the operator's container and bridges the host publishing bridge to
the local service the operator built. The operator never sees or runs this - the
platform copies it in and starts it, so building a public tunnel is no longer part
of the operator's job: it just listens on a local port.

Flow (all over the container's outbound HTTP proxy, which reaches the bridge's
public URL): announce online, long-poll for the next public request, replay it
against ``http://localhost:<port>``, and post the response back. Stdlib only, so
it runs against the image's plain ``python3`` with nothing to install.
"""

from __future__ import annotations

import argparse
import base64
import json
import threading
import time
import urllib.error
import urllib.request

_LOCAL_TIMEOUT = 55.0
_POLL_WAIT = 25.0


def _local_opener() -> urllib.request.OpenerDirector:
    # localhost is in the container's NO_PROXY, but build an explicit no-proxy
    # opener so the loopback call can never be sent to the outbound proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _forward_local(base: str, item: dict, opener: urllib.request.OpenerDirector) -> dict:
    """Replay one captured request against the local service."""

    url = base.rstrip("/") + item["path"]
    if item.get("query"):
        url = f"{url}?{item['query']}"
    body = base64.b64decode(item.get("body", "") or "")
    headers = {
        name: value
        for name, value in dict(item.get("headers", {})).items()
        if name.lower() not in {"host", "content-length", "connection"}
    }
    request = urllib.request.Request(
        url, data=body if body else None, headers=headers, method=item["method"]
    )
    try:
        with opener.open(request, timeout=_LOCAL_TIMEOUT) as response:
            payload = response.read()
            return {
                "status": response.status,
                "headers": {k: v for k, v in response.headers.items()},
                "body": base64.b64encode(payload).decode("ascii"),
            }
    except urllib.error.HTTPError as error:
        payload = error.read()
        return {
            "status": error.code,
            "headers": {k: v for k, v in (error.headers or {}).items()},
            "body": base64.b64encode(payload).decode("ascii"),
        }
    except (urllib.error.URLError, TimeoutError, OSError):
        # The service is not up yet (or refused). Report a gateway error so the
        # host barrier keeps waiting rather than treating this as a real answer.
        return {"status": 502, "headers": {}, "body": ""}


def _complete(relay: str, code: str, identifier: str, result: dict) -> None:
    data = json.dumps(result).encode("utf-8")
    request = urllib.request.Request(
        f"{relay}/connect/{code}/complete/{identifier}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=30.0).close()
    except (urllib.error.URLError, TimeoutError, OSError):
        pass


def _handle(relay: str, code: str, local: str, item: dict, opener) -> None:
    result = _forward_local(local, item, opener)
    _complete(relay, code, str(item["id"]), result)


def run(relay: str, code: str, port: int) -> None:
    relay = relay.rstrip("/")
    local = f"http://localhost:{port}"
    opener = _local_opener()

    # Announce online (retry until the bridge is reachable through the proxy).
    for _ in range(60):
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"{relay}/connect/{code}/online", data=b"", method="POST"),
                timeout=30.0,
            ).close()
            break
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2.0)

    next_url = f"{relay}/connect/{code}/next?wait={_POLL_WAIT}"
    while True:
        try:
            with urllib.request.urlopen(next_url, timeout=_POLL_WAIT + 15.0) as response:
                if response.status == 204:
                    continue
                item = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError:
            time.sleep(1.0)
            continue
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            time.sleep(1.0)
            continue
        threading.Thread(
            target=_handle, args=(relay, code, local, item, opener), daemon=True
        ).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--relay", required=True, help="public base URL of the host bridge")
    parser.add_argument("--code", required=True, help="connector-side code")
    parser.add_argument("--port", type=int, required=True, help="local service port")
    args = parser.parse_args(argv)
    run(args.relay, args.code, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
