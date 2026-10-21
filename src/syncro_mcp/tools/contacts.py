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
"""Contacts (people at a customer)."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import SyncroClient
from ._common import (
    READ_ONLY,
    WRITES,
    ClientFactory,
    OptionalCustomerId,
    Page,
    Offset,
    PerPage,
    compact,
    contains,
    list_page,
    run_tool,
    scan_filtered,
)

Text = Annotated[str | None, Field(description="Value to set.")]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def syncro_contacts_list(
        customer_id: OptionalCustomerId = None,
        query: Annotated[str | None, Field(description="Name, email or phone contains this (filtered by this server).")] = None,
        page: Page = None,
        per_page: PerPage = None,
        offset: Offset = None,
    ) -> str:
        """List contacts with email, phone and address, optionally for one customer."""

        async def action(client: SyncroClient) -> dict:
            params = compact({"customer_id": customer_id})
            text = (query or "").strip()
            if not text:
                return await list_page(client, "contacts", "contacts", params, page, per_page, offset)
            if offset is not None:
                raise ValueError("offset works only without the server-side filters; use page (Syncro pages) instead.")
            return await scan_filtered(
                client, "contacts", "contacts", params,
                lambda c: contains(c, text, "name", "email", "phone", "mobile"), page, per_page or 25,
                "query is matched by this server over the pages listed in scanned_pages.",
            )

        return await run_tool("syncro_contacts_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def syncro_contacts_get(contact_id: Annotated[int, Field(description="Syncro contact id.", ge=1)]) -> str:
        """Get one contact with email, phones, address and notes."""

        async def action(client: SyncroClient) -> Any:
            payload = await client.get(f"contacts/{contact_id}")
            return payload.get("contact", payload) if isinstance(payload, dict) else payload

        return await run_tool("syncro_contacts_get", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def syncro_contacts_create(
        customer_id: Annotated[int, Field(description="Customer the contact belongs to.", ge=1)],
        name: Annotated[str, Field(description="Full name.", min_length=1)],
        email: Text = None,
        phone: Text = None,
        mobile: Text = None,
        address1: Text = None,
        address2: Text = None,
        city: Text = None,
        state: Text = None,
        zip: Text = None,
        notes: Text = None,
    ) -> str:
        """Add a contact to a customer. Only when the user explicitly asks."""

        async def action(client: SyncroClient) -> Any:
            if not name.strip():
                raise ValueError("name must not be blank.")
            body = compact(
                {
                    "customer_id": customer_id,
                    "name": name.strip(),
                    "email": email,
                    "phone": phone,
                    "mobile": mobile,
                    "address1": address1,
                    "address2": address2,
                    "city": city,
                    "state": state,
                    "zip": zip,
                    "notes": notes,
                }
            )
            payload = await client.post("contacts", body)
            return payload.get("contact", payload) if isinstance(payload, dict) else payload

        return await run_tool("syncro_contacts_create", client_factory, action)
