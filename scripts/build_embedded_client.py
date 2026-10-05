#!/usr/bin/env python3
"""Build a Windows x64 multi-file client bundle entirely from Linux."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from build_timestamp import format_build_timestamp

PYTHON_VERSION = "3.11.9"
PYTHON_MINOR = "311"
PYTHON_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
    f"python-{PYTHON_VERSION}-embed-amd64.zip"
)
PYTHON_SHA256 = "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b"


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    """运行子命令，并在失败时保留原始退出码。"""
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def run_cached(command: list[str], cwd: Path, offline_only: bool) -> None:
    """优先使用 uv 缓存；缓存未命中时再联网下载。

    在线回退必须显式清掉 UV_OFFLINE/UV_FROZEN，否则父环境若带了离线标志，
    第二次调用仍会报 “not found in the cache / unsatisfiable”。
    """
    offline_env = os.environ.copy()
    offline_env["UV_OFFLINE"] = "1"
    try:
        run(command, cwd, offline_env)
        return
    except subprocess.CalledProcessError as exc:
        if offline_only:
            raise
        print(
            f"uv cache miss (exit {exc.returncode}); retrying with network access",
            flush=True,
        )
    online_env = os.environ.copy()
    for key in ("UV_OFFLINE", "UV_FROZEN", "UV_NO_SOURCES"):
        online_env.pop(key, None)
    # Prefer copy over hardlink when cache and target sit on different filesystems.
    online_env.setdefault("UV_LINK_MODE", "copy")
    run(command, cwd, online_env)


def download(url: str, destination: Path) -> None:
    """下载并缓存官方 CPython embeddable 压缩包。"""
    if destination.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    print(f"Downloading {url}", flush=True)
    with urllib.request.urlopen(url) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output)
    temporary.replace(destination)


def verify_python_archive(path: Path) -> None:
    """校验官方 runtime，防止缓存损坏或下载内容变化。"""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != PYTHON_SHA256:
        raise RuntimeError(f"unexpected CPython archive SHA-256: {digest}")


def copy_client_sources(source: Path, destination: Path) -> None:
    """复制运行所需源码，排除测试、状态和虚拟环境。"""
    ignored = {"tests", "data", ".venv", ".pytest_cache", "__pycache__"}
    for path in source.rglob("*.py"):
        relative = path.relative_to(source)
        if any(part in ignored for part in relative.parts):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    # Component-local client/version.json (overwrite later via stamp_client_version_json).
    # Do not copy the repo-root split version.json (server/client keys) into the bundle.
    candidate = source / "version.json"
    if candidate.is_file():
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            document = None
        if isinstance(document, dict) and document.get("version"):
            shutil.copy2(candidate, destination / "version.json")


def stamp_client_version_json(client_dir: Path, root: Path, build_date: str) -> None:
    """Write stamped version.json beside the packaged client package.

    ``client_dir`` is the packaged ``.../client`` package directory.
    ``build_date`` is ``yyyy-mm-dd HH:MM:SS.xxxx±xx:xx`` (legacy YYYY-MM-DD ok).
    """
    scripts = root / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        from build_timestamp import is_valid_build_timestamp
    finally:
        if str(scripts) in sys.path:
            sys.path.remove(str(scripts))
    if not is_valid_build_timestamp(build_date, allow_legacy_date=True):
        raise RuntimeError(
            f"invalid BUILD_DATE {build_date!r}; "
            "expected 'yyyy-mm-dd HH:MM:SS.xxxx±xx:xx' (or legacy YYYY-MM-DD)"
        )
    bundle_root = client_dir.parent if client_dir.name == "client" else client_dir
    sync = root / "scripts" / "sync_version_json.py"
    run(
        [
            sys.executable,
            str(sync),
            "--root",
            str(root),
            "--build-date",
            build_date,
            "--targets",
            "client-bundle",
            "--client-bundle-dir",
            str(bundle_root),
        ],
        root,
    )


def write_runtime_path(runtime: Path) -> None:
    """限制模块搜索路径到安装包内部，避免读取系统 Python 环境。"""
    content = "\n".join(
        [
            f"python{PYTHON_MINOR}.zip",
            ".",
            "..",
            "..\\Lib\\site-packages",
            "import site",
            "",
        ]
    )
    (runtime / f"python{PYTHON_MINOR}._pth").write_text(content, encoding="ascii")


def validate_windows_bundle(bundle: Path) -> None:
    """验证入口和扩展模块均为 Windows PE，防止混入 Linux ELF。"""
    required = [
        bundle / "lovemilk-class-broadcaster.exe",
        bundle / "lovemilk-class-broadcaster-updater.exe",
        bundle / "runtime" / "pythonw.exe",
        bundle / "runtime" / f"python{PYTHON_MINOR}.dll",
        # Qt Windows platform plugin is required for any GUI window.
        bundle / "Lib" / "site-packages" / "PySide6" / "plugins" / "platforms" / "qwindows.dll",
    ]
    for path in required:
        if not path.is_file() or path.read_bytes()[:2] != b"MZ":
            raise RuntimeError(f"Windows PE file is missing or invalid: {path}")
    foreign = [path for path in bundle.rglob("*.so")]
    if foreign:
        raise RuntimeError(f"Linux shared objects found in Windows bundle: {foreign[0]}")


def requirements_digest(path: Path) -> str:
    """Stable short hash of the Windows dependency lock input."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def ensure_windows_site_packages(root: Path, cache: Path, requirements: Path, offline: bool) -> Path:
    """Install Windows wheels once into a durable cache; reuse on later builds.

    ``uv pip install --target`` always writes into the output tree. The build used
    to wipe that tree every run, so packages were re-extracted even when the uv
    wheel cache already had them. Keep a full site-packages snapshot keyed by
    requirements contents instead.
    """
    key = requirements_digest(requirements)
    stamped = cache / "windows-site-packages" / f"cp311-win_amd64-{key}"
    marker = stamped / ".mkcb-site-packages.ok"
    if marker.is_file() and any(stamped.iterdir()):
        print(f"Reusing cached Windows site-packages: {stamped}", flush=True)
        return stamped

    if stamped.exists():
        shutil.rmtree(stamped)
    stamped.mkdir(parents=True, exist_ok=True)
    uv_command = [
        "uv",
        "pip",
        "install",
        "--target",
        str(stamped),
        "--python-version",
        "3.11",
        "--python-platform",
        "x86_64-pc-windows-msvc",
        "--only-binary",
        ":all:",
        "--requirements",
        str(requirements),
        "--cache-dir",
        str(cache / "uv"),
        # Windows wheels must come from PyPI (or mirror); never try to build sdist.
        "--no-compile",
    ]
    print(f"Installing Windows site-packages into cache {stamped}", flush=True)
    try:
        run_cached(uv_command, root, offline)
    except Exception:
        # Leave no half-filled snapshot that looks reusable next run.
        if stamped.exists():
            shutil.rmtree(stamped, ignore_errors=True)
        raise
    if not any(path.name != ".mkcb-site-packages.ok" for path in stamped.iterdir()):
        shutil.rmtree(stamped, ignore_errors=True)
        raise RuntimeError(f"Windows site-packages install produced an empty tree: {stamped}")
    marker.write_text(f"requirements_sha256_prefix={key}\n", encoding="ascii")
    return stamped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--python-archive", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--refresh-site-packages",
        action="store_true",
        help="Force reinstall of the cached Windows site-packages snapshot",
    )
    parser.add_argument(
        "--build-date",
        default=os.environ.get("BUILD_DATE") or format_build_timestamp(),
        help="build timestamp 'yyyy-mm-dd HH:MM:SS.xxxx±xx:xx' stamped into client/version.json",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    output = (args.output or root / "build/client/lovemilk-class-broadcaster").resolve()
    cache = root / "build/cache"
    archive = args.python_archive or cache / f"python-{PYTHON_VERSION}-embed-amd64.zip"
    requirements = root / "client/requirements.txt"
    if not archive.is_file():
        if args.offline:
            raise SystemExit(f"cached Python runtime is missing: {archive}")
        download(PYTHON_URL, archive)
    verify_python_archive(archive)

    if args.refresh_site_packages:
        key = requirements_digest(requirements)
        stamped = cache / "windows-site-packages" / f"cp311-win_amd64-{key}"
        shutil.rmtree(stamped, ignore_errors=True)

    site_cache = ensure_windows_site_packages(root, cache, requirements, args.offline)

    shutil.rmtree(output, ignore_errors=True)
    runtime = output / "runtime"
    site_packages = output / "Lib/site-packages"
    runtime.mkdir(parents=True)
    site_packages.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        package.extractall(runtime)
    write_runtime_path(runtime)

    # Copy the durable snapshot instead of re-running uv pip install every build.
    print(f"Copying site-packages from {site_cache}", flush=True)
    shutil.copytree(
        site_cache,
        site_packages,
        ignore=shutil.ignore_patterns(".mkcb-site-packages.ok"),
        dirs_exist_ok=False,
    )

    copy_client_sources(root / "client", output / "client")
    stamp_client_version_json(output / "client", root, str(args.build_date).strip())
    # 使用相同 minor 版本生成 -O 优化字节码；性能关键依赖来自原生 .pyd wheel。
    # version.json stays as data next to the bytecode package.
    run_cached(
        [
            "uv", "run", "--no-project", "--python", PYTHON_VERSION,
            "--cache-dir", str(cache / "uv"),
            "python", "-O", "-m", "compileall", "-q", "-f", "-b",
            str(output / "client"),
        ], root, args.offline,
    )
    # 发布包不携带项目源码；legacy .pyc 放在源码同目录，嵌入式 Python 可直接导入。
    for source in (output / "client").rglob("*.py"):
        source.unlink()
    for cache_dir in (output / "client").rglob("__pycache__"):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir)

    env = os.environ.copy()
    env.update({
        "CGO_ENABLED": "0", "GOOS": "windows", "GOARCH": "amd64",
        "GOCACHE": str(cache / "go"),
    })
    # Ensure PE icon resources exist (rsrc_windows_amd64.syso next to main packages).
    icon = root / "assets/icons/mkcb.ico"
    if icon.is_file():
        run(
            [
                sys.executable,
                str(root / "scripts/embed_windows_icon.py"),
                "--root",
                str(root),
                "--icon",
                str(icon),
                "--target",
                "launcher",
            ],
            root,
        )
    launcher = output / "lovemilk-class-broadcaster.exe"
    print("+ go build client launcher", flush=True)
    subprocess.run(
        ["go", "build", "-trimpath", "-ldflags=-s -w -H=windowsgui", "-o", str(launcher), "./cmd/client-launcher"],
        cwd=root, env=env, check=True,
    )
    updater = root / "build/updater/lovemilk-class-broadcaster-updater.exe"
    if not updater.is_file():
        raise RuntimeError(f"build updater first: {updater}")
    shutil.copy2(updater, output / updater.name)
    # Keep a copy of the icon beside the bundle for installers / shortcuts.
    if icon.is_file():
        shutil.copy2(icon, output / "mkcb.ico")
    validate_windows_bundle(output)
    print(f"Windows embedded client built: {output}")


if __name__ == "__main__":
    main()
