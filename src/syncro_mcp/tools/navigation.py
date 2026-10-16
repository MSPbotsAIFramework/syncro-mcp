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
"""Tool discovery (kept for compatibility) and the connection check."""

from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .._json import compact as to_json
from ..api_client import SyncroClient
from ._common import LOCAL, READ_ONLY, ClientFactory, run_tool

DOMAINS: dict[str, tuple[str, list[str]]] = {
    "customers": (
        "Customer accounts: list, get, search, create.",
        ["syncro_customers_list", "syncro_customers_get", "syncro_customers_search", "syncro_customers_create"],
    ),
    "tickets": (
        "Support tickets: list, get with comments, create, update, add comments.",
        ["syncro_tickets_list", "syncro_tickets_get", "syncro_tickets_create", "syncro_tickets_update", "syncro_tickets_add_comment"],
    ),
    "assets": ("Managed assets (devices): list, get, search.", ["syncro_assets_list", "syncro_assets_get", "syncro_assets_search"]),
    "contacts": ("Customer contacts: list, get, create.", ["syncro_contacts_list", "syncro_contacts_get", "syncro_contacts_create"]),
    "invoices": (
        "Invoices: list, get, create, email to the customer.",
        ["syncro_invoices_list", "syncro_invoices_get", "syncro_invoices_create", "syncro_invoices_email"],
    ),
}


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=LOCAL)
    async def syncro_navigate(
        domain: Annotated[
            Literal["customers", "tickets", "assets", "contacts", "invoices"], Field(description="Area to describe.")
        ],
    ) -> str:
        """Describe the tools of one Syncro area. All tools are already callable; this changes nothing."""
        summary, tools = DOMAINS[domain]
        return to_json({"domain": domain, "summary": summary, "tools": tools})

    @mcp.tool(annotations=READ_ONLY)
    async def syncro_status() -> str:
        """Check the Syncro connection: account subdomain, API user and its permissions."""

        async def action(client: SyncroClient) -> dict:
            me = await client.get("me") or {}
            return {
                "connected": True,
                "subdomain": client.subdomain,
                "user": {k: me.get(k) for k in ("user_name", "user_email", "user_id", "admin")} if isinstance(me, dict) else me,
                "permissions": me.get("permissions") if isinstance(me, dict) else None,
                "domains": list(DOMAINS),
            }

        return await run_tool("syncro_status", client_factory, action)
