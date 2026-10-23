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
"""FastMCP factory and the per-request credential middleware."""

import contextvars
import re
import uuid
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ._arguments import EnvelopeFastMCP
from .api_client import SyncroClient, SyncroCredentials
from .config import Settings

HEADER_SUBDOMAIN = "X-Syncro-Subdomain"
HEADER_API_KEY = "X-Syncro-API-Key"
REQUIRED_HEADERS = [HEADER_SUBDOMAIN, HEADER_API_KEY]

_PLACEHOLDER = re.compile(r"^\$\{.*\}$")
_BEARER = re.compile(r"^bearer(\s+|$)", re.IGNORECASE)
_SUBDOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HOST = re.compile(r"^(?:https?://)?([^/.]+)\.syncromsp\.com(?:/.*)?$", re.IGNORECASE)

# Per-request isolation: the middleware sets these for the duration of one
# HTTP request and resets them in `finally`. Nothing credential-related is
# ever stored at module level.
_credentials_var: contextvars.ContextVar[SyncroCredentials | None] = contextvars.ContextVar(
    "syncro_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("syncro_request_id", default="-")

INSTRUCTIONS = (
    "Syncro (SyncroMSP) is a PSA + RMM for MSPs. This server reads and, on "
    "request, writes one Syncro account: customers, their contacts, tickets "
    "(with comments), assets (managed devices) and invoices.\n"
    "Usage:\n"
    "- Records use numeric ids. Find a customer with syncro_customers_search "
    "or syncro_customers_list(query), then filter tickets, contacts, assets "
    "and invoices by customer_id.\n"
    "- Lists take per_page and page (or offset) and return has_more with "
    "next_page / next_offset; follow them until has_more is false. Some filters "
    "are applied by this server over a few pages; such results report "
    "scanned_pages and next_page.\n"
    "- syncro_tickets_get includes the ticket's comments.\n"
    "- syncro_status checks the connection and shows the API user.\n"
    "Creating or updating tickets, customers, contacts and invoices, adding "
    "comments (which may email the customer) and emailing invoices change "
    "live records or contact customers: call those tools only when the user "
    "explicitly asks, with the exact values given."
)


def get_client_from_context() -> SyncroClient | None:
    """The client for the current request, or None outside a request."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return SyncroClient(credentials)


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or _PLACEHOLDER.match(text):
        return None
    return text


def normalize_subdomain(value: str) -> str | None:
    """"yourcompany", "yourcompany.syncromsp.com" or "https://yourcompany.syncromsp.com/" -> "yourcompany"."""
    text = value.strip().lower()
    host = _HOST.match(text)
    if host:
        text = host.group(1)
    return text if _SUBDOMAIN.match(text) else None


class GatewayCredentialsMiddleware:
    """Reads the Syncro headers on /mcp requests into the request context.

    Needs X-Syncro-Subdomain and X-Syncro-API-Key; otherwise 401.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        subdomain_raw = _clean(request.headers.get(HEADER_SUBDOMAIN))
        api_key = _clean(request.headers.get(HEADER_API_KEY))
        if api_key:
            api_key = _clean(_BEARER.sub("", api_key))
        missing = [name for name, value in ((HEADER_SUBDOMAIN, subdomain_raw), (HEADER_API_KEY, api_key)) if not value]
        if missing:
            await JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": f"Missing header(s): {', '.join(missing)}",
                    "required_headers": REQUIRED_HEADERS,
                },
                status_code=401,
            )(scope, receive, send)
            return

        subdomain = normalize_subdomain(subdomain_raw or "")
        if subdomain is None:
            await JSONResponse(
                {"error": "Invalid header", "message": f"{HEADER_SUBDOMAIN} must be the account name, e.g. yourcompany for yourcompany.syncromsp.com"},
                status_code=400,
            )(scope, receive, send)
            return

        credentials = SyncroCredentials(subdomain=subdomain, api_key=api_key or "")
        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        creds_token = _credentials_var.set(credentials)
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool."""
    mcp = EnvelopeFastMCP(
        name="syncro-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    client_factory: Callable[[], SyncroClient | None] = get_client_from_context

    from .tools import assets, contacts, customers, invoices, navigation, tickets

    for module in (navigation, customers, tickets, assets, contacts, invoices):
        module.register(mcp, client_factory)

    return mcp
