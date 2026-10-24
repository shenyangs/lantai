from pathlib import Path

from lantai.utils import console_static


def _write_index(directory: Path, marker: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(marker, encoding="utf-8")


def test_source_checkout_prefers_latest_repo_console_dist(
    monkeypatch,
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "src" / "lantai"
    repo_dist = tmp_path / "console" / "dist"
    embedded_dist = package_dir / "console"
    _write_index(repo_dist, "new")
    _write_index(embedded_dist, "old")

    monkeypatch.setattr(
        console_static,
        "__file__",
        str(package_dir / "utils" / "console_static.py"),
    )
    monkeypatch.setattr(
        console_static.EnvVarLoader,
        "get_str",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.chdir(tmp_path)

    assert console_static.resolve_console_static_dir() == str(repo_dist)


def test_explicit_console_static_override_keeps_highest_priority(
    monkeypatch,
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "explicit-console"
    monkeypatch.setattr(
        console_static.EnvVarLoader,
        "get_str",
        lambda *_args, **_kwargs: str(explicit),
    )

    assert console_static.resolve_console_static_dir() == str(explicit)
