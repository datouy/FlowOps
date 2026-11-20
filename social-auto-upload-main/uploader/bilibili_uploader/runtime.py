from __future__ import annotations

import hashlib
import hmac
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import quote

import requests


GITHUB_RELEASE_API = "https://api.github.com/repos/biliup/biliup/releases/latest"
GITHUB_RELEASE_TAG_API = "https://api.github.com/repos/biliup/biliup/releases/tags/{tag}"
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_FILES = 5000


def get_biliup_runtime_root() -> Path:
    return Path.home() / ".social-auto-upload" / "tools" / "biliup"


def _normalize_system(system_name: str | None = None) -> str:
    system_value = (system_name or platform.system()).strip().lower()
    if system_value == "darwin":
        return "macos"
    return system_value


def _normalize_machine(machine_name: str | None = None) -> str:
    machine_value = (machine_name or platform.machine()).strip().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
    }
    return aliases.get(machine_value, machine_value)


def _build_platform_key(system_name: str | None = None, machine_name: str | None = None) -> str:
    return f"{_normalize_system(system_name)}-{_normalize_machine(machine_name)}"


def build_biliup_runtime_path(system_name: str | None = None) -> Path:
    executable_name = "biliup.exe" if _normalize_system(system_name) == "windows" else "biliup"
    return get_biliup_runtime_root() / _build_platform_key(system_name) / executable_name


def _build_biliup_version_path(system_name: str | None = None) -> Path:
    return build_biliup_runtime_path(system_name).with_name("version.txt")


def _select_release_asset(assets: list[dict]) -> dict:
    platform_key = _build_platform_key()
    preferred_patterns = {
        "windows-x86_64": ("x86_64-windows.zip",),
        "linux-x86_64": ("x86_64-linux.tar.xz",),
        "linux-aarch64": ("aarch64-linux.tar.xz",),
        "linux-arm": ("arm-linux.tar.xz",),
        "macos-x86_64": ("x86_64-macos.tar.xz",),
        "macos-aarch64": ("aarch64-macos.tar.xz",),
    }
    patterns = preferred_patterns.get(platform_key)
    if not patterns:
        raise RuntimeError(f"Unsupported biliup platform: {platform_key}")

    for asset in assets:
        asset_name = asset.get("name", "")
        if any(pattern in asset_name for pattern in patterns):
            return {
                "asset_name": asset_name,
                "asset_url": asset.get("browser_download_url", ""),
                "digest": str(asset.get("digest") or ""),
            }

    raise RuntimeError(f"No matching biliup release asset found for platform: {platform_key}")


def fetch_latest_release() -> dict:
    release_tag = str(os.environ.get("FLOWOPS_BILIBUP_RELEASE_TAG") or "").strip()
    api_url = (
        GITHUB_RELEASE_TAG_API.format(tag=quote(release_tag, safe=""))
        if release_tag else GITHUB_RELEASE_API
    )
    response = requests.get(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "social-auto-upload",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    selected_asset = _select_release_asset(payload.get("assets", []))
    return {
        "tag_name": payload.get("tag_name", ""),
        "asset_name": selected_asset["asset_name"],
        "asset_url": selected_asset["asset_url"],
        "digest": selected_asset["digest"],
    }


def read_local_biliup_version() -> str | None:
    version_path = _build_biliup_version_path()
    if not version_path.exists():
        return None
    return version_path.read_text(encoding="utf-8").strip() or None


def write_local_biliup_version(version: str) -> None:
    version_path = _build_biliup_version_path()
    version_path.parent.mkdir(parents=True, exist_ok=True)
    version_path.write_text(version, encoding="utf-8")


def _pick_executable(extract_root: Path) -> Path:
    candidates = []
    for path in extract_root.rglob("*"):
        if not path.is_file():
            continue
        lower_name = path.name.lower()
        if lower_name in {"biliup", "biliup.exe", "biliupr", "biliupr.exe"}:
            candidates.append(path)
    if not candidates:
        raise RuntimeError("Downloaded biliup archive does not contain a runnable executable")
    candidates.sort(key=lambda item: len(str(item)))
    return candidates[0]


def _archive_parts(name: str) -> tuple[str, ...]:
    value = str(name or "").replace("\\", "/")
    if not value or value.startswith("/") or (len(value) > 1 and value[1] == ":"):
        raise RuntimeError("拒绝解压绝对路径条目")
    parts = tuple(part for part in PurePosixPath(value).parts if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        raise RuntimeError("拒绝解压包含路径穿越的条目")
    return parts


def _safe_destination(root: Path, name: str) -> Path:
    target = root.joinpath(*_archive_parts(name)).resolve()
    if not target.is_relative_to(root.resolve()):
        raise RuntimeError("拒绝解压到目标目录之外")
    return target


def _copy_bounded(source, target: Path, remaining: int) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("wb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > remaining:
                raise RuntimeError("压缩包解压后超过安全体积上限")
            output.write(chunk)
    return written


def _extract_zip_safely(archive: Path, root: Path) -> None:
    total = 0
    with zipfile.ZipFile(archive) as handle:
        members = handle.infolist()
        if len(members) > MAX_ARCHIVE_FILES:
            raise RuntimeError("压缩包文件数量超过安全上限")
        for info in members:
            target = _safe_destination(root, info.filename)
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode) or (
                file_type and file_type not in (stat.S_IFREG, stat.S_IFDIR)
            ):
                raise RuntimeError("拒绝解压符号链接或特殊文件")
            if info.flag_bits & 0x1:
                raise RuntimeError("拒绝解压加密 ZIP 条目")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if info.file_size > MAX_EXTRACTED_BYTES - total:
                raise RuntimeError("压缩包声明体积超过安全上限")
            with handle.open(info, "r") as source:
                total += _copy_bounded(source, target, MAX_EXTRACTED_BYTES - total)


def _extract_tar_safely(archive: Path, root: Path) -> None:
    total = 0
    with tarfile.open(archive, "r:xz") as handle:
        members = handle.getmembers()
        if len(members) > MAX_ARCHIVE_FILES:
            raise RuntimeError("压缩包文件数量超过安全上限")
        for member in members:
            target = _safe_destination(root, member.name)
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise RuntimeError("拒绝解压符号链接、硬链接或设备文件")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise RuntimeError("压缩包包含不支持的特殊条目")
            if member.size > MAX_EXTRACTED_BYTES - total:
                raise RuntimeError("压缩包声明体积超过安全上限")
            source = handle.extractfile(member)
            if source is None:
                raise RuntimeError("无法读取压缩包文件条目")
            with source:
                total += _copy_bounded(source, target, MAX_EXTRACTED_BYTES - total)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_biliup_asset(release: dict, destination: Path) -> Path:
    asset_name = Path(str(release.get("asset_name") or "")).name
    asset_url = str(release.get("asset_url") or "").strip()
    expected_digest = str(release.get("digest") or "").strip().lower()
    if not asset_name or not asset_url.startswith("https://github.com/"):
        raise RuntimeError("Biliup 发布包地址或文件名不合法")
    if not expected_digest.startswith("sha256:"):
        if str(os.environ.get("FLOWOPS_BILIBUP_ALLOW_UNVERIFIED") or "").strip().lower() not in {
            "1", "true", "yes", "on",
        }:
            raise RuntimeError("Biliup 发布包缺少 SHA-256 摘要，已拒绝下载")
        expected_digest = ""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="biliup-download-") as temp_dir:
        temp_root = Path(temp_dir)
        archive_path = temp_root / asset_name
        total = 0
        with requests.get(asset_url, stream=True, timeout=(15, 120)) as response:
            response.raise_for_status()
            with archive_path.open("wb") as file_obj:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        total += len(chunk)
                        if total > MAX_ARCHIVE_BYTES:
                            raise RuntimeError("Biliup 发布包超过 256MB 安全上限")
                        file_obj.write(chunk)
        if expected_digest:
            actual_digest = _hash_file(archive_path)
            expected_hash = expected_digest.split(":", 1)[1]
            if len(expected_hash) != 64 or not hmac.compare_digest(actual_digest, expected_hash):
                raise RuntimeError("Biliup 发布包 SHA-256 校验失败")

        extract_root = temp_root / "extract"
        extract_root.mkdir(parents=True, exist_ok=True)
        if archive_path.suffix.lower() == ".zip":
            _extract_zip_safely(archive_path, extract_root)
        else:
            _extract_tar_safely(archive_path, extract_root)

        extracted_binary = _pick_executable(extract_root)
        temp_binary = destination.with_suffix(f"{destination.suffix}.tmp")
        shutil.copy2(extracted_binary, temp_binary)
        temp_binary.replace(destination)
        if _normalize_system() != "windows":
            destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return destination


def ensure_biliup_binary(force_check: bool = True) -> Path:
    binary_path = build_biliup_runtime_path()
    local_version = read_local_biliup_version()

    # 默认优先复用本地已存在的 biliup，避免每次执行都去请求 GitHub latest release。
    if binary_path.exists() and not force_check:
        return binary_path

    try:
        release = fetch_latest_release()
    except Exception:
        # 如果本地已经有可执行的 biliup，就在 GitHub 限流/网络失败时直接复用本地版本。
        if binary_path.exists():
            return binary_path
        raise

    latest_version = release["tag_name"]
    if binary_path.exists() and local_version == latest_version:
        return binary_path

    download_biliup_asset(release, binary_path)
    write_local_biliup_version(latest_version)
    return binary_path


def run_biliup_command(arguments: list[str], interactive: bool = False) -> subprocess.CompletedProcess[str]:
    binary_path = ensure_biliup_binary(force_check=False)
    command = [str(binary_path), *arguments]
    if interactive:
        return subprocess.run(command, check=False)
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
