"""Explicit local setup through one fixed entrypoint in a verified private runtime."""

import os
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from skulk.extensions.runtime_attachment import (
    HostSettings,
    InstallationIdentifier,
    ServiceConnection,
)
from skulk.extensions.runtime_files import read_private
from skulk.extensions.runtime_selection import RuntimeSelection, RuntimeSelector
from skulk.shared.constants import SKULK_CONFIG_HOME

_SETUP_ENTRYPOINT = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('__setup__',run_name='__main__')"
_MANAGEMENT_ENTRYPOINT = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('__manage__',run_name='__main__')"


def _retained_history(root: Path, selection: RuntimeSelection) -> None:
    journal = root / "installer/operations.sqlite3"
    read_private(journal, 128 * 1024 * 1024)
    with closing(sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True)) as database:
        if (
            database.execute("SELECT 1 FROM trust_floor WHERE singleton=1").fetchone()
            is None
            or database.execute(
                "SELECT 1 FROM releases WHERE bundle=? AND sequence=? AND digest=?",
                (selection.bundle_id, selection.sequence, selection.runtime_digest),
            ).fetchone()
            is None
        ):
            raise ValueError("restore retained plugin release history before setup")


async def setup_installed_plugin(plugin_id: str, arguments: tuple[str, ...]) -> None:
    """Replace this local terminal process with the selected plugin's setup command.

    Resolve the installation from the protected local service connection, verify
    signatures, compatibility and all executable bytes, then execute only the
    archive's fixed optional __setup__ entrypoint. Arguments are plugin-owned setup
    fields, never a command/module selector. The inherited installation lock blocks
    generation changes until setup exits. No remote route invokes this operation.
    """
    await _run_installed_plugin(plugin_id, arguments, "setup")


async def manage_installed_plugin(plugin_id: str, arguments: tuple[str, ...]) -> None:
    """Run the installed plugin's fixed local management entrypoint by identity.

    Verify the same selected runtime and inherited generation fence as setup.
    The plugin discovers its durable state from the verified installation and
    owns fixed terminal verbs; callers never select a path, module or executable.
    """
    await _run_installed_plugin(plugin_id, arguments, "manage")


async def _run_installed_plugin(
    plugin_id: str, arguments: tuple[str, ...], action: Literal["setup", "manage"]
) -> None:
    if os.geteuid() == 0 or os.geteuid() != os.getuid():
        raise ValueError("plugin setup requires the existing nonroot owner")
    identifier = TypeAdapter[str](InstallationIdentifier).validate_python(
        plugin_id, strict=True
    )
    if len(arguments) > 128 or sum(len(arg.encode()) for arg in arguments) > 16384:
        raise ValueError("plugin setup arguments exceed bound")
    if any("\x00" in arg for arg in arguments):
        raise ValueError("invalid plugin setup argument")
    connection = ServiceConnection.model_validate_json(
        read_private(SKULK_CONFIG_HOME / "managed-service/connection.json", 8192)
    )
    manager_root = Path(connection.manager_root)
    host = HostSettings.model_validate_json(read_private(manager_root / "host.json"))
    if host.profile_id != connection.profile_id:
        raise ValueError("local plugin service profile differs")
    root = manager_root / "installations" / identifier
    selection = RuntimeSelection.model_validate_json(
        read_private(root / "runtime-selection.json")
    )
    # Existing authority must be present before the selector's constructor can
    # initialize installer tables. Setup must never recreate lost trust history.
    _retained_history(root, selection)
    selector = RuntimeSelector(root)
    async with selector.installer.locked_generation(
        selection.runtime_digest, inherit_on_exec=True, wait_for_ownership=True
    ):
        if selector.current() != selection or selector.pending.exists():
            raise ValueError("plugin selection changed or requires recovery")
        generation = root / "generations" / selection.runtime_digest
        artifact = generation / "artifacts/bundle.pyz"
        entrypoint = "__setup__.py" if action == "setup" else "__manage__.py"
        bootstrap = _SETUP_ENTRYPOINT if action == "setup" else _MANAGEMENT_ENTRYPOINT
        with zipfile.ZipFile(artifact) as archive:
            try:
                entry = archive.getinfo(entrypoint)
            except KeyError:
                raise ValueError(
                    "this plugin has no selected local entrypoint"
                ) from None
            if entry.is_dir() or entry.file_size > 16384:
                raise ValueError("invalid plugin setup entrypoint")
        python = str(generation / "runtime/bin/python")
        # Exec retains terminal I/O and normal interrupt behavior. Only the verified
        # generation and fixed module come from us; provider prompts remain private.
        os.execve(
            python,
            (python, "-I", "-B", "-c", bootstrap, str(artifact), *arguments),
            {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
