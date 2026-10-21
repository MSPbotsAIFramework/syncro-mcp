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
"""Customer assets (managed devices)."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import SyncroClient
from ._common import (
    READ_ONLY,
    ClientFactory,
    OptionalCustomerId,
    Page,
    Offset,
    PerPage,
    SearchLimit,
    compact,
    list_page,
    run_tool,
    scan_filtered,
)

Serial = Annotated[str | None, Field(description="Exact serial number.")]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def syncro_assets_list(
        customer_id: OptionalCustomerId = None,
        asset_type: Annotated[str | None, Field(description='Asset type name, e.g. "Syncro Device" (filtered by this server).')] = None,
        asset_serial: Serial = None,
        query: Annotated[str | None, Field(description="Search text (name, serial, ...).")] = None,
        page: Page = None,
        per_page: PerPage = None,
        offset: Offset = None,
    ) -> str:
        """List assets (devices) with type, serial, customer and RMM properties."""

        async def action(client: SyncroClient) -> dict:
            serial = (asset_serial or "").strip() or None
            if serial and query:
                raise ValueError("Pass either query or asset_serial, not both.")
            params = compact({"customer_id": customer_id, "query": query or serial})
            if not asset_type and not serial:
                return await list_page(client, "customer_assets", "assets", params, page, per_page, offset)
            kind = (asset_type or "").strip().lower()

            def keep(asset: dict) -> bool:
                if kind and str(asset.get("asset_type") or "").lower() != kind:
                    return False
                return not serial or str(asset.get("asset_serial") or "").lower() == serial.lower()

            if offset is not None:
                raise ValueError("offset works only without the server-side filters; use page (Syncro pages) instead.")
            return await scan_filtered(
                client, "customer_assets", "assets", params, keep, page, per_page or 25,
                "asset_type / asset_serial are matched by this server over the pages listed in scanned_pages.",
            )

        return await run_tool("syncro_assets_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def syncro_assets_get(asset_id: Annotated[int, Field(description="Syncro asset id.", ge=1)]) -> str:
        """Get one asset with its customer, type, serial and properties (OS, hardware, RMM data)."""

        async def action(client: SyncroClient) -> Any:
            payload = await client.get(f"customer_assets/{asset_id}")
            return payload.get("asset", payload) if isinstance(payload, dict) else payload

        return await run_tool("syncro_assets_get", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def syncro_assets_search(
        query: Annotated[str | None, Field(description="Name or other text to look for.")] = None,
        asset_serial: Serial = None,
        customer_id: OptionalCustomerId = None,
        limit: SearchLimit = None,
    ) -> str:
        """Find assets by name or serial number, optionally within one customer."""

        async def action(client: SyncroClient) -> dict:
            text = (query or "").strip()
            serial = (asset_serial or "").strip()
            if not text and not serial:
                raise ValueError("Give query or asset_serial.")

            def keep(asset: dict) -> bool:
                return not serial or str(asset.get("asset_serial") or "").lower() == serial.lower()

            result = await scan_filtered(
                client, "customer_assets", "assets", compact({"query": text or serial, "customer_id": customer_id}),
                keep, 1, limit or 25, "Syncro search results.", keep_all=False,
            )
            result.pop("note", None)
            return result

        return await run_tool("syncro_assets_search", client_factory, action)
