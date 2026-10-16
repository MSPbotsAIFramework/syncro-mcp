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
"""Customers (the MSP's client accounts)."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import SyncroClient
from ._common import (
    READ_ONLY,
    WRITES,
    ClientFactory,
    Page,
    PerPage,
    SearchLimit,
    compact,
    list_page,
    run_tool,
    scan_filtered,
)

Text = Annotated[str | None, Field(description="Value to set.")]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def syncro_customers_list(
        query: Annotated[str | None, Field(description="Search text (name, email, phone, ...).")] = None,
        business_name: Annotated[str | None, Field(description="Business name contains this.")] = None,
        email: Annotated[str | None, Field(description="Customer email.")] = None,
        phone: Annotated[str | None, Field(description="Phone number (searched like query).")] = None,
        include_disabled: Annotated[bool | None, Field(description="Include disabled customers.")] = None,
        page: Page = None,
        per_page: PerPage = None,
    ) -> str:
        """List customers with contact details; filter by search text, business name, email or phone."""

        async def action(client: SyncroClient) -> dict:
            if query and phone:
                raise ValueError("Pass either query or phone, not both (phone is sent as the search text).")
            params = compact(
                {
                    "query": query or phone,
                    "business_name": business_name,
                    "email": email,
                    "include_disabled": include_disabled,
                }
            )
            return await list_page(client, "customers", "customers", params, page, per_page)

        return await run_tool("syncro_customers_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def syncro_customers_get(customer_id: Annotated[int, Field(description="Syncro customer id.", ge=1)]) -> str:
        """Get one customer with addresses, contacts and custom properties."""

        async def action(client: SyncroClient) -> Any:
            payload = await client.get(f"customers/{customer_id}")
            return payload.get("customer", payload) if isinstance(payload, dict) else payload

        return await run_tool("syncro_customers_get", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def syncro_customers_search(
        query: Annotated[str, Field(description="Name, business name, email or phone to look for.", min_length=1)],
        limit: SearchLimit = None,
    ) -> str:
        """Find customers by name, business name, email or phone; returns the best matches."""

        async def action(client: SyncroClient) -> dict:
            text = query.strip()
            if not text:
                raise ValueError("query must not be blank.")
            result = await scan_filtered(
                client, "customers", "customers", {"query": text}, lambda _c: True, 1, limit or 25, "Syncro search results."
            )
            result.pop("note", None)
            return result

        return await run_tool("syncro_customers_search", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def syncro_customers_create(
        business_name: Text = None,
        firstname: Text = None,
        lastname: Text = None,
        email: Text = None,
        phone: Text = None,
        mobile: Text = None,
        address: Text = None,
        address_2: Text = None,
        city: Text = None,
        state: Text = None,
        zip: Text = None,
        notes: Text = None,
    ) -> str:
        """Create a customer (business or person). Only when the user explicitly asks."""

        async def action(client: SyncroClient) -> Any:
            if not any(v and v.strip() for v in (business_name, firstname, lastname)):
                raise ValueError("Give business_name, or firstname / lastname for a person.")
            body = compact(
                {
                    "business_name": business_name,
                    "firstname": firstname,
                    "lastname": lastname,
                    "email": email,
                    "phone": phone,
                    "mobile": mobile,
                    "address": address,
                    "address_2": address_2,
                    "city": city,
                    "state": state,
                    "zip": zip,
                    "notes": notes,
                }
            )
            payload = await client.post("customers", body)
            return payload.get("customer", payload) if isinstance(payload, dict) else payload

        return await run_tool("syncro_customers_create", client_factory, action)
