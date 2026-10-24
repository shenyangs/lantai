"""Foundation gate entry: synthetic desktop notification behavior, no OS calls."""
from pathlib import Path
import os
import shutil
import subprocess


def test_native_notification_confirmation_and_adjacent_desktop_contracts():
    root = Path(__file__).resolve().parents[3]
    node = shutil.which("node")
    assert node, "The protected desktop notification checks require Node.js"
    result = subprocess.run(
        [
            node,
            "--test",
            "console/electron/notificationDeliveryConfirmation.test.cjs",
            "console/electron/notificationDeliveryObserver.test.cjs",
            "console/electron/systemBehaviorController.test.cjs",
            "console/electron/reminderNotificationRetry.test.cjs",
            "console/electron/packageAllowlist.test.cjs",
        ],
        cwd=root,
        env={**os.environ, "LANTAI_AUDIT_WORKSPACE": str(root)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
