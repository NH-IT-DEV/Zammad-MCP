"""Boundary tests: the MCP server is read-only except for an explicit allowlist of write tools.

Permitted writes: creating phone tickets (zammad_create_ticket) and storing reply drafts
(zammad_create_reply_draft). Every other write tool is never registered, including write tools
that upstream adds later (fail closed).
"""

import asyncio
import re
from collections.abc import Iterator
from unittest.mock import Mock, patch

import pytest
from fastmcp import Client
from mcp.types import ToolAnnotations

from mcp_zammad.server import PERMITTED_WRITE_TOOLS, ZammadMCPServer

READ_TOOLS = {
    "zammad_search_tickets",
    "zammad_get_ticket",
    "zammad_get_article_attachments",
    "zammad_download_attachment",
    "zammad_get_user",
    "zammad_search_users",
    "zammad_get_organization",
    "zammad_search_organizations",
    "zammad_get_current_user",
    "zammad_get_ticket_stats",
    "zammad_list_groups",
    "zammad_list_ticket_states",
    "zammad_list_ticket_priorities",
    "zammad_list_tags",
    "zammad_get_ticket_tags",
    "zammad_list_knowledge_bases",
    "zammad_get_knowledge_base",
    "zammad_get_kb_category",
    "zammad_list_kb_answers",
    "zammad_search_kb_answers",
    "zammad_get_kb_answer",
    "zammad_list_events",
}

WRITE_TOOLS = {"zammad_create_ticket", "zammad_create_reply_draft"}

BLOCKED_TOOLS = {
    "zammad_update_ticket",
    "zammad_bulk_update_tickets",
    "zammad_add_article",
    "zammad_merge_tickets",
    "zammad_add_ticket_tag",
    "zammad_remove_ticket_tag",
    "zammad_export_tickets",
    "zammad_create_user",
}


@pytest.fixture
def server() -> Iterator[ZammadMCPServer]:
    with patch("mcp_zammad.server.ZammadClient", return_value=Mock()):
        yield ZammadMCPServer()


def test_permitted_write_tools_constant() -> None:
    assert PERMITTED_WRITE_TOOLS == WRITE_TOOLS


@pytest.mark.asyncio
async def test_registered_tools_are_exactly_reads_plus_permitted_writes(server: ZammadMCPServer) -> None:
    async with Client(server.mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == READ_TOOLS | WRITE_TOOLS


@pytest.mark.asyncio
async def test_only_permitted_tools_are_not_read_only(server: ZammadMCPServer) -> None:
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
    writes = {t.name for t in tools if not (t.annotations and t.annotations.readOnlyHint is True)}
    assert writes == WRITE_TOOLS


@pytest.mark.parametrize("name", sorted(BLOCKED_TOOLS))
@pytest.mark.asyncio
async def test_blocked_tool_cannot_be_called(server: ZammadMCPServer, name: str) -> None:
    async with Client(server.mcp) as client:
        result = await client.call_tool(name, {}, raise_on_error=False)
    assert result.is_error


@pytest.mark.asyncio
async def test_no_tool_or_prompt_points_to_a_blocked_tool(server: ZammadMCPServer) -> None:
    """Descriptions must not steer the model towards tools that do not exist."""
    pattern = re.compile("|".join(sorted(BLOCKED_TOOLS, key=len, reverse=True)))
    async with Client(server.mcp) as client:
        texts = {f"tool {t.name}": t.description or "" for t in await client.list_tools()}
        for prompt in await client.list_prompts():
            rendered = await client.get_prompt(prompt.name, _prompt_args(prompt.name))
            texts[f"prompt {prompt.name}"] = " ".join(getattr(m.content, "text", "") for m in rendered.messages)
    offenders = {where: pattern.findall(text) for where, text in texts.items() if pattern.search(text)}
    assert offenders == {}


def _prompt_args(name: str) -> dict[str, str]:
    return {"ticket_id": "1"} if name != "escalation_summary" else {}


def test_guard_blocks_unlisted_write_tool_added_later(server: ZammadMCPServer) -> None:
    """A write tool registered after setup (e.g. from an upstream merge) is not exposed."""

    @server.mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, title="New Upstream Write"))
    def zammad_new_upstream_write() -> str:
        return "written"

    @server.mcp.tool()
    def zammad_unannotated_tool() -> str:
        return "written"

    @server.mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, title="New Read"))
    def zammad_new_read() -> str:
        return "read"

    async def names() -> set[str]:
        async with Client(server.mcp) as client:
            return {tool.name for tool in await client.list_tools()}

    registered = asyncio.run(names())
    assert "zammad_new_upstream_write" not in registered
    assert "zammad_unannotated_tool" not in registered
    assert "zammad_new_read" in registered


def test_guard_blocks_renamed_write_tool(server: ZammadMCPServer) -> None:
    """Passing an allowlisted name explicitly does not smuggle in a different write tool."""

    @server.mcp.tool(name="zammad_sneaky", annotations=ToolAnnotations(readOnlyHint=False))
    def zammad_create_ticket() -> str:
        return "written"

    async def names() -> set[str]:
        async with Client(server.mcp) as client:
            return {tool.name for tool in await client.list_tools()}

    assert "zammad_sneaky" not in asyncio.run(names())
