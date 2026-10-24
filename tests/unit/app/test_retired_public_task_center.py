from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_task_center_has_no_public_navigation_or_search_surface() -> None:
    sidebar = _read("console/src/layouts/Sidebar.tsx")
    preferences = _read("console/src/layouts/sidebarPreferences.ts")
    search = _read(
        "console/src/pages/Chat/components/ChatSearchPanel/unifiedSearch.ts"
    )
    registry = _read("src/lantai/customization/data/registry.json")

    assert 'key: "task-center"' not in sidebar
    assert 'path: "/task-center"' not in sidebar
    assert 'id: "task-center"' not in preferences
    assert "navigation.sidebar.tasks.visible" not in registry
    assert "runtimeApi.listTasks" not in search
    assert 'kind: "task"' not in search


def test_retired_task_center_route_only_redirects_to_chat() -> None:
    routes = _read("console/src/layouts/MainLayout/index.tsx")

    assert "TaskCenterPage" not in routes
    assert "TaskCenterV2Page" not in routes
    assert routes.count('path="/task-center') == 1
    assert (
        'path="/task-center/*"\n'
        '                    element={<Navigate to="/chat" replace />}'
    ) in routes


def test_packaged_runtime_smokes_follow_retired_route_into_chat() -> None:
    browser_content_smoke = _read(
        "console/electron/browserContentRuntimeSmoke.cjs"
    )
    concurrency_smoke = _read(
        "console/electron/taskCenterMultiWindowSmoke.cjs"
    )

    for smoke in (browser_content_smoke, concurrency_smoke):
        assert '#/task-center?run=' in smoke
        assert '"/task-center/legacy"' not in smoke
        assert '"高级控制"' not in smoke

    assert 'waitForPath(win, "/chat"' in browser_content_smoke
    assert 'waitForPath(first, "/chat"' in concurrency_smoke
    assert 'waitForPath(second, "/chat"' in concurrency_smoke
    assert "/api/runtime/runs/\\${encodeURIComponent(runId)}/export" in (
        browser_content_smoke
    )
    assert 'postRendererAction(first, runId, "budget"' in concurrency_smoke
    assert 'postRendererAction(first, runId, "pause"' in concurrency_smoke
    assert 'postRendererAction(second, runId, "pause"' in concurrency_smoke


def test_public_catalog_and_today_timeline_do_not_resurface_tasks() -> None:
    catalog = _read("src/lantai/runtime/capability_catalog.py")
    timeline_api = _read("src/lantai/app/time_system/api.py")
    product_guide = _read("src/lantai/agents/md_files/zh/PRODUCT_GUIDE.md")

    assert "task-center-v2" not in catalog
    assert 'action_path="/task-center"' not in timeline_api
    assert "task_provider" not in timeline_api
    assert "任务中心" not in product_guide
