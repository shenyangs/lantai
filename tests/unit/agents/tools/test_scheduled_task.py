# -*- coding: utf-8 -*-
import json

import pytest

from lantai.agents.tools.scheduled_task import prepare_scheduled_task
from lantai.runtime.adapters.legacy_toolkit_components import LegacyToolkit as Toolkit


def test_prepare_scheduled_task_registers_with_toolkit():
    toolkit = Toolkit()

    toolkit.register_tool_function(prepare_scheduled_task)


@pytest.mark.asyncio
async def test_prepare_scheduled_task_returns_confirmation_draft():
    response = await prepare_scheduled_task(
        name="每日早报",
        cron="0 9 * * *",
        prompt="帮我生成每日早报",
        timezone="Asia/Shanghai",
        summary="每天 9 点运行",
    )

    block = response.content[0]
    text = block["text"] if isinstance(block, dict) else block.text
    payload = json.loads(text)

    assert payload["type"] == "scheduled_task_draft"
    assert payload["summary"] == "每天 9 点运行"
    assert payload["spec"]["name"] == "每日早报"
    assert payload["spec"]["schedule"]["cron"] == "0 9 * * *"
    assert payload["spec"]["dispatch"]["target"]["session_id"] == ""
    assert payload["spec"]["request"]["input"][0]["content"][0]["text"] == (
        "帮我生成每日早报"
    )
