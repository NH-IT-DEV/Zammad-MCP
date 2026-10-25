"""Boundary tests: replies to customers exist only as Zammad shared drafts and are never sent."""

import os
from collections.abc import Iterator
from typing import NamedTuple
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastmcp import Client

from mcp_zammad.client import ZammadAPIError, ZammadClient
from mcp_zammad.server import ZammadMCPServer

ENV = {"ZAMMAD_URL": "https://test.zammad.com/api/v1", "ZAMMAD_HTTP_TOKEN": "token"}
DRAFT_URL = "https://test.zammad.com/api/v1/tickets/5/shared_draft"


class Harness(NamedTuple):
    """An MCP server wired to a controlled Zammad client double."""

    server: ZammadMCPServer
    zammad: Mock


@pytest.fixture
def harness() -> Iterator[Harness]:
    zammad = Mock()
    zammad.get_current_user.return_value = {"id": 1, "email": "agent@example.com"}
    zammad.create_reply_draft.return_value = {"shared_draft_id": 7}
    with patch("mcp_zammad.server.ZammadClient", return_value=zammad):
        yield Harness(ZammadMCPServer(), zammad)


@pytest.fixture
def zammad_api() -> Iterator[MagicMock]:
    """A ZammadClient backed by a double of the zammad_py API."""
    with patch("mcp_zammad.client.ZammadAPI") as api_cls, patch.dict(os.environ, ENV, clear=True):
        api_cls.return_value.url = "https://test.zammad.com/api/v1/"
        yield api_cls.return_value


def _client_answering_put(status_code: int, body: dict[str, object], text: str = "") -> tuple[ZammadClient, Mock]:
    """Build a client whose session answers every PUT with the given response."""
    client = ZammadClient()
    session = Mock()
    session.put.return_value = Mock(status_code=status_code, url=DRAFT_URL, text=text, json=Mock(return_value=body))
    client.api.session = session
    return client, session


@pytest.mark.asyncio
async def test_tool_is_registered_with_flat_schema(harness: Harness) -> None:
    async with Client(harness.server.mcp) as client:
        tools = {tool.name: tool.inputSchema for tool in await client.list_tools()}
    schema = tools["zammad_create_reply_draft"]
    assert set(schema["properties"]) == {"ticket_id", "body", "subject", "to", "cc"}
    assert schema["required"] == ["ticket_id", "body"]


@pytest.mark.asyncio
async def test_tool_creates_draft_and_returns_id(harness: Harness) -> None:
    arguments = {"ticket_id": 5, "body": "Hallo <Kunde>\nViele Grüße", "subject": "Re: T"}
    async with Client(harness.server.mcp) as client:
        result = await client.call_tool("zammad_create_reply_draft", arguments)
    harness.zammad.create_reply_draft.assert_called_once_with(
        ticket_id=5, body="Hallo &lt;Kunde&gt;<br>Viele Grüße", subject="Re: T", to=None, cc=None
    )
    assert result.structured_content["shared_draft_id"] == 7
    assert result.structured_content["ticket_id"] == 5


def test_client_puts_shared_draft_without_sending(zammad_api: MagicMock) -> None:
    client, session = _client_answering_put(201, {"shared_draft_id": 7, "assets": {}})

    result = client.create_reply_draft(ticket_id=5, body="Hi", subject="Re: T", to="c@example.com")

    assert result["shared_draft_id"] == 7
    zammad_api.ticket_article.create.assert_not_called()
    session.post.assert_not_called()
    assert session.put.call_args[0][0] == DRAFT_URL
    payload = session.put.call_args.kwargs["json"]
    assert payload["form_id"]
    assert payload["ticket_attributes"] == {}
    article = payload["new_article"]
    assert (article["body"], article["subject"], article["to"]) == ("Hi", "Re: T", "c@example.com")
    assert (article["content_type"], article["ticket_id"], article["type"]) == ("text/html", 5, "email")


def test_client_omits_unset_optional_fields(zammad_api: MagicMock) -> None:
    client, session = _client_answering_put(201, {"shared_draft_id": 7})

    client.create_reply_draft(ticket_id=5, body="Hi")

    article = session.put.call_args.kwargs["json"]["new_article"]
    assert not {"subject", "to", "cc"} & set(article)


def test_client_raises_loudly_on_http_error(zammad_api: MagicMock) -> None:
    client, _ = _client_answering_put(422, {}, text="Shared drafts are disabled")

    with pytest.raises(ZammadAPIError, match=r"422.*Shared drafts are disabled"):
        client.create_reply_draft(ticket_id=5, body="Hi")
