"""Application service connecting Runtime policy to the desktop bridge."""

from __future__ import annotations

from .bridge import ElectronMacUseBridgeClient
from .models import MacUseActionRequest, MacUseActionResult


class MacUseService:
    def __init__(self, bridge: ElectronMacUseBridgeClient | None = None) -> None:
        self.bridge = bridge or ElectronMacUseBridgeClient()

    async def execute(self, request: MacUseActionRequest) -> MacUseActionResult:
        return await self.bridge.execute(request)
