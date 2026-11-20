"""Boundary tests: the MCP server must never send mail or other messages to customers.

Articles are limited to non-sending types (note, phone) and always internal. Replies exist only
as shared drafts that an agent reviews and sends manually in Zammad.
"""

import os
from collections.abc import Iterator
from typing import Any, NamedTuple
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_zammad.client import ZammadClient
from mcp_zammad.server import ZammadMCPServer

ARTICLE_DATA: dict[str, Any] = {
    "id": 1,
    "ticket_id": 1,
    "body": "Test article",
    "type": "note",
    "sender": "Agent",
    "internal": True,
    "created_by_id": 1,
    "updated_by_id": 1,
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-01T00:00:00Z",
}

TICKET_DATA: dict[str, Any] = {
    "id": 1,
    "number": "12345",
    "title": "Test Ticket",
    "group_id": 1,
    "state_id": 1,
    "priority_id": 2,
    "customer_id": 1,
    "created_by_id": 1,
    "updated_by_id": 1,
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-01T00:00:00Z",
}

ENV = {"ZAMMAD_URL": "https://test.zammad.com/api/v1", "ZAMMAD_HTTP_TOKEN": "token"}


class Harness(NamedTuple):
    """An MCP server wired to a controlled Zammad client double."""

    server: ZammadMCPServer
    zammad: Mock


@pytest.fixture
def harness() -> Iterator[Harness]:
    zammad = Mock()
    zammad.get_current_user.return_value = {"id": 1, "email": "agent@example.com"}
    zammad.add_article.return_value = ARTICLE_DATA
    zammad.create_ticket.return_value = TICKET_DATA
    with patch("mcp_zammad.server.ZammadClient", return_value=zammad):
        yield Harness(ZammadMCPServer(), zammad)


@pytest.fixture
def zammad_api() -> Iterator[MagicMock]:
    """A ZammadClient backed by a double of the zammad_py API."""
    with patch("mcp_zammad.client.ZammadAPI") as api_cls, patch.dict(os.environ, ENV, clear=True):
        api_cls.return_value.url = "https://test.zammad.com/api/v1/"
        yield api_cls.return_value


async def _call_expecting_error(server: ZammadMCPServer, name: str, arguments: dict[str, Any]) -> str:
    async with Client(server.mcp) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(name, arguments)
    return str(exc_info.value)


# --- zammad_add_article / zammad_update_ticket are not registered ------------------------------


@pytest.mark.parametrize("name", ["zammad_add_article", "zammad_update_ticket", "zammad_bulk_update_tickets"])
@pytest.mark.asyncio
async def test_article_capable_tools_are_not_registered(harness: Harness, name: str) -> None:
    async with Client(harness.server.mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert name not in names
    harness.zammad.add_article.assert_not_called()
    harness.zammad.update_ticket.assert_not_called()


# --- zammad_create_ticket (tool boundary) -------------------------------------------------------

TICKET_ARGS = {"title": "T", "group": "Support", "customer": "c@example.com", "article_body": "Hello"}


@pytest.mark.parametrize("article_type", ["email", "note", "phone"])
@pytest.mark.asyncio
async def test_create_ticket_rejects_article_type_field(harness: Harness, article_type: str) -> None:
    """The article type is fixed to phone; the tool does not accept the field at all."""
    message = await _call_expecting_error(
        harness.server, "zammad_create_ticket", {**TICKET_ARGS, "article_type": article_type}
    )
    assert "article_type" in message
    harness.zammad.create_ticket.assert_not_called()


@pytest.mark.parametrize("field", ["article_internal", "article_sender", "sender", "to", "cc", "subject"])
@pytest.mark.asyncio
async def test_create_ticket_rejects_mail_and_visibility_fields(harness: Harness, field: str) -> None:
    message = await _call_expecting_error(harness.server, "zammad_create_ticket", {**TICKET_ARGS, field: "x"})
    assert field in message
    harness.zammad.create_ticket.assert_not_called()


@pytest.mark.asyncio
async def test_create_ticket_schema_has_no_article_options(harness: Harness) -> None:
    async with Client(harness.server.mcp) as client:
        tools = {tool.name: tool.inputSchema for tool in await client.list_tools()}
    properties = set(tools["zammad_create_ticket"]["properties"])
    assert properties == {"title", "group", "customer", "article_body", "state", "priority"}


@pytest.mark.asyncio
async def test_create_ticket_passes_no_article_options_to_client(harness: Harness) -> None:
    async with Client(harness.server.mcp) as client:
        await client.call_tool("zammad_create_ticket", TICKET_ARGS)
    kwargs = harness.zammad.create_ticket.call_args.kwargs
    assert set(kwargs) == {"title", "group", "customer", "article_body", "state", "priority"}


# --- ZammadClient (last line of defence) --------------------------------------------------------


def test_client_add_article_always_internal(zammad_api: MagicMock) -> None:
    zammad_api.ticket_article.create.return_value = {"id": 1}
    ZammadClient().add_article(ticket_id=1, body="x", article_type="phone")
    payload = zammad_api.ticket_article.create.call_args[0][0]
    assert payload["internal"] is True
    assert payload["type"] == "phone"


@pytest.mark.parametrize("article_type", ["email", "sms", "twitter status", "web", ""])
def test_client_add_article_rejects_sending_types(zammad_api: MagicMock, article_type: str) -> None:
    with pytest.raises(ValueError, match="article_type"):
        ZammadClient().add_article(ticket_id=1, body="x", article_type=article_type)
    zammad_api.ticket_article.create.assert_not_called()


@pytest.mark.parametrize("field", ["internal", "subject", "to", "cc"])
def test_client_add_article_has_no_mail_parameters(zammad_api: MagicMock, field: str) -> None:
    with pytest.raises(TypeError):
        ZammadClient().add_article(ticket_id=1, body="x", **{field: "y"})  # type: ignore[arg-type]
    zammad_api.ticket_article.create.assert_not_called()


def test_client_create_ticket_is_internal_agent_phone(zammad_api: MagicMock) -> None:
    """Phone type, Agent sender, internal: Zammad delivers nothing, and the default
    "auto reply (on new tickets)" trigger (articles from sender Customer) does not match."""
    zammad_api.ticket.create.return_value = {"id": 1}
    ZammadClient().create_ticket(title="T", group="G", customer="c@example.com", article_body="b")
    article = zammad_api.ticket.create.call_args[0][0]["article"]
    assert article == {"body": "b", "type": "phone", "sender": "Agent", "internal": True}


@pytest.mark.parametrize("field", ["article_type", "article_internal", "article_sender"])
def test_client_create_ticket_has_no_article_parameters(zammad_api: MagicMock, field: str) -> None:
    with pytest.raises(TypeError):
        ZammadClient().create_ticket(
            title="T",
            group="G",
            customer="c@example.com",
            article_body="b",
            **{field: "email"},  # type: ignore[arg-type]
        )
    zammad_api.ticket.create.assert_not_called()


# --- zammad_update_ticket: no articles through custom_fields ------------------------------------

EMAIL_ARTICLE = {"type": "email", "internal": False, "to": "customer@example.com", "body": "hi"}


@pytest.mark.parametrize("key", ["article", "ARTICLE"])
def test_client_update_ticket_rejects_article_in_custom_fields(zammad_api: MagicMock, key: str) -> None:
    with pytest.raises(ValueError, match="article"):
        ZammadClient().update_ticket(ticket_id=1, custom_fields={key: EMAIL_ARTICLE})
    zammad_api.ticket.update.assert_not_called()


def test_client_update_ticket_keeps_plain_custom_fields(zammad_api: MagicMock) -> None:
    zammad_api.ticket.update.return_value = {"id": 1}
    ZammadClient().update_ticket(ticket_id=1, custom_fields={"region": "north"})
    assert zammad_api.ticket.update.call_args[0][1] == {"region": "north"}
