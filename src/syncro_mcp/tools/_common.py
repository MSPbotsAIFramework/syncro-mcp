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
"""Shared argument types, paging / filtering helpers and the error-handling wrapper."""

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped, error_envelope
from ..api_client import SyncroClient, SyncroError
from ..config import MAX_SCAN_PAGES, PAGE_SIZE

logger = logging.getLogger("syncro_mcp.tools")

ClientFactory = Callable[[], SyncroClient | None]
Predicate = Callable[[dict], bool]

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
WRITES_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)

NOT_CONFIGURED = error_envelope(
    "not_configured", "Syncro credentials are missing for this request (X-Syncro-Subdomain and X-Syncro-API-Key).", False
)

Page = Annotated[int | None, Field(description="Page number, from 1 (25 records per page).", ge=1)]
PerPage = Annotated[int | None, Field(description="Records to return from the page (Syncro pages hold 25).", ge=1, le=25)]
OptionalCustomerId = Annotated[int | None, Field(description="Syncro customer id.", ge=1)]
SearchLimit = Annotated[int | None, Field(description="Maximum results (1-100, default 25).", ge=1, le=100)]


def compact(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def iso_date(value: str | None, name: str) -> str | None:
    """Validate an ISO 8601 date / datetime and return the YYYY-MM-DD part Syncro filters take."""
    if not value or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
        except ValueError as e:
            raise ValueError(f'{name} must be an ISO 8601 date, e.g. "2026-06-01" (got "{value}").') from e
    return parsed.date().isoformat()


def contains(record: dict, needle: str, *fields: str) -> bool:
    text = needle.lower()
    return any(text in str(record.get(f) or "").lower() for f in fields)


def _page_payload(payload: Any, key: str) -> tuple[list[dict], dict]:
    items = payload.get(key) if isinstance(payload, dict) else None
    meta = payload.get("meta") if isinstance(payload, dict) else None
    return [i for i in items or [] if isinstance(i, dict)], meta if isinstance(meta, dict) else {}


async def list_page(
    client: SyncroClient, path: str, key: str, params: dict[str, Any], page: int | None, per_page: int | None = None
) -> dict:
    """One Syncro page: {key: [...], total_entries, total_pages, page}."""
    current = page or 1
    items, meta = _page_payload(await client.get(path, compact({**params, "page": current})), key)
    if per_page:
        items = items[:per_page]
    return compact(
        {
            key: items,
            "count": len(items),
            "total_entries": meta.get("total_entries"),
            "total_pages": meta.get("total_pages"),
            "page": meta.get("page", current),
        }
    )


async def scan_filtered(
    client: SyncroClient,
    path: str,
    key: str,
    params: dict[str, Any],
    predicate: Predicate,
    page: int | None,
    want: int,
    note: str,
) -> dict:
    """Read up to MAX_SCAN_PAGES pages from ``page`` and keep records matching ``predicate``.

    Used for filters the Syncro API does not offer itself; the result says
    which pages were read so the caller can continue from next_page.
    """
    current = page or 1
    first, matches, total_pages = current, [], None
    for _ in range(MAX_SCAN_PAGES):
        items, meta = _page_payload(await client.get(path, compact({**params, "page": current})), key)
        total_pages = meta.get("total_pages", total_pages)
        matches.extend(i for i in items if predicate(i))
        exhausted = len(items) < PAGE_SIZE or (isinstance(total_pages, int) and current >= total_pages)
        if len(matches) >= want or exhausted:
            break
        current += 1
    else:
        exhausted = False
    return compact(
        {
            key: matches[:want],
            "count": min(len(matches), want),
            "scanned_pages": f"{first}-{current}",
            "total_pages": total_pages,
            "next_page": None if exhausted else current + 1,
            "note": note,
        }
    )


async def run_tool(
    name: str,
    client_factory: ClientFactory,
    action: Callable[[SyncroClient], Awaitable[Any]],
) -> str:
    """Resolve the caller's client, run ``action``, and always return a string.

    Errors never escape as exceptions: they become the standard error envelope.
    """
    client = client_factory()
    if client is None:
        return NOT_CONFIGURED
    started = time.monotonic()
    try:
        result = await action(client)
    except SyncroError as e:
        logger.info("tool=%s outcome=error status=%s ms=%d", name, e.status_code, _ms(started))
        return e.to_envelope()
    except ValueError as e:
        logger.info("tool=%s outcome=invalid_argument ms=%d", name, _ms(started))
        return error_envelope("invalid_argument", str(e), False)
    except Exception as e:  # noqa: BLE001 - tools must never raise
        logger.warning("tool=%s outcome=unexpected error=%s ms=%d", name, type(e).__name__, _ms(started))
        return error_envelope("upstream_error", f"Unexpected error: {type(e).__name__}", True)
    logger.info("tool=%s outcome=ok ms=%d", name, _ms(started))
    if result is None:
        result = {"result": None, "note": "Syncro returned an empty response."}
    return result if isinstance(result, str) else dump_json_capped(result)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
