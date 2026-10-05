"""客户端更新包校验、暂存与 updater 进程启动工具。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tarfile
from pathlib import Path
from typing import Any, Mapping, Optional



def _decompress_package(package: Path, temporary: Path) -> Path:
    """解压 .tar.zst（zstd magic）得到内部 tar；明文 tar 仅用于测试。"""
    with package.open("rb") as stream:
        magic = stream.read(4)
    if magic != b"\x28\xb5\x2f\xfd":
        if tarfile.is_tarfile(package):
            return package
        raise UpdateError("update package must be .tar.zst (zstd-compressed tar)")
    output = temporary / "package.tar"
    try:
        import zstandard as zstd  # type: ignore

        with package.open("rb") as source, output.open("wb") as destination:
            zstd.ZstdDecompressor().copy_stream(source, destination)
    except ImportError:
        executable = shutil.which("zstd")
        if not executable:
            raise UpdateError("zstd support is unavailable")
        with output.open("wb") as destination:
            result = subprocess.run([executable, "-d", "-q", "-c", str(package)], stdout=destination)
        if result.returncode:
            raise UpdateError("zstd decompression failed")
    if not tarfile.is_tarfile(output):
        raise UpdateError("update package is not a valid tar inside zstd")
    return output


class _Archive:
    """访问 files-v1 直接 TAR（.tar.zst 解压后）。"""

    def __init__(self, path: Path):
        self.path = path
        self.tar: Optional[tarfile.TarFile] = None
        if tarfile.is_tarfile(path):
            self.tar = tarfile.open(path, "r:")
        else:
            raise UpdateError("update package must be tar")

    def close(self) -> None:
        if self.tar:
            self.tar.close()

    def names(self) -> list[str]:
        return [item.name for item in self.tar.getmembers()]

    def members(self) -> list[Any]:
        return self.tar.getmembers()

    def is_dir(self, member: Any) -> bool:
        return member.isdir()

    def read(self, member: Any) -> bytes:
        stream = self.tar.extractfile(member)
        if stream is None:
            raise UpdateError(f"update archive entry cannot be read: {member.name}")
        with stream:
            return stream.read()

    def member_name(self, member: Any) -> str:
        return member.name


def _manifest_bytes(archive: _Archive) -> bytes:
    entries = []
    seen = set()
    for member in sorted(archive.members(), key=archive.member_name):
        name = archive.member_name(member)
        if name == "metadata.json" or archive.is_dir(member):
            continue
        if not name or name in seen:
            raise UpdateError(f"duplicate update path: {name}")
        seen.add(name)
        data = archive.read(member)
        entries.append({"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _verify_manifest(archive: _Archive, expected: str) -> None:
    if hashlib.sha256(_manifest_bytes(archive)).hexdigest() != expected.lower():
        raise UpdateError("update payload sha256 mismatch")


def _safe_member_path(target: Path, name: str, is_dir: bool) -> Path:
    """Resolve an archive member and reject traversal/absolute paths."""
    if not name or name in (".", ".."):
        raise UpdateError(f"update archive contains an unsafe path: {name}")
    destination = (target / name).resolve()
    root = target.resolve()
    if destination != root and root not in destination.parents:
        raise UpdateError(f"update archive contains an unsafe path: {name}")
    if destination == root and not is_dir:
        raise UpdateError(f"update archive contains an unsafe path: {name}")
    return destination


def _extract_members(archive: _Archive, target: Path, skip_metadata: bool = False) -> None:
    for member in archive.members():
        name = archive.member_name(member)
        if skip_metadata and name == "metadata.json":
            continue
        destination = _safe_member_path(target, name, archive.is_dir(member))
        if archive.is_dir(member):
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive.read(member))


class UpdateError(ValueError):
    """更新元数据、签名或包内容不符合要求。"""


def canonical_metadata(metadata: Mapping[str, Any]) -> bytes:
    """生成签名覆盖的稳定 JSON，不包含 signature 字段。"""
    signed = {key: value for key, value in metadata.items() if key != "signature"}
    return json.dumps(signed, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def normalize_components(metadata: Mapping[str, Any]) -> list[str]:
    """Return canonical components list (updater before client)."""
    raw = metadata.get("components")
    components: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            name = str(item).strip().lower()
            if name in ("client", "updater") and name not in components:
                components.append(name)
    if not components:
        legacy = str(metadata.get("component") or "").strip().lower()
        if legacy == "bundle":
            components = ["updater", "client"]
        elif legacy in ("client", "updater"):
            components = [legacy]
    rank = {"updater": 0, "client": 1}
    components.sort(key=lambda name: rank.get(name, 99))
    return components


def verify_metadata(metadata: Mapping[str, Any], public_key: bytes) -> None:
    """校验更新组件、版本和 SHA-256；当前版本不启用发布签名。"""
    required = ("version", "platform", "sha256")
    if any(not isinstance(metadata.get(key), str) or not metadata[key] for key in required):
        raise UpdateError("update metadata is incomplete")
    if not normalize_components(metadata):
        raise UpdateError("update components is incomplete")
    digest = str(metadata["sha256"]).lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise UpdateError("update sha256 is invalid")
    return


def sha256_file(path: Path) -> str:
    """以流式方式计算更新包摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def store_update_package(metadata: Mapping[str, Any], package_bytes: bytes, destination: Path) -> Path:
    """保存服务端通过业务连接直接发送的更新包为 ``*.tar.zst``。"""
    verify_metadata(metadata, b"")
    destination.mkdir(parents=True, exist_ok=True)
    digest = str(metadata.get("sha256") or "").lower()
    if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest):
        package = destination / f"{digest}.tar.zst"
    else:
        package = destination / "update-package.tar.zst"
    if len(package_bytes) >= 4 and package_bytes[:4] != b"\x28\xb5\x2f\xfd":
        raise UpdateError("update package must be .tar.zst (zstd-compressed tar)")
    package.write_bytes(package_bytes)
    with tempfile.TemporaryDirectory(prefix="mkcb-package-") as decompression:
        outer_path = _decompress_package(package, Path(decompression))
        archive = _Archive(outer_path)
        try:
            if "metadata.json" not in archive.names():
                raise UpdateError("update package metadata.json is missing")
            metadata_document = json.loads(archive.read(next(item for item in archive.members() if archive.member_name(item) == "metadata.json")))
            if any(metadata_document.get(key) != metadata.get(key) for key in ("version", "sha256")):
                raise UpdateError("embedded update metadata does not match")
            if normalize_components(metadata_document) != normalize_components(metadata):
                raise UpdateError("embedded update components do not match")
            if metadata_document.get("payload_format", "files-v1") != "files-v1":
                raise UpdateError("update package payload format is unsupported")
            _verify_manifest(archive, str(metadata["sha256"]))
        except Exception:
            package.unlink(missing_ok=True)
            raise
        finally:
            archive.close()
    return package


def stage_update(package: Path, metadata: Mapping[str, Any], staging_dir: Path) -> Path:
    """验证压缩包内 metadata.json，并安全解压到独立暂存目录。"""
    staging_dir.mkdir(parents=True, exist_ok=True)
    target = staging_dir / str(metadata["version"])
    if target.exists():
        shutil.rmtree(target)
    target.mkdir()
    with tempfile.TemporaryDirectory(prefix="mkcb-stage-") as temporary:
        outer_path = _decompress_package(package, Path(temporary))
        archive = _Archive(outer_path)
        try:
            names = set(archive.names())
            if "metadata.json" not in names:
                raise UpdateError("update package metadata.json is missing")
            metadata_member = next(item for item in archive.members() if archive.member_name(item) == "metadata.json")
            embedded = json.loads(archive.read(metadata_member).decode("utf-8"))
            if not isinstance(embedded, dict):
                raise UpdateError("embedded update metadata does not match")
            if str(embedded.get("version") or "") != str(metadata.get("version") or ""):
                raise UpdateError("embedded update metadata does not match")
            if str(embedded.get("sha256") or "").lower() != str(metadata.get("sha256") or "").lower():
                raise UpdateError("embedded update metadata does not match")
            if normalize_components(embedded) != normalize_components(metadata):
                raise UpdateError("embedded update components do not match")
            if embedded.get("payload_format", "files-v1") != "files-v1":
                raise UpdateError("update package payload format is unsupported")
            if "payload_format" in embedded:
                _verify_manifest(archive, str(metadata["sha256"]))
            else:
                payload_members = [item for item in archive.members() if archive.member_name(item) != "metadata.json" and not archive.is_dir(item)]
                if len(payload_members) != 1 or hashlib.sha256(archive.read(payload_members[0])).hexdigest() != str(metadata["sha256"]).lower():
                    raise UpdateError("update payload sha256 mismatch")
            _extract_members(archive, target, skip_metadata=True)
        finally:
            archive.close()
    return target


def verify_package_files(package: Path, metadata: Mapping[str, Any]) -> None:
    """Open the on-disk ``.tar.zst`` and verify embedded metadata + files-v1 digest."""
    verify_metadata(metadata, b"")
    with tempfile.TemporaryDirectory(prefix="mkcb-verify-") as temporary:
        outer_path = _decompress_package(package, Path(temporary))
        archive = _Archive(outer_path)
        try:
            if "metadata.json" not in archive.names():
                raise UpdateError("update package metadata.json is missing")
            metadata_member = next(item for item in archive.members() if archive.member_name(item) == "metadata.json")
            embedded = json.loads(archive.read(metadata_member).decode("utf-8"))
            if not isinstance(embedded, dict):
                raise UpdateError("embedded update metadata does not match")
            if str(embedded.get("version") or "") != str(metadata.get("version") or ""):
                raise UpdateError("embedded update metadata does not match")
            if str(embedded.get("sha256") or "").lower() != str(metadata.get("sha256") or "").lower():
                raise UpdateError("embedded update metadata does not match")
            if normalize_components(embedded) != normalize_components(metadata):
                raise UpdateError("embedded update components do not match")
            if embedded.get("payload_format", "files-v1") != "files-v1":
                raise UpdateError("update package payload format is unsupported")
            _verify_manifest(archive, str(metadata["sha256"]))
        finally:
            archive.close()


def resolve_install_dir() -> Path:
    """Installation root that the Go updater should overlay onto."""
    env = os.environ.get("MKCB_INSTALL_DIR")
    if env:
        return Path(env).resolve()
    # Source checkout: client/ package lives under repo/client.
    return Path(__file__).resolve().parents[1]


def resolve_updater_executable(install_dir: Optional[Path] = None) -> Path:
    """Locate ``lovemilk-class-broadcaster-updater(.exe)`` next to the install root."""
    root = install_dir or resolve_install_dir()
    names = (
        "lovemilk-class-broadcaster-updater.exe",
        "lovemilk-class-broadcaster-updater",
    )
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    raise UpdateError(f"updater executable not found under {root}")


def _windows_long_path(path: Path) -> str:
    """Return a Win32 path that bypasses MAX_PATH when needed (``\\\\?\\`` prefix)."""
    text = str(Path(path))
    if sys.platform != "win32":
        return text
    if text.startswith("\\\\?\\") or text.startswith("\\\\.\\"):
        return text
    resolved = str(Path(path).resolve())
    if resolved.startswith("\\\\"):
        # UNC: \\server\share\... → \\?\UNC\server\share\...
        return "\\\\?\\UNC\\" + resolved.lstrip("\\")
    return "\\\\?\\" + resolved


def prepare_apply_workspace(data_dir: Path, digest: str) -> Path:
    """Create a short apply work dir to stay under Windows MAX_PATH (~260).

    Full sha256 twice in the path easily exceeds MAX_PATH under deep Desktop
    install folders; use the first 16 hex chars only as the directory name.
    """
    short = str(digest or "").strip().lower()
    if len(short) < 8 or any(ch not in "0123456789abcdef" for ch in short):
        raise UpdateError(f"invalid update digest for apply workspace: {digest!r}")
    # 16 hex chars is unique enough for concurrent apply folders.
    work_dir = Path(data_dir) / "updates" / "apply" / short[:16]
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def copy_file_resilient(source: Path, destination: Path) -> Path:
    """Copy ``source`` → ``destination``, using long-path APIs on Windows when needed."""
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, destination)
        return destination
    except OSError as first_error:
        if sys.platform != "win32":
            raise
        # Retry with \\?\ so paths past MAX_PATH still work when long-path is enabled
        # or when the API accepts the extended prefix.
        try:
            src = _windows_long_path(source)
            dst = _windows_long_path(destination)
            parent = _windows_long_path(destination.parent)
            os.makedirs(parent, exist_ok=True)
            shutil.copy2(src, dst)
            return destination
        except OSError as second_error:
            raise UpdateError(
                "could not copy update file to apply workspace "
                f"(source={source} dest={destination} first={first_error} retry={second_error})"
            ) from second_error


def stage_package_for_apply(package_path: Path, work_dir: Path) -> Path:
    """Place a durable package copy in ``work_dir`` with a short fixed name.

    Avoids ``<64-hex>.tar.zst`` under ``apply/<64-hex>/`` which blows past MAX_PATH.
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    durable = work_dir / "pkg.tar.zst"
    source = Path(package_path)
    try:
        same = source.resolve() == durable.resolve()
    except OSError:
        same = False
    if same:
        return durable
    return copy_file_resilient(source, durable)


def prepare_detached_updater(updater: Path, work_dir: Path) -> Path:
    """Copy the updater PE out of the install tree so it can replace itself."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    destination = work_dir / Path(updater).name
    return copy_file_resilient(Path(updater), destination)


def launch_updater(
    updater: Path,
    package: Path,
    install_dir: Path,
    pid: Optional[int] = None,
    *,
    detach: bool = True,
    log_file: Optional[Path] = None,
) -> subprocess.Popen[Any]:
    """启动独立 updater；旧客户端退出后由 updater 完成替换。

    Updater writes its own log (default ``<install-dir>/data/updater.log``),
    separate from the client ``client.log`` / loguru sink.
    """
    package = Path(package)
    updater = Path(updater)
    install_dir = Path(install_dir)
    if not package.is_file():
        raise UpdateError(f"update package missing before launching updater: {package}")
    if not updater.is_file():
        raise UpdateError(f"detached updater missing: {updater}")
    command = [str(updater), "--package", str(package), "--install-dir", str(install_dir)]
    if pid is not None:
        command.extend(("--pid", str(pid)))
    log_path = Path(log_file) if log_file is not None else (install_dir / "data" / "updater.log")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    command.extend(("--log-file", str(log_path)))
    kwargs: dict[str, Any] = {}
    if detach:
        # Survive parent exit on Windows; avoid console flash for the GUI updater.
        if sys.platform == "win32":
            creationflags = 0
            for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
                creationflags |= int(getattr(subprocess, name, 0) or 0)
            kwargs["creationflags"] = creationflags
            kwargs["close_fds"] = True
        else:
            kwargs["start_new_session"] = True
            kwargs["close_fds"] = True
    else:
        kwargs["close_fds"] = True
    return subprocess.Popen(command, **kwargs)


def temporary_update_dir() -> tempfile.TemporaryDirectory[str]:
    """创建由调用方负责清理的更新临时目录。"""
    return tempfile.TemporaryDirectory(prefix="mkcb-update-")
