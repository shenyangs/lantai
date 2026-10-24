"""Explicitly configured RSSHub transport, separate from public article reading."""

import ipaddress
from urllib.parse import urljoin, urlsplit

import httpx2

from ...agents.tools.web import PublicWebDocument
from ..mcp.network_security import resolve_mcp_endpoint, create_pinned_mcp_http_client
from .catalog import instance_url


async def request_feed(url, base, allow_private):
    base = instance_url(base)
    parsed_base = urlsplit(base)
    current = url
    for _ in range(4):
        parsed = urlsplit(current)
        if (parsed.scheme, parsed.netloc) != (
            parsed_base.scheme,
            parsed_base.netloc,
        ) or not parsed.path.startswith(parsed_base.path.rstrip("/") + "/"):
            raise ValueError("RSSHub 重定向离开了已配置实例，请检查实例地址")
        endpoint = await resolve_mcp_endpoint(current, allow_private_network=True)
        address = ipaddress.ip_address(endpoint.pinned_ip)
        try:
            ipaddress.ip_address(endpoint.host)
            literal = True
        except ValueError:
            literal = False
        fake_ip = address in ipaddress.ip_network("198.18.0.0/15") and not literal
        if not address.is_global and not fake_ip and not allow_private:
            raise ValueError("此实例位于本机或局域网，请先在实例设置中允许访问")
        async with create_pinned_mcp_http_client(
            endpoint,
            headers={"User-Agent": "Lantai-Reading/1.0"},
            timeout=httpx2.Timeout(12),
        ) as client:
            async with client.stream("GET", endpoint.url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("实例重定向缺少目标地址")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 2_097_152:
                        raise ValueError("订阅源超过 2 MB 限制")
                    chunks.append(chunk)
                return PublicWebDocument(
                    current, response.headers.get("content-type", ""), b"".join(chunks)
                )
    raise ValueError("实例重定向次数过多")
