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
"""tools/list snapshot and error-envelope mapping. No network access."""

import asyncio
import json

import pytest

from syncro_mcp.api_client import SyncroError
from syncro_mcp.server import INSTRUCTIONS, create_mcp_server

# Tool names are a public contract: existing agents and tenant connections
# depend on them. Changing this snapshot is a breaking change.
EXPECTED_TOOLS: dict[str, set[str]] = {
    "syncro_navigate": {"domain"},
    "syncro_status": set(),
    "syncro_customers_list": set(),
    "syncro_customers_get": {"customer_id"},
    "syncro_customers_search": {"query"},
    "syncro_customers_create": set(),
    "syncro_tickets_list": set(),
    "syncro_tickets_get": {"ticket_id"},
    "syncro_tickets_create": {"customer_id", "subject"},
    "syncro_tickets_update": {"ticket_id"},
    "syncro_tickets_add_comment": {"ticket_id", "body"},
    "syncro_assets_list": set(),
    "syncro_assets_get": {"asset_id"},
    "syncro_assets_search": set(),
    "syncro_contacts_list": set(),
    "syncro_contacts_get": {"contact_id"},
    "syncro_contacts_create": {"customer_id", "name"},
    "syncro_invoices_list": set(),
    "syncro_invoices_get": {"invoice_id"},
    "syncro_invoices_create": {"customer_id"},
    "syncro_invoices_email": {"invoice_id"},
}

WRITE_TOOLS = {
    "syncro_customers_create",
    "syncro_tickets_create",
    "syncro_tickets_update",
    "syncro_tickets_add_comment",
    "syncro_contacts_create",
    "syncro_invoices_create",
    "syncro_invoices_email",
}


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(create_mcp_server().list_tools())}


def test_tool_names_are_stable(tools):
    assert set(tools) == set(EXPECTED_TOOLS)


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_required_params(tools, name):
    assert set(tools[name].inputSchema.get("required", [])) == EXPECTED_TOOLS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_description_budget(tools, name):
    description = (tools[name].description or "").strip()
    assert description, f"{name} has no description"
    assert len(description) <= 500
    assert len(description.splitlines()[0]) <= 100


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_annotations_match_side_effects(tools, name):
    annotations = tools[name].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is (name not in WRITE_TOOLS)
    assert not annotations.destructiveHint


@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
def test_write_tools_say_when_to_use_them(tools, name):
    description = tools[name].description or ""
    assert "explicitly asks" in description or "explicit request" in description


def test_invoice_email_params(tools):
    assert set(tools["syncro_invoices_email"].inputSchema["properties"]) == {"invoice_id", "cc_emails", "subject", "message"}


def test_instructions_budget():
    assert len(INSTRUCTIONS) <= 1500


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "unauthorized", False),
        (403, "unauthorized", False),
        (404, "not_found", False),
        (422, "invalid_argument", False),
        (429, "rate_limited", True),
        (500, "upstream_error", True),
        (0, "upstream_error", True),
    ],
)
def test_error_envelope(status, code, retryable):
    envelope = json.loads(SyncroError(status, "boom").to_envelope())
    assert envelope == {"error": {"code": code, "message": "boom", "retryable": retryable}}


def test_tool_without_credentials_returns_not_configured():
    content, _ = asyncio.run(create_mcp_server().call_tool("syncro_status", {}))
    assert json.loads(content[0].text)["error"]["code"] == "not_configured"


def test_navigate_needs_no_credentials():
    content, _ = asyncio.run(create_mcp_server().call_tool("syncro_navigate", {"domain": "assets"}))
    assert json.loads(content[0].text)["tools"][0] == "syncro_assets_list"
