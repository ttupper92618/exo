"""Generic verification of signed offline plugin runtimes without plugin imports."""

import hashlib
import importlib.metadata
import importlib.util
import io
import json
import platform
import stat
import sys
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from skulk.extensions.runtime_files import read_private

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9._:@-]+$")
]
RuntimePlatform = Literal["macos-arm64", "ubuntu-24.04-x86_64"]


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class RuntimeTrust(_Contract):
    """Explicit local publisher authority and current revocation view."""

    revision: int = Field(ge=1, description="Monotonic owner trust revision.")
    expires_at: int = Field(gt=0, description="UTC Unix expiry of this trust view.")
    publishers: dict[Identifier, Digest] = Field(
        min_length=1,
        max_length=16,
        description="Publisher IDs mapped to Ed25519 public keys.",
    )
    revoked_publishers: tuple[Identifier, ...] = Field(
        default=(), max_length=16, description="Publishers no longer authorized."
    )
    revoked_artifacts: tuple[Digest, ...] = Field(
        default=(),
        max_length=128,
        description="Revoked bundle, wheel or runtime digests.",
    )


class RuntimeWheel(_Contract):
    """One exact publisher-approved wheel; no version resolution is permitted."""

    filename: str = Field(
        pattern=r"^[A-Za-z0-9_.+-]+\.whl$",
        max_length=240,
        description="Safe wheel basename.",
    )
    sha256: Digest = Field(description="Exact artifact SHA-256.")
    size: int = Field(ge=1, le=67108864, description="Exact compressed byte count.")

    @property
    def distribution(self) -> tuple[str, str]:
        """Return canonical distribution identity from the wheel filename."""
        name, version, _, _ = parse_wheel_filename(self.filename)
        return str(name), str(version)


class _ManifestClaims(BaseModel):
    # Plugin-specific policy remains opaque. The signature covers the original
    # canonical payload, never this partial interpretation of its common claims.
    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")
    bundle_id: Identifier
    bundle_version: str
    skulk_requires: str = Field(max_length=128)
    executable: Literal["bundle.pyz"]
    executable_sha256: Digest


class _ReleaseClaims(_Contract):
    protocol: Literal[1]
    publisher: Identifier
    sequence: int = Field(ge=1)
    created_at: int = Field(gt=0)
    expires_at: int = Field(gt=0)
    artifact_name: Literal["bundle.pyz"]
    artifact_size: int = Field(ge=1, le=67108864)
    manifest: _ManifestClaims
    platforms: tuple[Literal["darwin", "linux"], ...] = Field(
        min_length=1, max_length=2
    )
    python_requires: str = Field(max_length=128)
    skulk_build_sha256: Digest
    dependency_lock_sha256: Digest
    state_schema: Identifier
    compatible_state_schemas: tuple[Identifier, ...] = Field(max_length=8)
    permissions: tuple[Annotated[str, Field(min_length=1, max_length=512)], ...] = (
        Field(min_length=1, max_length=16)
    )


class _RuntimeClaims(_Contract):
    protocol: Literal[2]
    implementation: Literal["cpython"]
    platform: RuntimePlatform
    release: _ReleaseClaims
    wheels: tuple[RuntimeWheel, ...] = Field(min_length=1, max_length=64)


class _Signed(_Contract):
    runtime: dict[str, JsonValue]
    signature: str = Field(pattern=r"^[a-f0-9]{128}$")


@final
@dataclass(frozen=True)
class QualifiedHost:
    """Locally measured execution environment, never taken from an API caller."""

    platform: RuntimePlatform
    python_version: str
    skulk_version: str
    skulk_build_sha256: str


@final
@dataclass(frozen=True)
class VerifiedRuntime:
    """Authenticated immutable bytes and their generic installation claims."""

    metadata: bytes
    payload: bytes
    claims: _RuntimeClaims

    @property
    def digest(self) -> str:
        """Bind every signed claim including opaque plugin policy."""
        return hashlib.sha256(self.payload).hexdigest()

    @property
    def inventory(self) -> dict[str, str]:
        """Return a fresh exact dependency inventory without installer tools."""
        return dict(sorted(wheel.distribution for wheel in self.claims.wheels))


def canonical_json(value: JsonValue) -> bytes:
    """Serialize JSON with the signed runtime's canonical field ordering."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def measure_host() -> QualifiedHost:
    """Measure actual core sources, native bindings, Python and supported platform."""
    target: RuntimePlatform
    if sys.platform == "darwin" and platform.machine() == "arm64":
        target = "macos-arm64"
    elif (
        sys.platform == "linux"
        and platform.machine() == "x86_64"
        and platform.freedesktop_os_release().get("ID") == "ubuntu"
        and platform.freedesktop_os_release().get("VERSION_ID") == "24.04"
    ):
        target = "ubuntu-24.04-x86_64"
    else:
        raise ValueError("unsupported managed runtime platform")
    if sys.implementation.name != "cpython":
        raise ValueError("unsupported managed Python implementation")
    digest = hashlib.sha256()
    count = total = 0
    for package in ("skulk", "skulk_pyo3_bindings"):
        specification = importlib.util.find_spec(package)
        if specification is None or specification.origin is None:
            raise ValueError("qualified core build is unavailable")
        root = Path(specification.origin).parent
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if any(
                part in {"__pycache__", "tests", ".pytest_cache"}
                for part in relative.parts
            ):
                continue
            if path.is_symlink():
                raise ValueError("core build contains an unsupported file")
            if path.is_dir() or path.suffix in {".pyc", ".pyo"}:
                continue
            if not path.is_file():
                raise ValueError("core build contains an unsupported file")
            count += 1
            total += path.stat().st_size
            if count > 20000 or total > 536870912:
                raise ValueError("core build exceeds verification bound")
            with path.open("rb") as source:
                content = hashlib.file_digest(source, "sha256").digest()
            digest.update((package + "/" + relative.as_posix()).encode() + b"\0")
            digest.update(content)
    return QualifiedHost(
        target,
        platform.python_version(),
        importlib.metadata.version("skulk"),
        digest.hexdigest(),
    )


def verify_runtime(
    metadata: bytes, trust: RuntimeTrust, host: QualifiedHost, *, now: int
) -> VerifiedRuntime:
    """Authenticate complete v2 metadata, then enforce local trust and compatibility.

    Plugin-specific manifest fields are signed opaque data. No plugin package,
    provider policy or private dependency is imported to inspect a release.
    """
    if len(metadata) > 131072:
        raise ValueError("runtime metadata exceeds bound")
    signed = _Signed.model_validate_json(metadata)
    payload = canonical_json(signed.runtime)
    claims = _RuntimeClaims.model_validate_json(payload)
    release = claims.release
    public = trust.publishers.get(release.publisher)
    if (
        public is None
        or release.publisher in trust.revoked_publishers
        or now >= trust.expires_at
    ):
        raise ValueError("runtime publisher trust refused")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public)).verify(
            bytes.fromhex(signed.signature), payload
        )
    except (InvalidSignature, ValueError):
        raise ValueError("runtime signature refused") from None
    runtime = VerifiedRuntime(metadata, payload, claims)
    expected_os = "darwin" if host.platform == "macos-arm64" else "linux"
    if (
        not release.created_at <= now < release.expires_at
        or claims.platform != host.platform
        or expected_os not in release.platforms
        or host.python_version not in SpecifierSet(release.python_requires)
        or host.skulk_version not in SpecifierSet(release.manifest.skulk_requires)
        or host.skulk_build_sha256 != release.skulk_build_sha256
        or runtime.digest in trust.revoked_artifacts
        or release.manifest.executable_sha256 in trust.revoked_artifacts
        or any(wheel.sha256 in trust.revoked_artifacts for wheel in claims.wheels)
    ):
        raise ValueError("runtime compatibility or revocation refused")
    inventory = runtime.inventory
    if (
        len(inventory) != len(claims.wheels)
        or sum(wheel.size for wheel in claims.wheels) > 536870912
    ):
        raise ValueError("ambiguous or oversized runtime inventory")
    if (
        hashlib.sha256(canonical_json(dict(inventory))).hexdigest()
        != release.dependency_lock_sha256
    ):
        raise ValueError("runtime dependency inventory differs")
    return runtime


def verified_artifacts(runtime: VerifiedRuntime, directory: Path) -> dict[str, bytes]:
    """Verify exact runtime bytes and normalize unsupported archive failures."""
    try:
        return _verified_artifacts(runtime, directory)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError):
        raise ValueError("invalid or unsupported runtime archive") from None


def _verified_artifacts(runtime: VerifiedRuntime, directory: Path) -> dict[str, bytes]:
    """Verify exact bundle and wheels, archive paths, tags and complete dependencies."""
    release = runtime.claims.release
    bundle = read_private(directory / "bundle.pyz", release.artifact_size)
    if (
        len(bundle) != release.artifact_size
        or hashlib.sha256(bundle).hexdigest() != release.manifest.executable_sha256
    ):
        raise ValueError("runtime bundle differs")
    result = {"bundle.pyz": bundle}
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        entries = archive.infolist()
        if len(entries) > 20000 or sum(item.file_size for item in entries) > 268435456:
            raise ValueError("expanded bundle exceeds bound")
        if len({item.filename for item in entries}) != len(entries):
            raise ValueError("ambiguous bundle paths")
        for item in entries:
            path = PurePosixPath(item.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in item.filename
                or stat.S_ISLNK(item.external_attr >> 16)
            ):
                raise ValueError("unsafe bundle path")
        owner = next(
            (item for item in entries if item.filename == "__owner__.py"), None
        )
        if owner is None or not 0 < owner.file_size <= 65536:
            raise ValueError("bundle requires the fixed owner entrypoint")
    tags = set(sys_tags())
    expanded = 0
    inventory = runtime.inventory
    for wheel in runtime.claims.wheels:
        content = read_private(directory / wheel.filename, wheel.size)
        if (
            len(content) != wheel.size
            or hashlib.sha256(content).hexdigest() != wheel.sha256
        ):
            raise ValueError("runtime wheel differs")
        name, version, _, supported = parse_wheel_filename(wheel.filename)
        if not tags.intersection(supported):
            raise ValueError("wheel does not support this interpreter")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            expanded += sum(item.file_size for item in entries)
            if (
                len(entries) > 20000
                or expanded > 536870912
                or sum(item.file_size for item in entries) > 268435456
            ):
                raise ValueError("expanded runtime exceeds bound")
            if len({item.filename for item in entries}) != len(entries):
                raise ValueError("ambiguous wheel paths")
            for item in entries:
                path = PurePosixPath(item.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in item.filename
                    or stat.S_ISLNK(item.external_attr >> 16)
                ):
                    raise ValueError("unsafe wheel path")
            metadata = [
                item
                for item in entries
                if item.filename.endswith(".dist-info/METADATA")
            ]
            if len(metadata) != 1 or metadata[0].file_size > 1048576:
                raise ValueError("missing or oversized wheel metadata")
            parsed = BytesParser().parsebytes(archive.read(metadata[0]))
            if (
                canonicalize_name(str(parsed.get("Name", ""))) != name
                or Version(str(parsed.get("Version", "0"))) != version
            ):
                raise ValueError("wheel identity differs")
            for value in parsed.get_all("Requires-Dist", []):
                requirement = Requirement(str(value))
                if requirement.marker is not None and not requirement.marker.evaluate(
                    {"extra": ""}
                ):
                    continue
                dependency = str(canonicalize_name(requirement.name))
                if (
                    requirement.url is not None
                    or requirement.extras
                    or dependency not in inventory
                    or Version(inventory[dependency]) not in requirement.specifier
                ):
                    raise ValueError("incomplete or incompatible wheel dependency")
        result[wheel.filename] = content
    return result
