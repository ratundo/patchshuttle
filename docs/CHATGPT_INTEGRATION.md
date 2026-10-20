# Read PatchShuttle results from ChatGPT

The optional MCP server exposes `history_list`, `history_latest` and
`history_show` for one explicitly selected workspace. It does not expose job
execution, database execution, raw log downloads or arbitrary filesystem reads.
Returned project text is untrusted data, not instructions.

Install `patchshuttle[mcp]`. Create an owner-controlled JSON configuration
outside job content, replacing every example value:

```json
{
  "issuer": "https://identity.example/",
  "jwks_url": "https://identity.example/.well-known/jwks.json",
  "resource_url": "https://history.example/mcp",
  "subject": "your-exact-OAuth-subject",
  "port": 8765
}
```

Start with `patchshuttle-mcp --workspace /absolute/project --config /absolute/mcp.json`.
The process binds only `127.0.0.1`. Put it behind your HTTPS reverse proxy and
forward `/mcp` and `/.well-known/oauth-protected-resource/mcp` to port 8765.
Keep Authorization and Host headers intact. Do not expose an unauthenticated
alternate listener. The resource URL must end in the exact `/mcp` path.

Use an established OAuth authorization server with discovery, authorization-code
and PKCE support, and ChatGPT-compatible client registration (CIMD or DCR, or
configured static client credentials). Configure audience to the exact resource
URL and issue the `patchshuttle:history:read` scope to the configured subject.
The resource server accepts RS256/ES256 JWT access tokens only and validates
signature, expiry, issuer, audience, scope and the exact subject. It is not an
OAuth authorization server; opaque tokens/introspection are not implemented.
No access tokens or signing secrets belong in patch jobs or this configuration.

In ChatGPT's custom MCP connection settings, supply the HTTPS MCP URL and
configure OAuth. Install/select the resulting plugin and ask for the latest
PatchShuttle result. Availability and the actual linking flow depend on the
account/workspace. A live connection to the user's deployment is not established
by installing this Python package.

Local validation covers signed tokens, rejected claims/signatures, protected
resource discovery, HTTP authorization and history reads. It does not constitute
an end-to-end ChatGPT/OAuth-provider deployment test. The existing history
redaction remains best effort; authenticated access can reveal project details.

Official setup and authorization references:

- https://developers.openai.com/api/docs/guides/custom-mcp-server
- https://developers.openai.com/plugins/build/auth
- https://developers.openai.com/api/docs/guides/secure-mcp-tunnels

A Secure MCP Tunnel is another documented transport option, but this package's
initial configuration and local tests target an HTTPS reverse-proxy deployment.
