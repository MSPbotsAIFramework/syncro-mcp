# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Credential middleware: 401/400 on bad headers, per-request isolation."""

import asyncio

import pytest
from starlette.testclient import TestClient

from syncro_mcp.__main__ import build_http_app
from syncro_mcp.server import (
    GatewayCredentialsMiddleware,
    _credentials_var,
    create_mcp_server,
    get_client_from_context,
    normalize_subdomain,
)

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
CREDS = {"X-Syncro-Subdomain": "yourcompany", "X-Syncro-API-Key": "your-syncro-api-key"}


def _client() -> TestClient:
    return TestClient(build_http_app(create_mcp_server()))


def test_health_is_local():
    with _client() as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Syncro-API-Key": "key"},
        {"X-Syncro-Subdomain": "yourcompany"},
        {"X-Syncro-Subdomain": "yourcompany", "X-Syncro-API-Key": "Bearer "},
        {"X-Syncro-Subdomain": "yourcompany", "X-Syncro-API-Key": "${SYNCRO_API_KEY}"},
    ],
)
def test_missing_credentials_return_401(headers):
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 401
    assert resp.json()["required_headers"] == ["X-Syncro-Subdomain", "X-Syncro-API-Key"]


def test_bad_subdomain_returns_400():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS | {"X-Syncro-Subdomain": "evil.example.com/x"})
    assert resp.status_code == 400


def test_tools_list_over_http():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS)
    assert resp.status_code == 200
    for name in ("syncro_navigate", "syncro_tickets_list", "syncro_invoices_email", "syncro_assets_search"):
        assert name in resp.text


@pytest.mark.parametrize(
    "value,expected",
    [
        ("yourcompany", "yourcompany"),
        ("YOURCOMPANY", "yourcompany"),
        ("yourcompany.syncromsp.com", "yourcompany"),
        ("https://yourcompany.syncromsp.com/", "yourcompany"),
        ("my-shop", "my-shop"),
        ("evil.example.com", None),
        ("yourcompany/../x", None),
        ("-yourcompany", None),
    ],
)
def test_normalize_subdomain(value, expected):
    assert normalize_subdomain(value) == expected


def _run_middleware(headers: list[tuple[bytes, bytes]]) -> dict:
    seen: dict = {}

    async def app(scope, receive, send):
        seen["credentials"] = _credentials_var.get()
        seen["client"] = get_client_from_context()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    asyncio.run(GatewayCredentialsMiddleware(app)({"type": "http", "path": "/mcp", "headers": headers}, receive, send))
    return seen


def test_headers_reach_request_context_and_are_reset():
    seen = _run_middleware([(b"x-syncro-subdomain", b"https://yourcompany.syncromsp.com"), (b"x-syncro-api-key", b"Bearer key-123")])
    assert seen["credentials"].api_key == "key-123"
    assert seen["client"].base_url == "https://yourcompany.syncromsp.com/api/v1"
    assert _credentials_var.get() is None


def test_concurrent_requests_do_not_share_credentials():
    observed: list[tuple[str, str]] = []

    async def app(scope, receive, send):
        before = _credentials_var.get().api_key
        await asyncio.sleep(0.01)
        observed.append((before, _credentials_var.get().api_key))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = GatewayCredentialsMiddleware(app)

    def scope(key: bytes) -> dict:
        return {"type": "http", "path": "/mcp", "headers": [(b"x-syncro-subdomain", b"yourcompany"), (b"x-syncro-api-key", key)]}

    async def both():
        await asyncio.gather(middleware(scope(b"tenant-a"), receive, send), middleware(scope(b"tenant-b"), receive, send))

    asyncio.run(both())
    assert sorted(observed) == [("tenant-a", "tenant-a"), ("tenant-b", "tenant-b")]


def test_credentials_repr_hides_key():
    seen = _run_middleware([(b"x-syncro-subdomain", b"yourcompany"), (b"x-syncro-api-key", b"very-secret-value")])
    assert "very-secret-value" not in repr(seen["credentials"])
