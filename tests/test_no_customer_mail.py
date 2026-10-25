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


# --- zammad_add_article (tool boundary) ---------------------------------------------------------


@pytest.mark.asyncio
async def test_add_article_rejects_email_type(harness: Harness) -> None:
    message = await _call_expecting_error(
        harness.server, "zammad_add_article", {"ticket_id": 1, "body": "hi", "article_type": "email"}
    )
    assert "article_type" in message
    harness.zammad.add_article.assert_not_called()


@pytest.mark.parametrize("field", ["internal", "to", "cc", "subject"])
@pytest.mark.asyncio
async def test_add_article_rejects_mail_and_visibility_fields(harness: Harness, field: str) -> None:
    value: Any = False if field == "internal" else "x@example.com"
    message = await _call_expecting_error(
        harness.server, "zammad_add_article", {"ticket_id": 1, "body": "hi", field: value}
    )
    assert field in message
    harness.zammad.add_article.assert_not_called()


@pytest.mark.asyncio
async def test_add_article_schema_exposes_only_non_sending_types(harness: Harness) -> None:
    async with Client(harness.server.mcp) as client:
        tools = {tool.name: tool.inputSchema for tool in await client.list_tools()}
    properties = tools["zammad_add_article"]["properties"]
    assert properties["article_type"]["enum"] == ["note", "phone"]
    assert not {"internal", "to", "cc", "subject"} & set(properties)


@pytest.mark.asyncio
async def test_add_article_accepts_phone_type(harness: Harness) -> None:
    async with Client(harness.server.mcp) as client:
        await client.call_tool("zammad_add_article", {"ticket_id": 1, "body": "Call", "article_type": "phone"})
    assert harness.zammad.add_article.call_args.kwargs["article_type"] == "phone"


# --- zammad_create_ticket (tool boundary) -------------------------------------------------------

TICKET_ARGS = {"title": "T", "group": "Support", "customer": "c@example.com", "article_body": "Hello"}


@pytest.mark.asyncio
async def test_create_ticket_rejects_email_article_type(harness: Harness) -> None:
    message = await _call_expecting_error(
        harness.server, "zammad_create_ticket", {**TICKET_ARGS, "article_type": "email"}
    )
    assert "article_type" in message
    harness.zammad.create_ticket.assert_not_called()


@pytest.mark.asyncio
async def test_create_ticket_rejects_article_internal_flag(harness: Harness) -> None:
    message = await _call_expecting_error(
        harness.server, "zammad_create_ticket", {**TICKET_ARGS, "article_internal": False}
    )
    assert "article_internal" in message
    harness.zammad.create_ticket.assert_not_called()


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


def test_client_create_ticket_always_internal(zammad_api: MagicMock) -> None:
    zammad_api.ticket.create.return_value = {"id": 1}
    ZammadClient().create_ticket(title="T", group="G", customer="c@example.com", article_body="b")
    article = zammad_api.ticket.create.call_args[0][0]["article"]
    assert article == {"body": "b", "type": "note", "internal": True}


def test_client_create_ticket_rejects_sending_type(zammad_api: MagicMock) -> None:
    with pytest.raises(ValueError, match="article_type"):
        ZammadClient().create_ticket(
            title="T", group="G", customer="c@example.com", article_body="b", article_type="email"
        )
    zammad_api.ticket.create.assert_not_called()


def test_client_create_ticket_has_no_article_internal_parameter(zammad_api: MagicMock) -> None:
    with pytest.raises(TypeError):
        ZammadClient().create_ticket(
            title="T",
            group="G",
            customer="c@example.com",
            article_body="b",
            article_internal=False,  # type: ignore[call-arg]
        )
    zammad_api.ticket.create.assert_not_called()


# --- zammad_update_ticket: no articles through custom_fields ------------------------------------

EMAIL_ARTICLE = {"type": "email", "internal": False, "to": "customer@example.com", "body": "hi"}


@pytest.mark.parametrize("key", ["article", "Article", " article ", "article_attributes"])
@pytest.mark.asyncio
async def test_update_ticket_rejects_article_in_custom_fields(harness: Harness, key: str) -> None:
    message = await _call_expecting_error(
        harness.server, "zammad_update_ticket", {"ticket_id": 1, "custom_fields": {key: EMAIL_ARTICLE}}
    )
    assert "article" in message.lower()
    harness.zammad.update_ticket.assert_not_called()


@pytest.mark.parametrize("key", ["article", "ARTICLE"])
def test_client_update_ticket_rejects_article_in_custom_fields(zammad_api: MagicMock, key: str) -> None:
    with pytest.raises(ValueError, match="article"):
        ZammadClient().update_ticket(ticket_id=1, custom_fields={key: EMAIL_ARTICLE})
    zammad_api.ticket.update.assert_not_called()


def test_client_update_ticket_keeps_plain_custom_fields(zammad_api: MagicMock) -> None:
    zammad_api.ticket.update.return_value = {"id": 1}
    ZammadClient().update_ticket(ticket_id=1, custom_fields={"region": "north"})
    assert zammad_api.ticket.update.call_args[0][1] == {"region": "north"}
