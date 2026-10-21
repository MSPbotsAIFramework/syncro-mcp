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
"""Auth, retries, paging / filtering and request shapes against a mocked Syncro API."""

import json

import httpx
import pytest

from syncro_mcp import api_client
from syncro_mcp.api_client import SyncroClient, SyncroCredentials, SyncroError
from syncro_mcp.server import _credentials_var, create_mcp_server

CREDS = SyncroCredentials(subdomain="yourcompany", api_key="key-123")
META = {"total_pages": 3, "total_entries": 60, "per_page": 25, "page": 1}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(api_client.asyncio, "sleep", instant)


class Recorder:
    """Mock transport: replies by "METHOD path" or path below /api/v1/."""

    def __init__(self, routes=None, default=None):
        self.requests: list[httpx.Request] = []
        self.routes = routes or {}
        self.default = default if default is not None else {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v1/")
        reply = self.routes.get(f"{request.method} {path}", self.routes.get(path, self.default))
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=reply)

    def calls(self, path: str, method: str | None = None) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == f"/api/v1/{path}" and (method is None or r.method == method)]


def body(request: httpx.Request):
    return json.loads(request.content) if request.content else None


async def _call(monkeypatch, recorder: Recorder, tool: str, args: dict):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    monkeypatch.setattr(api_client, "_get_shared_http_client", lambda: http)
    ctx = _credentials_var.set(CREDS)
    try:
        content, _ = await create_mcp_server().call_tool(tool, args)
    finally:
        _credentials_var.reset(ctx)
    return json.loads(content[0].text)


def _pages(key: str, pages: list[list[dict]]):
    """A route returning one of ``pages`` per requested page number."""

    def reply(request: httpx.Request) -> httpx.Response:
        number = int(request.url.params.get("page", "1"))
        items = pages[number - 1] if number <= len(pages) else []
        return httpx.Response(200, json={key: items, "meta": {"total_pages": len(pages), "page": number, "per_page": 25}})

    return reply


# ---- auth and transport ----------------------------------------------------


async def test_key_is_sent_as_bearer_not_in_the_url():
    rec = Recorder({"me": {"user_name": "Joe"}})
    client = SyncroClient(CREDS, httpx.AsyncClient(transport=httpx.MockTransport(rec)))
    await client.get("me")
    request = rec.requests[0]
    assert str(request.url) == "https://yourcompany.syncromsp.com/api/v1/me"
    assert request.headers["Authorization"] == "Bearer key-123"
    assert "key-123" not in str(request.url)


async def test_401_names_the_headers(monkeypatch):
    rec = Recorder({"me": lambda r: httpx.Response(401, json={"error": "Unauthorized"})})
    result = await _call(monkeypatch, rec, "syncro_status", {})
    assert result["error"]["code"] == "unauthorized"
    assert "X-Syncro-API-Key" in result["error"]["message"]


async def test_get_retries_5xx_then_succeeds():
    replies = iter([httpx.Response(502), httpx.Response(200, json={"tickets": []})])
    rec = Recorder({"tickets": lambda r: next(replies)})
    client = SyncroClient(CREDS, httpx.AsyncClient(transport=httpx.MockTransport(rec)))
    assert await client.get("tickets") == {"tickets": []}
    assert len(rec.requests) == 2


async def test_post_is_not_retried_on_5xx():
    rec = Recorder({"POST tickets": lambda r: httpx.Response(500, json={"message": "boom"})})
    client = SyncroClient(CREDS, httpx.AsyncClient(transport=httpx.MockTransport(rec)))
    with pytest.raises(SyncroError) as err:
        await client.post("tickets", {"subject": "x"})
    assert "boom" in err.value.message
    assert len(rec.requests) == 1


async def test_validation_errors_are_surfaced(monkeypatch):
    rec = Recorder({"POST contacts": lambda r: httpx.Response(422, json={"errors": ["Customer must exist"]})})
    result = await _call(monkeypatch, rec, "syncro_contacts_create", {"customer_id": 9, "name": "Jane"})
    assert result["error"]["code"] == "invalid_argument"
    assert "Customer must exist" in result["error"]["message"]


# ---- lists -----------------------------------------------------------------


async def test_tickets_list_maps_filters_to_api_names(monkeypatch):
    rec = Recorder({"tickets": {"tickets": [{"id": 1}, {"id": 2}], "meta": META}})
    args = {"customer_id": 5, "status": "New", "since": "2026-06-01T08:00:00Z", "page": 2, "per_page": 1}
    result = await _call(monkeypatch, rec, "syncro_tickets_list", args)
    params = rec.requests[-1].url.params
    # page 2 of 1-record pages is the second record overall, i.e. on Syncro's page 1.
    assert (params["customer_id"], params["status"], params["since_updated_at"], params["page"]) == ("5", "New", "2026-06-01", "1")
    assert "per_page" not in params
    assert result == {
        "tickets": [{"id": 2}], "count": 1, "total_entries": 60, "total_pages": 60, "page": 2, "per_page": 1,
        "offset": 1, "has_more": True, "next_page": 3, "next_offset": 2,
    }


async def test_tickets_resolved_filter_scans_pages(monkeypatch):
    page1 = [{"id": i, "resolved_at": None} for i in range(25)]
    page2 = [{"id": 100, "resolved_at": "2026-06-02"}, {"id": 101, "resolved_at": None}]
    rec = Recorder({"tickets": _pages("tickets", [page1, page2])})
    result = await _call(monkeypatch, rec, "syncro_tickets_list", {"resolved": True})
    assert [t["id"] for t in result["tickets"]] == [100]
    assert result["scanned_pages"] == "1-2"
    assert "next_page" not in result
    assert len(rec.requests) == 2


async def test_bad_since_is_rejected(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "syncro_tickets_list", {"since": "yesterday"})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_customers_phone_is_sent_as_search(monkeypatch):
    rec = Recorder({"customers": {"customers": [], "meta": META}})
    await _call(monkeypatch, rec, "syncro_customers_list", {"phone": "555-0100", "include_disabled": True})
    params = rec.requests[0].url.params
    assert (params["query"], params["include_disabled"]) == ("555-0100", "true")


async def test_customers_search_limit(monkeypatch):
    rec = Recorder({"customers": _pages("customers", [[{"id": i} for i in range(25)], [{"id": 99}]])})
    result = await _call(monkeypatch, rec, "syncro_customers_search", {"query": "yourcompany", "limit": 26})
    assert result["count"] == 26
    assert rec.requests[0].url.params["query"] == "yourcompany"


async def test_assets_serial_is_matched_exactly(monkeypatch):
    assets = [{"id": 1, "asset_serial": "ABC123"}, {"id": 2, "asset_serial": "ABC1234"}]
    rec = Recorder({"customer_assets": _pages("assets", [assets])})
    result = await _call(monkeypatch, rec, "syncro_assets_list", {"asset_serial": "abc123", "customer_id": 3})
    params = rec.requests[0].url.params
    assert (params["query"], params["customer_id"]) == ("abc123", "3")
    assert [a["id"] for a in result["assets"]] == [1]


async def test_contacts_query_filters_locally(monkeypatch):
    contacts = [{"id": 1, "name": "Jane Doe", "email": "jane@example.com"}, {"id": 2, "name": "Bob", "email": "bob@example.com"}]
    rec = Recorder({"contacts": _pages("contacts", [contacts])})
    result = await _call(monkeypatch, rec, "syncro_contacts_list", {"customer_id": 4, "query": "JANE"})
    assert [c["id"] for c in result["contacts"]] == [1]
    assert "query" not in rec.requests[0].url.params


async def test_invoices_status_mapping(monkeypatch):
    rec = Recorder({"invoices": {"invoices": [], "meta": META}})
    await _call(monkeypatch, rec, "syncro_invoices_list", {"status": "paid", "since_date": "2026-01-01"})
    params = rec.requests[0].url.params
    assert (params["paid"], params["since_updated_at"]) == ("true", "2026-01-01")
    unsupported = await _call(monkeypatch, rec, "syncro_invoices_list", {"status": "void"})
    assert unsupported["error"]["code"] == "invalid_argument"


async def test_invoices_partial_and_customer_filter(monkeypatch):
    invoices = [
        {"id": 1, "customer_id": 7, "amount_paid": 10.0, "balance_due": "5.0"},
        {"id": 2, "customer_id": 7, "amount_paid": 0.0, "balance_due": "15.0"},
        {"id": 3, "customer_id": 8, "amount_paid": 10.0, "balance_due": "5.0"},
    ]
    rec = Recorder({"invoices": _pages("invoices", [invoices])})
    result = await _call(monkeypatch, rec, "syncro_invoices_list", {"status": "partial", "customer_id": 7})
    assert [i["id"] for i in result["invoices"]] == [1]
    assert rec.requests[0].url.params["unpaid"] == "true"


# ---- writes ----------------------------------------------------------------


async def test_ticket_create_body(monkeypatch):
    rec = Recorder({"POST tickets": {"ticket": {"id": 50, "number": 1050}}})
    args = {"customer_id": 5, "subject": "Printer down", "problem_type": "Hardware", "comment_body": "Office printer offline"}
    result = await _call(monkeypatch, rec, "syncro_tickets_create", args)
    assert body(rec.calls("tickets", "POST")[0]) == {
        "customer_id": 5,
        "subject": "Printer down",
        "problem_type": "Hardware",
        "comments_attributes": [{"subject": "Initial Issue", "body": "Office printer offline", "hidden": False}],
    }
    assert result == {"id": 50, "number": 1050}


async def test_ticket_update_uses_put_with_given_fields(monkeypatch):
    rec = Recorder({"PUT tickets/50": {"ticket": {"id": 50}}})
    await _call(monkeypatch, rec, "syncro_tickets_update", {"ticket_id": 50, "status": "Resolved"})
    assert body(rec.calls("tickets/50", "PUT")[0]) == {"status": "Resolved"}
    empty = await _call(monkeypatch, Recorder(), "syncro_tickets_update", {"ticket_id": 50})
    assert empty["error"]["code"] == "invalid_argument"


async def test_add_comment_body(monkeypatch):
    rec = Recorder({"POST tickets/50/comment": {"comment": {"id": 2}}})
    await _call(monkeypatch, rec, "syncro_tickets_add_comment", {"ticket_id": 50, "body": "Checked cables", "hidden": True})
    assert body(rec.calls("tickets/50/comment", "POST")[0]) == {"subject": "Update", "body": "Checked cables", "hidden": True}


async def test_customer_create_needs_a_name(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "syncro_customers_create", {"email": "x@example.com"})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_invoice_create_defaults_date_and_maps_notes(monkeypatch):
    rec = Recorder({"POST invoices": {"invoice": {"id": 6}}})
    await _call(monkeypatch, rec, "syncro_invoices_create", {"customer_id": 2, "notes": "June work", "due_date": "2026-07-01"})
    sent = body(rec.calls("invoices", "POST")[0])
    assert sent["customer_id"] == 2 and sent["note"] == "June work" and sent["due_date"] == "2026-07-01"
    assert len(sent["date"]) == 10 and "number" not in sent


async def test_invoice_email(monkeypatch):
    rec = Recorder({"POST invoices/6/email": {}})
    result = await _call(monkeypatch, rec, "syncro_invoices_email", {"invoice_id": 6})
    assert result["status"] == "sent"
    assert body(rec.calls("invoices/6/email", "POST")[0]) == {}
    assert "ignored_params" not in result


async def test_invoice_email_accepts_and_ignores_cc_subject_message(monkeypatch):
    rec = Recorder({"POST invoices/6/email": {}})
    args = {"invoice_id": 6, "cc_emails": "billing@example.com", "subject": "Your invoice", "message": "Thanks"}
    result = await _call(monkeypatch, rec, "syncro_invoices_email", args)
    assert result["status"] == "sent"
    assert result["ignored_params"] == ["cc_emails", "subject", "message"]
    assert body(rec.calls("invoices/6/email", "POST")[0]) == {}


async def test_gets_unwrap_their_record(monkeypatch):
    rec = Recorder({"tickets/1": {"ticket": {"id": 1, "comments": []}}, "customer_assets/4": {"asset": {"id": 4}}})
    assert await _call(monkeypatch, rec, "syncro_tickets_get", {"ticket_id": 1}) == {"id": 1, "comments": []}
    assert await _call(monkeypatch, rec, "syncro_assets_get", {"asset_id": 4}) == {"id": 4}


async def test_status_reports_user(monkeypatch):
    me = {"user_name": "Joe", "user_email": "joe@example.com", "user_id": 1, "admin": True, "permissions": {"ticket": {"read": True}}}
    result = await _call(monkeypatch, Recorder({"me": me}), "syncro_status", {})
    assert result["connected"] is True and result["subdomain"] == "yourcompany"
    assert result["user"]["user_name"] == "Joe"


# ---- paging never skips or repeats records (PRD-20961) -----------------------


def _syncro(key: str, rows: list[dict], size: int, report_size: bool = True):
    """A Syncro list endpoint with a fixed page size that ignores per_page."""

    def reply(request: httpx.Request) -> httpx.Response:
        number = int(request.url.params.get("page", "1"))
        pages = max(1, -(-len(rows) // size))
        meta = {"total_pages": pages, "total_entries": len(rows), "page": number}
        if report_size:
            meta["per_page"] = size
        return httpx.Response(200, json={key: rows[(number - 1) * size : number * size], "meta": meta})

    return reply


async def _follow(monkeypatch, tool: str, key: str, route, args: dict) -> list:
    """Call ``tool`` and follow its own paging fields to the end."""
    seen: list = []
    call = dict(args)
    for _ in range(200):
        out = await _call(monkeypatch, Recorder({key if key != "assets" else "customer_assets": route}), tool, call)
        assert out["count"] == len(out[key])
        seen += [row["id"] for row in out[key]]
        if out.get("next_page"):
            call = {**args, "page": out["next_page"]}
        elif out.get("next_offset") is not None:
            call = {k: v for k, v in args.items() if k != "page"} | {"offset": out["next_offset"]}
        else:
            assert not out.get("has_more")
            return seen
    raise AssertionError("paging did not finish")


CUSTOMERS = [{"id": i, "business_name": f"Customer {i}"} for i in range(1, 944)]


@pytest.mark.parametrize("args", [{}, {"per_page": 20}, {"per_page": 7}, {"per_page": 100}], ids=["default", "20", "7", "100"])
async def test_customers_paging_returns_all_943_once(monkeypatch, args):
    seen = await _follow(monkeypatch, "syncro_customers_list", "customers", _syncro("customers", CUSTOMERS, 100), args)
    assert seen == list(range(1, 944))


async def test_contacts_and_filtered_tickets_page_completely(monkeypatch):
    contacts = [{"id": i} for i in range(1, 1309)]
    seen = await _follow(monkeypatch, "syncro_contacts_list", "contacts", _syncro("contacts", contacts, 50), {"per_page": 20})
    assert seen == list(range(1, 1309))
    tickets = [{"id": i} for i in range(1, 8)]  # e.g. a since filter matching 7 tickets
    seen = await _follow(monkeypatch, "syncro_tickets_list", "tickets", _syncro("tickets", tickets, 50), {"since": "2026-10-15", "per_page": 3})
    assert seen == list(range(1, 8))


async def test_has_more_reflects_records_actually_returned(monkeypatch):
    tickets = [{"id": i} for i in range(1, 8)]
    out = await _call(monkeypatch, Recorder({"tickets": _syncro("tickets", tickets, 50)}), "syncro_tickets_list", {"per_page": 3})
    assert (out["count"], out["has_more"], out["next_page"], out["next_offset"], out["total_pages"]) == (3, True, 2, 3, 3)


async def test_page_size_is_learned_when_meta_omits_it(monkeypatch):
    # Built-in size for customers is 100; this account's endpoint pages by 40 and does not say so.
    seen = await _follow(monkeypatch, "syncro_customers_list", "customers", _syncro("customers", CUSTOMERS, 40, report_size=False), {"per_page": 30})
    assert seen == list(range(1, 944))


async def test_large_records_cut_by_size_limit_continue_without_gaps(monkeypatch):
    big = [{"id": i, "notes": [f"line {j}" for j in range(5)], **{f"f{j}": f"value-{j:04d}" for j in range(60)}} for i in range(1, 251)]
    seen = await _follow(monkeypatch, "syncro_customers_list", "customers", _syncro("customers", big, 100), {})
    assert seen == list(range(1, 251))


async def test_offset_and_page_together_are_rejected(monkeypatch):
    out = await _call(monkeypatch, Recorder({"customers": _syncro("customers", CUSTOMERS, 100)}), "syncro_customers_list", {"page": 2, "offset": 5})
    assert out["error"]["code"] == "invalid_argument"


async def test_scan_filter_keeps_every_match_on_the_last_page_read(monkeypatch):
    page1 = [{"id": i, "resolved_at": "2026-06-02"} for i in range(1, 51)]
    page2 = [{"id": i, "resolved_at": "2026-06-02"} for i in range(51, 61)]
    rec = Recorder({"tickets": _syncro("tickets", page1 + page2, 50)})
    out = await _call(monkeypatch, rec, "syncro_tickets_list", {"resolved": True, "per_page": 20})
    assert [t["id"] for t in out["tickets"]] == list(range(1, 51))  # nothing on page 1 is dropped
    assert out["next_page"] == 2 and out["has_more"] is True


async def test_search_still_honours_its_limit(monkeypatch):
    rec = Recorder({"customers": _syncro("customers", CUSTOMERS, 100)})
    out = await _call(monkeypatch, rec, "syncro_customers_search", {"query": "Customer", "limit": 5})
    assert [c["id"] for c in out["customers"]] == [1, 2, 3, 4, 5]
    assert out["has_more"] is True
