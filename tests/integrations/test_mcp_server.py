"""Read-only MCP transport and real JWT authorization boundaries."""

import asyncio
import json
import time
from types import SimpleNamespace

import jwt
import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from patchshuttle import Job, execute_plan
from patchshuttle.integrations.mcp_server import (
    SCOPE,
    OwnerTokenVerifier,
    ServerConfig,
    create_server,
    main,
)
from patchshuttle.planner import plan_job
from patchshuttle.workspace import init_workspace


@pytest.fixture
def config():
    return ServerConfig(
        issuer="https://identity.example/",
        jwks_url="https://identity.example/keys",
        resource_url="https://history.example/mcp",
        subject="owner-123",
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https:///foo",
        "https://x@y",
        "https://x:y@z",
        "https://x?q=1",
        "https://x/#a",
    ],
)
def test_config_rejects_unsafe_urls(config, url):
    with pytest.raises(ValueError):
        ServerConfig.model_validate({**config.model_dump(), "issuer": url})


def signed_verifier(config, monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = OwnerTokenVerifier(config)
    monkeypatch.setattr(
        verifier.keys,
        "get_signing_key_from_jwt",
        lambda token: SimpleNamespace(key=private.public_key()),
    )
    claims = {
        "iss": config.issuer,
        "aud": config.resource_url,
        "sub": config.subject,
        "exp": int(time.time()) + 300,
        "scope": SCOPE,
    }
    return verifier, private, claims


def test_real_signed_owner_token(config, monkeypatch):
    verifier, private, claims = signed_verifier(config, monkeypatch)
    token = jwt.encode(claims, private, algorithm="RS256")
    result = asyncio.run(verifier.verify_token(token))
    assert result.resource == config.resource_url
    assert result.scopes == [SCOPE]
    assert result.client_id == config.subject


@pytest.mark.parametrize(
    "field,value",
    [
        ("iss", "https://other.example/"),
        ("aud", "wrong"),
        ("sub", "other"),
        ("scope", "write"),
        ("scope", [SCOPE]),
        ("exp", 1),
        ("exp", str(int(time.time()) + 300)),
    ],
)
def test_rejects_wrong_token_claims(config, monkeypatch, field, value):
    verifier, private, claims = signed_verifier(config, monkeypatch)
    claims[field] = value
    assert (
        asyncio.run(
            verifier.verify_token(jwt.encode(claims, private, algorithm="RS256"))
        )
        is None
    )


def test_rejects_invalid_signature_and_unavailable_keys(config, monkeypatch):
    verifier, private, claims = signed_verifier(config, monkeypatch)
    assert asyncio.run(verifier.verify_token("invalid")) is None

    def unavailable(token):
        raise jwt.PyJWKClientConnectionError("offline")

    monkeypatch.setattr(verifier.keys, "get_signing_key_from_jwt", unavailable)
    assert (
        asyncio.run(
            verifier.verify_token(jwt.encode(claims, private, algorithm="RS256"))
        )
        is None
    )


def test_transport_requires_auth_and_exposes_discovery(tmp_path, config):
    server = create_server(init_workspace(tmp_path).workspace, config)
    with TestClient(
        server.streamable_http_app(), base_url="https://history.example"
    ) as client:
        response = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert response.status_code == 401
        assert "resource_metadata" in response.headers["www-authenticate"]
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200
        assert metadata.json()["resource"] == config.resource_url


def test_history_tools_are_bounded_and_read_only(tmp_path, config):
    ws = init_workspace(tmp_path).workspace
    job = Job(
        protocol=1,
        project_id=ws.project_id,
        id="PATCH-MCP-TEST",
        kind="patch",
        actions=[{"create_file": {"path": "example.txt", "content": "example"}}],
        checks=[{"import_check": {"modules": ["json"]}}],
    )
    execute_plan(plan_job(job, ws), approved=True)
    server = create_server(ws, config)

    async def exercise():
        tools = await server.list_tools()
        assert {t.name for t in tools} == {
            "history_list",
            "history_latest",
            "history_show",
        }
        assert all(
            t.annotations.readOnlyHint and not t.annotations.destructiveHint
            for t in tools
        )
        listing = await server.call_tool("history_list", {"limit": 1})
        # FastMCP returns content blocks and structured output.
        output = json.loads(listing[0].text)
        reference = output["records"][0]["reference"]
        latest = json.loads((await server.call_tool("history_latest", {}))[0].text)
        exact = json.loads(
            (await server.call_tool("history_show", {"reference": reference}))[0].text
        )
        assert latest == exact
        assert exact["job"]["id"] == job.id
        with pytest.raises(Exception, match="limit"):
            await server.call_tool("history_list", {"limit": 101})
        with pytest.raises(Exception):
            await server.call_tool("history_show", {"reference": "../../.env"})

    asyncio.run(exercise())
    assert (tmp_path / "example.txt").read_text() == "example"


def test_cli_uses_exact_workspace_and_loopback(tmp_path, config, monkeypatch):
    init_workspace(tmp_path)
    path = tmp_path / "mcp.json"
    path.write_text(config.model_dump_json())
    seen = []

    def create(ws, cfg):
        assert ws.root == tmp_path
        assert cfg == config
        return SimpleNamespace(run=lambda **kwargs: seen.append(kwargs))

    monkeypatch.setattr("patchshuttle.integrations.mcp_server.create_server", create)
    result = CliRunner().invoke(
        main, ["--workspace", str(tmp_path), "--config", str(path)]
    )
    assert result.exit_code == 0, result.output
    assert seen == [{"transport": "streamable-http"}]


def test_rejects_wrong_resource_path(tmp_path, config):
    config.resource_url = "https://history.example/other"
    with pytest.raises(ValueError, match="/mcp"):
        create_server(init_workspace(tmp_path).workspace, config)


def test_authenticated_transport_lists_only_read_tools(tmp_path, config, monkeypatch):
    verifier, private, claims = signed_verifier(config, monkeypatch)
    monkeypatch.setattr(
        "patchshuttle.integrations.mcp_server.OwnerTokenVerifier", lambda cfg: verifier
    )
    server = create_server(init_workspace(tmp_path).workspace, config)
    token = jwt.encode(claims, private, algorithm="RS256")
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(
        server.streamable_http_app(), base_url="https://history.example"
    ) as client:
        response = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.status_code == 200
        assert {t["name"] for t in response.json()["result"]["tools"]} == {
            "history_list",
            "history_latest",
            "history_show",
        }
        claims["sub"] = "another-owner"
        headers["Authorization"] = "Bearer " + jwt.encode(
            claims, private, algorithm="RS256"
        )
        denied = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert denied.status_code == 401
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert (
        asyncio.run(
            verifier.verify_token(jwt.encode(claims, other_key, algorithm="RS256"))
        )
        is None
    )
