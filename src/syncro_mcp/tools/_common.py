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
from ..config import MAX_PER_PAGE, MAX_SCAN_PAGES, NATIVE_PAGE_SIZES, PAGE_SIZE

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

Page = Annotated[int | None, Field(description="Page number, from 1, in pages of per_page records.", ge=1)]
PerPage = Annotated[
    int | None,
    Field(description=f"Records per page (1-{MAX_PER_PAGE}; default: Syncro's own page size for this list).", ge=1, le=MAX_PER_PAGE),
]
Offset = Annotated[
    int | None,
    Field(description="Start at this record (0-based) instead of page; use next_offset from the previous result.", ge=0),
]
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
    client: SyncroClient,
    path: str,
    key: str,
    params: dict[str, Any],
    page: int | None,
    per_page: int | None = None,
    offset: int | None = None,
) -> dict:
    """Records ``offset`` (or ``(page - 1) * per_page``) onward, ``per_page`` of them.

    Syncro pages have a fixed size per endpoint and most endpoints take no per_page,
    so the caller's window is mapped onto Syncro pages (reading the next one when the
    window spans two). The result has has_more / next_offset (and next_page when the
    window is page-aligned), so following them never skips or repeats a record.
    """
    if page is not None and offset is not None:
        raise ValueError("Pass page or offset, not both.")
    size = NATIVE_PAGE_SIZES.get(path, PAGE_SIZE)
    for _ in range(3):  # re-run once the response reveals a different page size
        want = per_page or size
        start = offset if offset is not None else ((page or 1) - 1) * want
        window = await _read_window(client, path, key, params, start, want, size)
        if isinstance(window, int):
            size = window
            continue
        rows, total = window
        more = start + len(rows) < total if isinstance(total, int) else len(rows) >= want
        result = compact(
            {
                key: rows,
                "count": len(rows),
                "total_entries": total,
                "total_pages": -(-total // want) if isinstance(total, int) else None,
                "page": start // want + 1 if offset is None else None,
                "per_page": want,
                "offset": start,
                "has_more": more,
                "next_page": start // want + 2 if more and offset is None else None,
                "next_offset": start + len(rows) if more else None,
            }
        )
        # Lets the response size cap continue right after the last record it returns.
        result["_resume"] = {"key": "next_offset", "start": start}
        return result
    raise SyncroError(502, f"Syncro kept changing the page size of {path}; try again.")


async def _read_window(
    client: SyncroClient, path: str, key: str, params: dict[str, Any], start: int, want: int, size: int
) -> tuple[list[dict], int | None] | int:
    """Read records [start, start + want) assuming ``size`` records per Syncro page.

    Returns (rows, total_entries), or the real page size when the response shows
    that ``size`` was wrong (the caller then retries with it).
    """
    rows: list[dict] = []
    total: int | None = None
    position = start
    while len(rows) < want:
        syncro_page, inside = divmod(position, size)
        items, meta = _page_payload(await client.get(path, compact({**params, "page": syncro_page + 1})), key)
        total_pages = meta.get("total_pages")
        actual = meta.get("per_page")
        last = not items or (isinstance(total_pages, int) and syncro_page + 1 >= total_pages)
        if not (isinstance(actual, int) and actual > 0) and not last and items:
            actual = len(items)  # a full, non-final page shows the real size
        if isinstance(actual, int) and actual > 0 and actual != size:
            return actual
        if isinstance(meta.get("total_entries"), int):
            total = meta["total_entries"]
        chunk = items[inside : inside + want - len(rows)]
        rows.extend(chunk)
        position += len(chunk)
        if last or len(items) <= inside:
            break
    return rows, total


async def filtered_page(
    client: SyncroClient,
    path: str,
    key: str,
    params: dict[str, Any],
    predicate: Predicate,
    page: int | None,
    per_page: int | None,
    offset: int | None,
    note: str,
) -> dict:
    """Up to ``per_page`` records matching ``predicate`` (a filter Syncro does not offer).

    Scanning starts at record ``offset`` (or the first record of Syncro page ``page``)
    and reads at most MAX_SCAN_PAGES Syncro pages. next_offset is the record right
    after the last match returned (or after the last record read when nothing more
    matched), so following it never skips or repeats a match - also when the size
    cap cuts the result, thanks to the per-match positions passed in ``_resume``.
    """
    if page is not None and offset is not None:
        raise ValueError("Pass page or offset, not both.")
    want = per_page or PAGE_SIZE
    size = NATIVE_PAGE_SIZES.get(path, PAGE_SIZE)
    for _ in range(3):  # re-run once the response reveals a different page size
        start = offset if offset is not None else ((page or 1) - 1) * size
        scan = await _scan(client, path, key, params, predicate, start, want, size)
        if isinstance(scan, int):
            size = scan
            continue
        matches, positions, end, last_read, total, first_page = scan
        more = end < total if isinstance(total, int) else not last_read
        result = compact(
            {
                key: matches,
                "count": len(matches),
                "per_page": want,
                "offset": start,
                "scanned_pages": f"{first_page}-{(max(end, start + 1) - 1) // size + 1}",
                "total_entries": total,
                "has_more": more,
                "next_page": end // size + 1 if more and end % size == 0 else None,
                "next_offset": end if more else None,
                "note": note,
            }
        )
        # Where to continue after each match, so a size-capped result resumes exactly.
        result["_resume"] = {"key": "next_offset", "positions": positions}
        return result
    raise SyncroError(502, f"Syncro kept changing the page size of {path}; try again.")


async def _scan(
    client: SyncroClient,
    path: str,
    key: str,
    params: dict[str, Any],
    predicate: Predicate,
    start: int,
    want: int,
    size: int,
) -> tuple[list[dict], list[int], int, bool, int | None, int] | int:
    """Scan from record ``start``; returns (matches, positions, end, last_read, total, first_page).

    ``end`` is the record offset to continue from; ``positions[i]`` is where to continue
    after ``matches[i]``. Returns the real page size instead when ``size`` was wrong.
    """
    matches: list[dict] = []
    positions: list[int] = []
    total: int | None = None
    syncro_page, inside = divmod(start, size)
    first_page = syncro_page + 1
    end = start
    last_read = False
    for _ in range(MAX_SCAN_PAGES):
        items, meta = _page_payload(await client.get(path, compact({**params, "page": syncro_page + 1})), key)
        total_pages = meta.get("total_pages")
        last_read = not items or (isinstance(total_pages, int) and syncro_page + 1 >= total_pages)
        actual = meta.get("per_page")
        if not (isinstance(actual, int) and actual > 0) and items and not last_read:
            actual = len(items)  # a full, non-final page shows the real size
        if isinstance(actual, int) and actual > 0 and actual != size:
            return actual
        if isinstance(meta.get("total_entries"), int):
            total = meta["total_entries"]
        base = syncro_page * size
        for index in range(inside, len(items)):
            end = base + index + 1
            if predicate(items[index]):
                matches.append(items[index])
                positions.append(end)
                if len(matches) >= want:
                    return matches, positions, end, last_read and index + 1 >= len(items), total, first_page
        end = max(end, base + len(items))
        if last_read or len(items) < size:
            if total is None:
                total = end
            return matches, positions, end, True, total, first_page
        syncro_page, inside = syncro_page + 1, 0
    return matches, positions, end, False, total, first_page


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
    """Search helper: up to ``want`` matching records from up to MAX_SCAN_PAGES pages (no paging)."""
    current = page or 1
    first, matches = current, []
    exhausted = False
    for _ in range(MAX_SCAN_PAGES):
        items, meta = _page_payload(await client.get(path, compact({**params, "page": current})), key)
        total_pages = meta.get("total_pages")
        page_size = meta.get("per_page")
        matches.extend(i for i in items if predicate(i))
        exhausted = (
            not items
            or (isinstance(total_pages, int) and current >= total_pages)
            or (isinstance(page_size, int) and len(items) < page_size)
        )
        if len(matches) >= want or exhausted:
            break
        current += 1
    return compact(
        {
            key: matches[:want],
            "count": min(len(matches), want),
            "scanned_pages": f"{first}-{current}",
            "has_more": len(matches) > want or not exhausted,
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
