import sys
from pathlib import Path
from typing import cast


def find_resources() -> Path:
    """Return installed declarative resources, retaining source and bundle support.

    Wheel resources live beside the imported Skulk package so installation does
    not depend on a checkout or the process working directory. A missing payload
    raises ``FileNotFoundError`` rather than silently using empty model metadata.
    """
    resources = (
        _find_resources_in_package()
        or _find_resources_in_repo()
        or _find_resources_in_bundle()
    )
    if resources is None:
        raise FileNotFoundError(
            "Unable to locate Skulk resources. Reinstall the complete Skulk package."
        )
    return resources


def _find_resources_in_package() -> Path | None:
    candidate = Path(__file__).resolve().parent.parent / "resources"
    return candidate if candidate.is_dir() else None


def _find_resources_in_repo() -> Path | None:
    current_module = Path(__file__).resolve()
    for parent in current_module.parents:
        build = parent / "resources"
        if build.is_dir():
            return build
    return None


def _find_resources_in_bundle() -> Path | None:
    frozen_root = cast(str | None, getattr(sys, "_MEIPASS", None))
    if frozen_root is None:
        return None
    candidate = Path(frozen_root) / "resources"
    if candidate.is_dir():
        return candidate
    return None


def find_dashboard_optional() -> Path | None:
    """Locate the built dashboard assets, or ``None`` if they are absent.

    A headless/worker node (e.g. a Linux node with no node/npm to build the
    dashboard) is a first-class deployment, so a missing ``dashboard-react/dist``
    must not be fatal. Callers that can run without the UI use this and skip
    serving it; callers that require the UI use :func:`find_dashboard`.
    """
    return _find_react_dashboard_in_repo() or _find_dashboard_in_bundle()


def find_dashboard() -> Path:
    dashboard = find_dashboard_optional()
    if not dashboard:
        raise FileNotFoundError(
            "Unable to locate dashboard assets — run: cd dashboard-react && npm install && npm run build && cd .."
        )
    return dashboard


def _find_react_dashboard_in_repo() -> Path | None:
    """Skulk React dashboard assets inside the source tree."""
    current_module = Path(__file__).resolve()
    for parent in current_module.parents:
        build = parent / "dashboard-react" / "dist"
        if build.is_dir() and (build / "index.html").exists():
            return build
    return None


def _find_dashboard_in_bundle() -> Path | None:
    """Bundled dashboard assets for packaged desktop builds."""
    frozen_root = cast(str | None, getattr(sys, "_MEIPASS", None))
    if frozen_root is None:
        return None
    candidate = Path(frozen_root) / "dashboard"
    if candidate.is_dir():
        return candidate
    return None
