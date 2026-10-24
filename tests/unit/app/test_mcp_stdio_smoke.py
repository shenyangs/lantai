"""Real local stdio transport round-trip; no model, credentials or network."""
import json
import os
import sys

import pytest

from lantai.app.mcp.stateful_client import StdIOStatefulClient


@pytest.mark.skipif(os.name == "nt", reason="POSIX PID liveness check")
async def test_real_stdio_discovery_call_reload_and_shutdown(tmp_path):
    script = tmp_path / "echo_mcp.py"
    script.write_text(
        """import json,os,sys
for line in sys.stdin:
    message=json.loads(line)
    if "id" not in message:
        continue
    method=message.get("method")
    params=message.get("params",{})
    if method=="initialize":
        result={"protocolVersion":params["protocolVersion"],"capabilities":{"tools":{}},"serverInfo":{"name":"local-echo","version":"1"}}
    elif method=="tools/list":
        result={"tools":[{"name":"echo","description":"Echo test payload","inputSchema":{"type":"object","properties":{"text":{"type":"string"}}}}]}
    elif method=="tools/call":
        result={"content":[{"type":"text","text":json.dumps({"pid":os.getpid(),"echo":params.get("arguments",{}).get("text")})}],"isError":False}
    else:
        result={}
    print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":result}),flush=True)
"""
    )
    client = StdIOStatefulClient(
        "local-smoke",
        sys.executable,
        args=[str(script)],
        read_timeout_seconds=2,
    )
    pids = []
    try:
        await client.connect(timeout=5)
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["echo"]
        for index in range(2):
            result = await client.call_tool("echo", {"text": f"round-{index}"})
            body = json.loads(result.content[0].text)
            assert body["echo"] == f"round-{index}"
            pids.append(body["pid"])
            if index == 0:
                await client.reload(timeout=5)
        assert pids[0] != pids[1]
    finally:
        await client.close()
    assert client._lifecycle_task is None
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
