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
"""Tickets and ticket comments."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import SyncroClient
from ._common import (
    READ_ONLY,
    WRITES,
    WRITES_IDEMPOTENT,
    ClientFactory,
    OptionalCustomerId,
    Page,
    Offset,
    PerPage,
    compact,
    iso_date,
    list_page,
    run_tool,
    filtered_page,
)

TicketId = Annotated[int, Field(description="Syncro ticket id (not the ticket number).", ge=1)]
Status = Annotated[
    str | None, Field(description='Ticket status, e.g. "New", "In Progress", "Waiting on Customer", "Resolved".')
]
UserId = Annotated[int | None, Field(description="Syncro user (technician) id.", ge=1)]
DueDate = Annotated[str | None, Field(description="Due date, ISO 8601.")]


def _ticket(payload: Any) -> Any:
    return payload.get("ticket", payload) if isinstance(payload, dict) else payload


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def syncro_tickets_list(
        customer_id: OptionalCustomerId = None,
        contact_id: Annotated[int | None, Field(description="Syncro contact id.", ge=1)] = None,
        status: Status = None,
        user_id: UserId = None,
        problem_type: Annotated[str | None, Field(description="Problem type, exact (filtered by this server).")] = None,
        resolved: Annotated[bool | None, Field(description="Only resolved (true) or unresolved (false) tickets.")] = None,
        query: Annotated[str | None, Field(description="Search text.")] = None,
        since: Annotated[str | None, Field(description="Only tickets updated since this ISO 8601 date.")] = None,
        page: Page = None,
        per_page: PerPage = None,
        offset: Offset = None,
    ) -> str:
        """List tickets, newest first, by customer, contact, status, technician, problem type or date."""

        async def action(client: SyncroClient) -> dict:
            params = compact(
                {
                    "customer_id": customer_id,
                    "contact_id": contact_id,
                    "status": status,
                    "user_id": user_id,
                    "query": query,
                    "since_updated_at": iso_date(since, "since"),
                }
            )
            if problem_type is None and resolved is None:
                return await list_page(client, "tickets", "tickets", params, page, per_page, offset)
            wanted = problem_type.strip().lower() if problem_type else None

            def keep(ticket: dict) -> bool:
                if wanted and str(ticket.get("problem_type") or "").lower() != wanted:
                    return False
                return resolved is None or bool(ticket.get("resolved_at")) is resolved

            return await filtered_page(
                client, "tickets", "tickets", params, keep, page, per_page, offset,
                "problem_type / resolved are filtered by this server over the pages listed in scanned_pages.",
            )

        return await run_tool("syncro_tickets_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def syncro_tickets_get(ticket_id: TicketId) -> str:
        """Get one ticket with its comments, customer, assignee, status and dates."""

        async def action(client: SyncroClient) -> Any:
            return _ticket(await client.get(f"tickets/{ticket_id}"))

        return await run_tool("syncro_tickets_get", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def syncro_tickets_create(
        customer_id: Annotated[int, Field(description="Syncro customer id.", ge=1)],
        subject: Annotated[str, Field(description="Ticket subject.", min_length=1)],
        problem_type: Annotated[str | None, Field(description="Problem type as configured in Syncro.")] = None,
        status: Status = None,
        contact_id: Annotated[int | None, Field(description="Syncro contact id.", ge=1)] = None,
        user_id: UserId = None,
        due_date: DueDate = None,
        comment_body: Annotated[str | None, Field(description="Initial description, added as the first comment.")] = None,
        comment_subject: Annotated[str | None, Field(description='Subject of that comment (default "Initial Issue").')] = None,
    ) -> str:
        """Create a ticket for a customer. Only when the user explicitly asks."""

        async def action(client: SyncroClient) -> Any:
            if not subject.strip():
                raise ValueError("subject must not be blank.")
            comment = None
            if comment_body and comment_body.strip():
                comment = [{"subject": comment_subject or "Initial Issue", "body": comment_body, "hidden": False}]
            body = compact(
                {
                    "customer_id": customer_id,
                    "subject": subject.strip(),
                    "problem_type": problem_type,
                    "status": status,
                    "contact_id": contact_id,
                    "user_id": user_id,
                    "due_date": due_date,
                    "comments_attributes": comment,
                }
            )
            return _ticket(await client.post("tickets", body))

        return await run_tool("syncro_tickets_create", client_factory, action)

    @mcp.tool(annotations=WRITES_IDEMPOTENT)
    async def syncro_tickets_update(
        ticket_id: TicketId,
        subject: Annotated[str | None, Field(description="New subject.")] = None,
        problem_type: Annotated[str | None, Field(description="New problem type.")] = None,
        status: Status = None,
        user_id: UserId = None,
        due_date: DueDate = None,
    ) -> str:
        """Change a ticket's subject, problem type, status, technician or due date. Only on explicit request."""

        async def action(client: SyncroClient) -> Any:
            body = compact(
                {"subject": subject, "problem_type": problem_type, "status": status, "user_id": user_id, "due_date": due_date}
            )
            if not body:
                raise ValueError("Nothing to update: pass at least one field to change.")
            return _ticket(await client.put(f"tickets/{ticket_id}", body))

        return await run_tool("syncro_tickets_update", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def syncro_tickets_add_comment(
        ticket_id: TicketId,
        body: Annotated[str, Field(description="Comment text.", min_length=1)],
        subject: Annotated[str | None, Field(description='Comment subject (default "Update").')] = None,
        hidden: Annotated[bool | None, Field(description="Internal note not shown to the customer.")] = None,
        do_not_email: Annotated[bool | None, Field(description="Do not email the customer about it.")] = None,
    ) -> str:
        """Add a comment to a ticket; public comments may email the customer.

        Only when the user explicitly asks.
        """

        async def action(client: SyncroClient) -> Any:
            if not body.strip():
                raise ValueError("body must not be blank.")
            payload = await client.post(
                f"tickets/{ticket_id}/comment",
                compact({"subject": subject or "Update", "body": body, "hidden": hidden, "do_not_email": do_not_email}),
            )
            return payload.get("comment", payload) if isinstance(payload, dict) else payload

        return await run_tool("syncro_tickets_add_comment", client_factory, action)
