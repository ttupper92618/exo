# pyright: reportPrivateUsage=false
"""Dashboard-asset resolution for headless/worker nodes (#333)."""

from pathlib import Path

import pytest

from skulk.utils import dashboard_path


def test_resources_from_installed_package_without_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = tmp_path / "environment/lib/python3.13/site-packages/skulk"
    resources = package / "resources"
    resources.mkdir(parents=True)
    monkeypatch.setattr(
        dashboard_path, "__file__", str(package / "utils/dashboard_path.py")
    )
    monkeypatch.chdir(tmp_path)
    assert dashboard_path.find_resources() == resources


def test_installed_resources_take_precedence_over_ancestor_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = tmp_path / "skulk"
    resources = package / "resources"
    resources.mkdir(parents=True)
    (tmp_path / "resources").mkdir()
    monkeypatch.setattr(
        dashboard_path, "__file__", str(package / "utils/dashboard_path.py")
    )
    assert dashboard_path.find_resources() == resources


def test_retained_service_snapshot_resource_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    resources = tmp_path / "runtime/resources"
    resources.mkdir(parents=True)
    monkeypatch.setattr(
        dashboard_path,
        "__file__",
        str(
            tmp_path
            / "runtime/lib/python3.13/site-packages/skulk/utils/dashboard_path.py"
        ),
    )
    assert dashboard_path.find_resources() == resources


def test_resources_in_desktop_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    monkeypatch.setattr(dashboard_path, "_find_resources_in_package", lambda: None)
    monkeypatch.setattr(dashboard_path, "_find_resources_in_repo", lambda: None)
    monkeypatch.setattr(dashboard_path.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert dashboard_path.find_resources() == resources


def test_missing_resources_require_complete_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dashboard_path, "_find_resources_in_package", lambda: None)
    monkeypatch.setattr(dashboard_path, "_find_resources_in_repo", lambda: None)
    monkeypatch.setattr(dashboard_path, "_find_resources_in_bundle", lambda: None)
    with pytest.raises(FileNotFoundError, match="Reinstall the complete Skulk package"):
        dashboard_path.find_resources()


def test_find_dashboard_optional_returns_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A headless node with no built assets and no bundle: resolution must yield
    # None rather than raising, so importing constants does not fail on boot.
    monkeypatch.setattr(dashboard_path, "_find_react_dashboard_in_repo", lambda: None)
    monkeypatch.setattr(dashboard_path, "_find_dashboard_in_bundle", lambda: None)
    assert dashboard_path.find_dashboard_optional() is None


def test_find_dashboard_raises_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The strict variant still raises for callers that require the UI.
    monkeypatch.setattr(dashboard_path, "_find_react_dashboard_in_repo", lambda: None)
    monkeypatch.setattr(dashboard_path, "_find_dashboard_in_bundle", lambda: None)
    with pytest.raises(FileNotFoundError):
        dashboard_path.find_dashboard()


def test_find_dashboard_optional_returns_found_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        dashboard_path, "_find_react_dashboard_in_repo", lambda: tmp_path
    )
    monkeypatch.setattr(dashboard_path, "_find_dashboard_in_bundle", lambda: None)
    assert dashboard_path.find_dashboard_optional() == tmp_path
    assert dashboard_path.find_dashboard() == tmp_path
