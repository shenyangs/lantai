# Third-party notices

## Optional llama.cpp backend download

Lantai's local-model installer downloads selected release archives directly from
ggml-org/llama.cpp on GitHub. These archives are not bundled into Lantai by this
integration. The admitted release is `b8744`, source commit
`d7ff074c87ecacd57d5760e2f678866ba9fe7149`; the eight archive digests and exact
sizes are fixed in `src/lantai/local_models/llamacpp_assets.py`.

The complete embedded notices for llama.cpp (MIT), cpp-httplib (MIT), jsonhpp
(MIT), and, where embedded, BoringSSL (Apache-2.0 and the complete upstream
support-code notice) are preserved in
`src/lantai/local_models/llamacpp_notices.py`. Linux packages contain three of
these sections; macOS and Windows packages contain four. Each installation
checks the complete, untrimmed `llama-server --license` stdout against this
fixed manifest and the actual server bytes. The verified text is saved as
`.lantai-llamacpp-notices.txt` beside the installed server. The installation
receipt binds that text to the archive and server digests.

- Upstream: https://github.com/ggml-org/llama.cpp
- License sources: `LICENSE`, `licenses/LICENSE-cpp-httplib`,
  `licenses/LICENSE-jsonhpp` at the source commit above, and BoringSSL
  `0.20260327.0/LICENSE`.
- Integration and evidence: 2026-10-04; LTA-022.
- Dependency review still open: the four Windows archives contain Microsoft's
  OpenMP LLVM runtime copied by the upstream release workflow from
  `redist/debug_nonredist`. The x64 DLL digest is
  `9ab1cb787e52b2a36133899c01c1db1f876067d1471cd224ead85f40a8152b99`;
  the arm64 DLL digest is
  `d2649698fc68466ee88cf58ae3ab0fab106f29326e83aaaa5643d30651a962e3`.
  The embedded server notice does not establish authorization for these DLLs.
  Exact rights and notice evidence for this direct-upstream download path
  remain pending. Their presence is not reported as a completed license review.
- Linux's system OpenSSL and other system libraries are external runtime
  dependencies; the embedded notices do not cover all system-library licenses.

This review establishes archive identity and embedded notice consistency.
A successful `--version` check is not evidence of model inference, GPU
availability, OS compatibility on every target, or complete dependency licensing.

## Agent Client Protocol Python SDK

Lantai bundles the official Python SDK to expose its existing runtime as an
ACP agent server over standard input and output. Lantai's runtime, workspace
authorization, session handling, and cancellation remain independently
implemented.

- Upstream: https://github.com/agentclientprotocol/python-sdk
- Package: `agent-client-protocol>=0.12.1,<0.13`
- License: Apache License 2.0
- Local integration: 2026-09-27

## Huashu Mac Use design reference

Lantai's signed macOS helper and Runtime contracts are independently implemented,
informed by huashu-mac-use's probe-first control hierarchy, background observation,
and action-evidence discipline. The upstream skill, CLI, scripts, binaries, paths,
brand, and per-application coordinates are not bundled or required at runtime.

- Upstream: https://github.com/alchaincyf/huashu-mac-use
- Evaluated revision: `b51b3701507a72ce07d3609059da3d4b92fa14e5`
- Upstream license: MIT, Copyright (c) 2026 Huashu (花叔)
- Local implementation: 2026-09-07
- Integration notes: `docs/mac-use-architecture-zh.md`

## Browser Use design reference

The browser observation and progress enhancements are independently implemented
for Lantai, informed by Browser Use's semantic DOM and action-loop mechanisms.
No Browser Use runtime package or source file is bundled by this change.

- Reference: https://github.com/shenyangs/browser-use
- Original project: https://github.com/browser-use/browser-use
- Evaluated revision: `e25ab65e699af3031a1f2d348526de2844be0e89`
- Upstream license: MIT, Copyright (c) 2024 Gregor Zunic
- Integration notes: `docs/browser-use-mechanism-fusion-2026-09-07-zh.md`

## AgentScope CoPaw channel adapters

The Slack and Tencent Yuanbao channel adapters under
`src/lantai/app/channels/slack` and `src/lantai/app/channels/yuanbao` are
adapted from the AgentScope CoPaw project. They have been modified for
Lantai's runtime contracts, secure channel policies, delivery ledger, and
configuration console.

- Upstream: https://github.com/agentscope-ai/CoPaw
- License: Apache License 2.0
- Local modifications: 2026-08-30

## Tencent BrowserSkill compatibility integration

Lantai includes an independently implemented compatibility adapter for the
BrowserSkill `bsk` command-line protocol. It can connect to a separately
installed BrowserSkill CLI and browser extension; BrowserSkill source code,
binaries, and extensions are not bundled in Lantai.

- Upstream: https://github.com/Tencent/BrowserSkill
- Upstream revision evaluated: `47ac947f188bc554b66c09f5873b8045d6be57b2`
- License: MIT License
- Local integration: 2026-09-04

## OpenChatCut MCP connector

Lantai includes an independently implemented connector that interoperates
with a separately installed OpenChatCut desktop app through its local MCP
endpoint. OpenChatCut source code, binaries, media codecs, and model assets are
not bundled in Lantai.

- Upstream: https://github.com/0xsline/OpenChatCut
- Upstream revision evaluated: `19cba6e1a70a3e589545ce02de975f6494c918f6`
- Upstream license: GNU Affero General Public License v3.0 or later
- Local integration: 2026-09-04

## Chrome DevTools MCP

Lantai bundles the official `chrome-devtools-mcp` npm package and launches it
with the Node.js runtime embedded in Electron. Lantai supplies a reserved MCP
configuration, full-capability startup flags, an explicit package-version
boundary, and its own permission policy; the upstream package is not modified.

- Upstream: https://github.com/ChromeDevTools/chrome-devtools-mcp
- Package: `chrome-devtools-mcp@1.8.0`
- Upstream revision: `45f187b1e3202c9f32ddba913be5d68751c3caa3`
- npm integrity: `sha512-Wrm9z0/5WbVs778apjWgYRkpe9bvYQWjK2zVRwqoPAtz1IHQ5+GvotM07UGXJcfrA0rj6Gt1Pnn5+w/Tf1nU4w==`
- License: Apache License 2.0
- Local integration: 2026-09-05
