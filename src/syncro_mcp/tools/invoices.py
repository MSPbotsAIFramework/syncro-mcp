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
"""Invoices."""

from datetime import date
from typing import Annotated, Any, Literal

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
    iso_date,
    list_page,
    run_tool,
    scan_filtered,
)

InvoiceId = Annotated[int, Field(description="Syncro invoice id.", ge=1)]
_IGNORED = "Accepted for compatibility; the Syncro API ignores it (the invoice email endpoint takes no options)."
InvoiceStatus = Literal["paid", "unpaid", "partial", "draft", "sent", "viewed", "void"]
_UNSUPPORTED = {"draft", "sent", "viewed", "void"}


def _amount(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _invoice(payload: Any) -> Any:
    return payload.get("invoice", payload) if isinstance(payload, dict) else payload


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def syncro_invoices_list(
        customer_id: OptionalCustomerId = None,
        status: Annotated[InvoiceStatus | None, Field(description="paid, unpaid or partial (partially paid).")] = None,
        since_date: Annotated[str | None, Field(description="Only invoices updated since this ISO 8601 date.")] = None,
        query: Annotated[str | None, Field(description="Invoice number, name or customer name contains this.")] = None,
        page: Page = None,
        per_page: PerPage = None,
        offset: Offset = None,
    ) -> str:
        """List invoices (number, customer, date, total, balance) by customer, paid state or date."""

        async def action(client: SyncroClient) -> dict:
            if status in _UNSUPPORTED:
                raise ValueError(f'Syncro\'s API has no "{status}" invoice state; use paid, unpaid or partial.')
            params = compact(
                {
                    "paid": True if status == "paid" else None,
                    "unpaid": True if status in ("unpaid", "partial") else None,
                    "since_updated_at": iso_date(since_date, "since_date"),
                }
            )
            text = (query or "").strip()
            if customer_id is None and not text and status != "partial":
                return await list_page(client, "invoices", "invoices", params, page, per_page, offset)

            def keep(invoice: dict) -> bool:
                if customer_id is not None and invoice.get("customer_id") != customer_id:
                    return False
                if status == "partial" and not (_amount(invoice.get("amount_paid")) > 0 and _amount(invoice.get("balance_due")) > 0):
                    return False
                return not text or contains(invoice, text, "number", "name", "customer_business_then_name")

            if offset is not None:
                raise ValueError("offset works only without the server-side filters; use page (Syncro pages) instead.")
            return await scan_filtered(
                client, "invoices", "invoices", params, keep, page, per_page or 25,
                "customer_id / query / partial are matched by this server over the pages listed in scanned_pages.",
            )

        return await run_tool("syncro_invoices_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def syncro_invoices_get(invoice_id: InvoiceId) -> str:
        """Get one invoice with line items, totals, payments and balance due."""

        async def action(client: SyncroClient) -> Any:
            return _invoice(await client.get(f"invoices/{invoice_id}"))

        return await run_tool("syncro_invoices_get", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def syncro_invoices_create(
        customer_id: Annotated[int, Field(description="Customer to bill.", ge=1)],
        date: Annotated[str | None, Field(description="Invoice date, ISO 8601 (default today).")] = None,
        due_date: Annotated[str | None, Field(description="Due date, ISO 8601.")] = None,
        notes: Annotated[str | None, Field(description="Note printed on the invoice.")] = None,
    ) -> str:
        """Create an empty invoice for a customer, numbered by Syncro. Only when the user explicitly asks."""

        async def action(client: SyncroClient) -> Any:
            body = compact(
                {
                    "customer_id": customer_id,
                    "date": iso_date(date, "date") or _today(),
                    "due_date": iso_date(due_date, "due_date"),
                    "note": notes,
                }
            )
            return _invoice(await client.post("invoices", body))

        return await run_tool("syncro_invoices_create", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def syncro_invoices_email(
        invoice_id: InvoiceId,
        cc_emails: Annotated[str | list[str] | None, Field(description=_IGNORED)] = None,
        subject: Annotated[str | None, Field(description=_IGNORED)] = None,
        message: Annotated[str | None, Field(description=_IGNORED)] = None,
    ) -> str:
        """Email an invoice to its customer using the account's invoice email settings.

        Contacts the customer: only when the user explicitly asks for that invoice.
        cc_emails, subject and message are not supported by Syncro and are not applied.
        """
        ignored = [k for k, v in (("cc_emails", cc_emails), ("subject", subject), ("message", message)) if v is not None]

        async def action(client: SyncroClient) -> dict:
            # POST /invoices/{id}/email takes no request body; these options cannot be sent.
            await client.post(f"invoices/{invoice_id}/email")
            result = {"status": "sent", "invoice_id": invoice_id, "message": "Syncro accepted the request to email the invoice."}
            if ignored:
                result["ignored_params"] = ignored
            return result

        return await run_tool("syncro_invoices_email", client_factory, action)


def _today() -> str:
    return date.today().isoformat()
