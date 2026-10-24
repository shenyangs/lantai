from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_system_calendar_capability_is_not_shipped() -> None:
    removed_paths = (
        "src/lantai/app/time_system/calendar_gateway.py",
        "src/lantai/agents/tools/calendar_event.py",
        "console/src/pages/Settings/Calendar",
        "console/src/pages/Chat/components/CalendarEventDraftCard.tsx",
        "console/src/pages/Chat/components/calendarEventDraft.ts",
    )
    assert all(not (ROOT / path).exists() for path in removed_paths)

    forbidden_by_file = {
        "pyproject.toml": ("pyobjc-framework-EventKit",),
        "lantai-server.spec": ("EventKit", "NSCalendars"),
        "console/package.json": ("NSCalendars",),
        "scripts/pack/build_macos.sh": ("NSCalendars",),
        "src/lantai/app/time_system/api.py": ("/calendar/", "CalendarConnection"),
        "src/lantai/agents/tool_registry.py": ("prepare_calendar_event",),
        "src/lantai/runtime/capability_catalog.py": ("prepare_calendar_event",),
        "src/lantai/app/tool_catalog.py": ("prepare_calendar_event",),
        "console/src/layouts/MainLayout/index.tsx": ("/calendar", "CalendarSettingsPage"),
        "console/src/pages/Settings/Home/index.tsx": ("\u7cfb\u7edf\u65e5\u5386", "/calendar"),
        "console/src/pages/Control/Reminders/index.tsx": (
            "calendar_event",
            "calendar_status",
            "\u8fde\u63a5\u7cfb\u7edf\u65e5\u5386",
        ),
        "console/src/pages/Chat/index.tsx": ("CalendarEventDraftCard",),
        "console/src/pages/Chat/components/QuquResponseCard.tsx": (
            "isCalendarEventToolName",
        ),
        "electron/entry.py": ("calendar_event", "EventKit"),
        "src/lantai/app/routers/computer_permissions.py": ("calendar",),
        "console/src/api/modules/computerPermissions.ts": ("calendar",),
    }
    failures = {
        path: [needle for needle in needles if needle in _text(path)]
        for path, needles in forbidden_by_file.items()
    }
    assert not {path: needles for path, needles in failures.items() if needles}
