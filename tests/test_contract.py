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
"""Backwards compatibility with the tool contract the gateway already knows.

contract_baseline.json is the previous version's tools/list (tool -> params, required).
Every baseline tool must still exist, accept every baseline parameter, and not
require anything the baseline did not require.
"""

import asyncio
import json
from pathlib import Path

import pytest

from syncro_mcp.server import create_mcp_server

BASELINE: dict[str, dict[str, list[str]]] = json.loads((Path(__file__).parent / "contract_baseline.json").read_text(encoding="utf-8"))["int_baseline"]


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(create_mcp_server().list_tools())}


@pytest.mark.parametrize("name", sorted(BASELINE))
def test_baseline_tool_exists(tools, name):
    assert name in tools


@pytest.mark.parametrize("name", sorted(BASELINE))
def test_baseline_params_still_accepted(tools, name):
    params = set(tools[name].inputSchema.get("properties", {}))
    missing = set(BASELINE[name]["params"]) - params
    assert not missing, f"{name} lost params: {sorted(missing)}"


@pytest.mark.parametrize("name", sorted(BASELINE))
def test_no_new_required_params(tools, name):
    required = set(tools[name].inputSchema.get("required", []))
    extra = required - set(BASELINE[name]["required"])
    assert not extra, f"{name} newly requires: {sorted(extra)}"
