---
name: liaison-share
description: "Use Liaison when the user wants to share a local web app, generated HTML/PPT, API, dashboard, or private network service with other people. Confirm permission before exposing any local service."
metadata:
  builtin_skill_version: "1.0"
  lantai:
    emoji: "🔗"
    requires: {}
---

# Liaison Local Sharing

Use this skill when the user wants to share a local page, localhost app, generated HTML/PPT, API, dashboard, or private network service for external preview.

## What Liaison Is

Liaison is an open-source secure reverse proxy and private application access solution. It is designed to connect devices and apps behind NAT without exposing LAN ports directly, with TLS encryption, connector-based access, traffic monitoring, IP allowlists, and support for HTTP/HTTPS, WebSocket, and TCP proxying.

Source: https://github.com/liaisonio/liaison

## Decision Flow

1. Identify the target: static HTML, Vite/Next/Flask/FastAPI service, API, dashboard, file directory, or another internal service.
2. Check local readiness: port, path, auth state, sensitive data, and whether the page opens locally.
3. Before public sharing, explain the local address, port, path, and exposure risk, then wait for explicit user approval.
4. Prefer read-only previews. Do not expose the workspace root, system config, secrets, database admin panels, private chats, or internal admin consoles.
5. If the user only needs local access, provide the local URL instead of using Liaison.
6. If Liaison is not installed or configured, explain that a server and connector are required. Do not download or run unknown binaries without explicit approval.

## Recommended Operations

- Static HTML: serve only the target directory locally, then expose that local URL through Liaison.
- Existing dev server: reuse the current port, such as `http://127.0.0.1:5173`, instead of starting a duplicate server.
- API service: expose only demo or documentation endpoints and warn against exposing write, delete, or admin endpoints.
- File sharing: use a dedicated read-only directory rather than the whole workspace.
- Before sharing: verify the page is not an error page, not a private logged-in page, and that assets load.
- After sharing: tell the user the public URL, exposure scope, lifetime, and how to stop sharing.

## Safety Boundary

- External sharing requires explicit confirmation by default. Without approval, only explain the plan or provide local preview steps.
- If secrets, tokens, cookies, personal chats, customer data, or unsanitized private data are present, ask the user to remove them before continuing.
- Do not present Liaison as long-term hosting. It is best for controlled access and temporary previews; production publishing should use normal deployment or object storage.
- If the Liaison entry supports allowlists, expiration, login, or access control, recommend enabling them.

## Response Templates

Before sharing:

> I can expose `local URL` through Liaison as a read-only preview. The external scope is `scope`, and anyone with the link may be able to access it. Should I continue?

After sharing:

> Sharing link ready: `URL`. It currently exposes `scope`; stop it from the Liaison dashboard or by stopping the local service.
