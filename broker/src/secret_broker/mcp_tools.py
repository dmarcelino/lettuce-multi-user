"""The three MCP tools agents get. None of them adds, changes or deletes vault
items; that is the web UI's job, behind Cloudflare Access."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .config import MAX_WAIT_SECONDS
from .service import Broker

INSTRUCTIONS = (
    "The user's personal vault: addresses, company and tax details, memberships, payment cards, ID documents, "
    "logins and notes. list_items shows what exists (names and fields, no values). request_item asks for "
    "specific fields of one item and says why; unless the user set that item to share without asking, they "
    "approve it on their phone first. get_result then returns the values. Ask only for the fields the task needs."
)


def build_server(broker: Broker) -> MCPServer:
    server = MCPServer(name="vault", instructions=INSTRUCTIONS)

    @server.tool(
        description="List the items in the user's vault: id, name, type, what the user said it may be used for, "
        "its field keys and labels, and whether using it needs the user's approval. Never includes values.",
        annotations=ToolAnnotations(title="List vault items", read_only_hint=True, open_world_hint=False),
    )
    async def list_items() -> dict[str, Any]:
        return broker.list_items()

    # Not read-only on purpose: Lettuce's bridge then shows its own prompt too.
    # That prompt is a convenience; the vault's approval page is the gate.
    @server.tool(
        description="Ask for some fields of one vault item, e.g. the street, city and postal code of an address "
        "to fill in a delivery form. Give the field keys from list_items and a short purpose the user will read "
        "before approving. Items that need approval return an id for get_result; items the user allows without "
        "asking return the values directly. Ask only for what the task needs.",
        annotations=ToolAnnotations(title="Request vault item", read_only_hint=False, open_world_hint=False),
    )
    async def request_item(item_id: str, fields: list[str], purpose: str) -> dict[str, Any]:
        return await broker.request_item(item_id, fields, purpose)

    @server.tool(
        description="Get the answer to a request_item: pending, denied, expired, or done with the values the "
        f"user agreed to share (they may withhold some fields). wait_seconds (0-{MAX_WAIT_SECONDS}) waits for "
        "the answer. Values are returned once.",
        annotations=ToolAnnotations(title="Get vault request result", read_only_hint=True, open_world_hint=False),
    )
    async def get_result(request_id: str, wait_seconds: float = 0) -> dict[str, Any]:
        return await broker.get_result(request_id, wait_seconds)

    return server
