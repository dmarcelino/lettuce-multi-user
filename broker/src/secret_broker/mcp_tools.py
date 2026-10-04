"""The three MCP tools agents get. None of them adds, changes or deletes
secrets; that is the web UI's job, behind Cloudflare Access."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .config import MAX_WAIT_SECONDS
from .service import Broker

INSTRUCTIONS = (
    "The user's personal vault of secrets: addresses, company details, notes, memberships, ID documents, "
    "payment cards and logins. list_secrets shows each secret's name, type and public details. Private "
    "fields (an address text, a card number, a password...) need request_secret with a short purpose; "
    "unless the user set that secret to share without asking, they approve it on their phone first, and "
    "get_result then returns the values. Free-text secrets hold natural language: read them and use the parts "
    "the task needs. Ask only for what the task needs."
)


def build_server(broker: Broker) -> MCPServer:
    server = MCPServer(name="vault", instructions=INSTRUCTIONS)

    @server.tool(
        description="List the secrets in the user's vault: id, name, type, what the user said it may be used "
        "for, its public details (e.g. programme, document type, website, username) and the keys of its private "
        "fields, and whether using it needs the user's approval. Never includes private values.",
        annotations=ToolAnnotations(title="List secrets", read_only_hint=True, open_world_hint=False),
    )
    async def list_secrets() -> dict[str, Any]:
        return broker.list_secrets()

    # Not read-only on purpose: Lettuce's bridge then shows its own prompt too.
    # That prompt is a convenience; the vault's approval page is the gate.
    @server.tool(
        description="Ask for the private fields of one secret, e.g. the text of an address to fill in a "
        "delivery form. Give a short purpose the user will read before approving. fields lists private field "
        "keys from list_secrets; leave it out to ask for all of them. Secrets that need approval return an id "
        "for get_result; secrets the user allows without asking return the values directly.",
        annotations=ToolAnnotations(title="Request a secret", read_only_hint=False, open_world_hint=False),
    )
    async def request_secret(secret_id: str, purpose: str, fields: list[str] | None = None) -> dict[str, Any]:
        return await broker.request_secret(secret_id, purpose, fields)

    @server.tool(
        description="Get the answer to a request_secret: pending, denied, expired, or done with the values the "
        f"user agreed to share (they may withhold some fields). wait_seconds (0-{MAX_WAIT_SECONDS}) waits for "
        "the answer. Values are returned once.",
        annotations=ToolAnnotations(title="Get request result", read_only_hint=True, open_world_hint=False),
    )
    async def get_result(request_id: str, wait_seconds: float = 0) -> dict[str, Any]:
        return await broker.get_result(request_id, wait_seconds)

    return server
