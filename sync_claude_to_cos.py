#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse, request

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_DOWNLOAD_BASE_URL = "https://downloads.claude.ai/claude-code-releases"
DEFAULT_GCS_BUCKET = "claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819"
DEFAULT_GCS_PREFIX = "claude-code-releases/"
DEFAULT_TIMEOUT_SECONDS = 30
UPLOAD_MAX_RETRIES = 3
UPLOAD_PART_SIZE_MB = 20
UPLOAD_MAX_THREADS = 2


def log(target_name: str, message: str, level: str = "info") -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{level}] [{target_name}] [{ts}] {message}", flush=True)


def format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


def format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class SyncConfig:
    channel: str
    download_base_url: str
    gcs_bucket: str
    gcs_prefix: str
    timeout_seconds: int
    upload_when_current: bool


@dataclass(frozen=True)
class CosConfigData:
    bucket: str
    region: str
    secret_id: str
    secret_key: str
    token: str | None
    scheme: str


@dataclass(frozen=True)
class TargetConfig:
    name: str
    platform: str
    local_path: Path
    metadata_path: Path
    cos_key: str


@dataclass(frozen=True)
class Settings:
    sync: SyncConfig
    cos: CosConfigData
    targets: tuple[TargetConfig, ...]


@dataclass(frozen=True)
class ReleaseInfo:
    target_name: str
    version: str
    platform: str
    binary_name: str
    checksum: str
    size: int
    download_url: str
    manifest_url: str


@dataclass(frozen=True)
class LocalInfo:
    exists: bool
    checksum: str | None = None
    version: str | None = None
    version_source: str | None = None
    matches_latest: bool = False


@dataclass
class RemoteCatalog:
    version: str
    manifest_url: str
    manifest_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    versions_cache: list[str] | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync Claude Code packages for multiple platforms and upload them to Tencent COS."
    )
    parser.add_argument("--config", default="sync_claude_exe.toml", help="Path to TOML config file.")
    parser.add_argument("--check-only", action="store_true", help="Only detect versions. Do not download or upload.")
    parser.add_argument("--skip-upload", action="store_true", help="Update local packages but skip COS upload.")
    parser.add_argument("--force-download", action="store_true", help="Download and replace even if local package is already current.")
    parser.add_argument("--force-upload", action="store_true", help="Upload to COS even if no local update happened.")
    parser.add_argument("--channel", help="Override configured release channel, for example latest, stable, or 2.1.126.")
    parser.add_argument("--target", action="append", help="Only process the named target. Can be passed multiple times.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        settings = load_settings(args)
        targets = filter_targets(settings.targets, args.target)
        catalog = build_remote_catalog(settings.sync)
        cos_client = None

        for target in targets:
            t_target_start = time.monotonic()
            log(target.name, f"--- processing target ---")
            release = build_release_info(settings.sync, catalog, target)
            local = inspect_local_file(settings.sync, catalog, target, release)
            print_status(settings.sync, target, local, release)

            if args.check_only:
                continue

            updated = False
            if args.force_download or not local.matches_latest:
                download_and_replace(settings.sync, target, release)
                updated = True
                local = LocalInfo(
                    exists=True,
                    checksum=release.checksum,
                    version=release.version,
                    version_source="download",
                    matches_latest=True,
                )
            elif local.exists and local.version:
                write_metadata(target.metadata_path, target.name, local.version, target.platform, local.checksum or "")

            should_upload = updated or args.force_upload or settings.sync.upload_when_current
            if args.skip_upload:
                log(target.name, "skip upload enabled; COS upload not executed.")
                continue

            if should_upload:
                if cos_client is None:
                    cos_client = build_cos_client(settings.cos)
                upload_to_cos(cos_client, settings.cos, target)
            else:
                log(target.name, "local package is already current; COS upload skipped.")

            log(target.name, f"target done in {format_elapsed(time.monotonic() - t_target_start)}")

        return 0
    except SyncError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


def load_settings(args: argparse.Namespace) -> Settings:
    config_path = Path(args.config).expanduser().resolve()
    config = load_toml_file(config_path) if config_path.exists() else {}
    base_dir = config_path.parent if config_path.exists() else Path.cwd()

    sync_cfg = config.get("claude") or config.get("sync") or {}
    cos_cfg = config.get("cos") or {}
    targets_cfg = config.get("targets") or []

    sync = SyncConfig(
        channel=args.channel or env_or_value("CLAUDE_SYNC_CHANNEL", sync_cfg.get("channel"), "latest"),
        download_base_url=env_or_value("CLAUDE_SYNC_DOWNLOAD_BASE_URL", sync_cfg.get("download_base_url"), DEFAULT_DOWNLOAD_BASE_URL).rstrip("/"),
        gcs_bucket=env_or_value("CLAUDE_SYNC_GCS_BUCKET", sync_cfg.get("gcs_bucket"), DEFAULT_GCS_BUCKET),
        gcs_prefix=env_or_value("CLAUDE_SYNC_GCS_PREFIX", sync_cfg.get("gcs_prefix"), DEFAULT_GCS_PREFIX),
        timeout_seconds=int(env_or_value("CLAUDE_SYNC_TIMEOUT_SECONDS", sync_cfg.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS)),
        upload_when_current=parse_bool(env_or_value("CLAUDE_SYNC_UPLOAD_WHEN_CURRENT", sync_cfg.get("upload_when_current"), False)),
    )

    cos = CosConfigData(
        bucket=resolve_secret_value(env_or_value("TENCENT_COS_BUCKET", cos_cfg.get("bucket"), "")),
        region=resolve_secret_value(env_or_value("TENCENT_COS_REGION", cos_cfg.get("region"), "")),
        secret_id=resolve_secret_value(env_or_value("TENCENT_COS_SECRET_ID", cos_cfg.get("secret_id"), "")),
        secret_key=resolve_secret_value(env_or_value("TENCENT_COS_SECRET_KEY", cos_cfg.get("secret_key"), "")),
        token=empty_to_none(resolve_secret_value(env_or_value("TENCENT_COS_TOKEN", cos_cfg.get("token"), ""))),
        scheme=env_or_value("TENCENT_COS_SCHEME", cos_cfg.get("scheme"), "https"),
    )

    targets = load_targets(base_dir, targets_cfg, sync_cfg, cos_cfg)
    if not targets:
        raise SyncError("no sync targets configured.")

    return Settings(sync=sync, cos=cos, targets=tuple(targets))


def load_targets(
    base_dir: Path,
    targets_cfg: list[dict[str, Any]],
    sync_cfg: dict[str, Any],
    cos_cfg: dict[str, Any],
) -> list[TargetConfig]:
    if targets_cfg:
        return [build_target(base_dir, target_cfg) for target_cfg in targets_cfg]

    if sync_cfg.get("platform") and sync_cfg.get("local_exe_path") and cos_cfg.get("key"):
        legacy_local_path = resolve_path(base_dir, str(sync_cfg["local_exe_path"]))
        return [
            TargetConfig(
                name=str(sync_cfg.get("platform") or "legacy"),
                platform=str(sync_cfg["platform"]),
                local_path=legacy_local_path,
                metadata_path=resolve_path(
                    base_dir,
                    str(sync_cfg.get("metadata_path") or default_metadata_path(legacy_local_path)),
                ),
                cos_key=normalize_cos_key(str(cos_cfg["key"])),
            )
        ]

    return []


def build_target(base_dir: Path, target_cfg: dict[str, Any]) -> TargetConfig:
    name = require_non_empty(str(target_cfg.get("name", "")).strip(), "Target name")
    platform = require_non_empty(str(target_cfg.get("platform", "")).strip(), f"Target platform ({name})")
    local_path = resolve_path(base_dir, require_non_empty(str(target_cfg.get("local_path", "")).strip(), f"Target local_path ({name})"))
    metadata_path_raw = str(target_cfg.get("metadata_path", "")).strip() or default_metadata_path(local_path)
    metadata_path = resolve_path(base_dir, metadata_path_raw)
    cos_key = normalize_cos_key(require_non_empty(str(target_cfg.get("cos_key", "")).strip(), f"Target cos_key ({name})"))
    return TargetConfig(
        name=name,
        platform=platform,
        local_path=local_path,
        metadata_path=metadata_path,
        cos_key=cos_key,
    )


def filter_targets(targets: tuple[TargetConfig, ...], selected_names: list[str] | None) -> tuple[TargetConfig, ...]:
    if not selected_names:
        return targets

    selected_set = {name.strip() for name in selected_names if name.strip()}
    filtered = tuple(target for target in targets if target.name in selected_set)
    if not filtered:
        raise SyncError(f"no targets matched: {', '.join(sorted(selected_set))}")
    return filtered


def build_remote_catalog(sync: SyncConfig) -> RemoteCatalog:
    version = resolve_remote_version(sync)
    manifest_url = f"{sync.download_base_url}/{version}/manifest.json"
    manifest = fetch_json(manifest_url, sync.timeout_seconds)
    return RemoteCatalog(
        version=version,
        manifest_url=manifest_url,
        manifest_cache={version: manifest},
    )


def build_release_info(sync: SyncConfig, catalog: RemoteCatalog, target: TargetConfig) -> ReleaseInfo:
    manifest = catalog.manifest_cache[catalog.version]
    platform_info = manifest.get("platforms", {}).get(target.platform)
    if not platform_info:
        raise SyncError(f"platform {target.platform} was not found in remote manifest.")

    checksum = str(platform_info.get("checksum", "")).strip().lower()
    size = int(platform_info.get("size", 0))
    binary_name = str(platform_info.get("binary", "")).strip()
    if not checksum or not binary_name:
        raise SyncError(f"manifest entry for platform {target.platform} is incomplete.")

    download_url = f"{sync.download_base_url}/{catalog.version}/{target.platform}/{binary_name}"
    return ReleaseInfo(
        target_name=target.name,
        version=catalog.version,
        platform=target.platform,
        binary_name=binary_name,
        checksum=checksum,
        size=size,
        download_url=download_url,
        manifest_url=catalog.manifest_url,
    )


def resolve_remote_version(sync: SyncConfig) -> str:
    channel = sync.channel.strip()
    if is_version_string(channel):
        return channel
    version = fetch_text(f"{sync.download_base_url}/{channel}", sync.timeout_seconds).strip()
    if not is_version_string(version):
        raise SyncError(f"remote channel {channel!r} returned an invalid version: {version!r}")
    return version


def inspect_local_file(sync: SyncConfig, catalog: RemoteCatalog, target: TargetConfig, release: ReleaseInfo) -> LocalInfo:
    if not target.local_path.exists():
        return LocalInfo(exists=False)

    checksum = sha256_file(target.local_path)
    if checksum == release.checksum:
        write_metadata(target.metadata_path, target.name, release.version, target.platform, checksum)
        return LocalInfo(
            exists=True,
            checksum=checksum,
            version=release.version,
            version_source="latest-manifest",
            matches_latest=True,
        )

    metadata = read_metadata(target.metadata_path)
    if metadata and metadata.get("checksum") == checksum and metadata.get("platform") == target.platform:
        version = str(metadata.get("version", "")).strip() or None
        if version:
            return LocalInfo(
                exists=True,
                checksum=checksum,
                version=version,
                version_source="local-metadata",
                matches_latest=(version == release.version),
            )

    version = resolve_version_by_checksum(sync, catalog, target, checksum)
    if version:
        write_metadata(target.metadata_path, target.name, version, target.platform, checksum)
        return LocalInfo(
            exists=True,
            checksum=checksum,
            version=version,
            version_source="checksum-scan",
            matches_latest=(version == release.version),
        )

    return LocalInfo(
        exists=True,
        checksum=checksum,
        version=None,
        version_source=None,
        matches_latest=False,
    )


def resolve_version_by_checksum(sync: SyncConfig, catalog: RemoteCatalog, target: TargetConfig, checksum: str) -> str | None:
    versions = list_remote_versions(sync, catalog)
    ordered_versions = [catalog.version] + [version for version in versions if version != catalog.version]

    for version in ordered_versions:
        manifest = get_manifest(sync, catalog, version)
        platform_info = manifest.get("platforms", {}).get(target.platform)
        if not platform_info:
            continue

        remote_checksum = str(platform_info.get("checksum", "")).strip().lower()
        if remote_checksum == checksum:
            return version
    return None


def list_remote_versions(sync: SyncConfig, catalog: RemoteCatalog) -> list[str]:
    if catalog.versions_cache is not None:
        return catalog.versions_cache

    versions: list[str] = []
    page_token: str | None = None

    while True:
        query = {
            "prefix": sync.gcs_prefix,
            "delimiter": "/",
        }
        if page_token:
            query["pageToken"] = page_token
        url = (
            f"https://storage.googleapis.com/storage/v1/b/{parse.quote(sync.gcs_bucket, safe='')}/o?"
            f"{parse.urlencode(query)}"
        )
        payload = fetch_json(url, sync.timeout_seconds)
        for prefix in payload.get("prefixes", []):
            prefix_text = str(prefix)
            if not prefix_text.startswith(sync.gcs_prefix):
                continue
            version = prefix_text[len(sync.gcs_prefix) :].strip("/")
            if is_version_string(version):
                versions.append(version)
        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    catalog.versions_cache = sorted(set(versions), key=version_sort_key, reverse=True)
    return catalog.versions_cache


def get_manifest(sync: SyncConfig, catalog: RemoteCatalog, version: str) -> dict[str, Any]:
    cached = catalog.manifest_cache.get(version)
    if cached is not None:
        return cached
    manifest_url = f"{sync.download_base_url}/{version}/manifest.json"
    manifest = fetch_json(manifest_url, sync.timeout_seconds)
    catalog.manifest_cache[version] = manifest
    return manifest


def version_sort_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def is_version_string(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 3 and all(part.isdigit() for part in parts)


def read_metadata(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_metadata(path: Path, target_name: str, version: str, platform: str, checksum: str) -> None:
    payload = {
        "target": target_name,
        "version": version,
        "platform": platform,
        "checksum": checksum,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        raise SyncError(f"failed to write metadata file: {path} ({exc})") from exc


def download_and_replace(sync: SyncConfig, target: TargetConfig, release: ReleaseInfo) -> None:
    log(target.name, f"downloading {release.version} ({format_bytes(release.size)}) from {release.download_url}")
    target.local_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    temp_path = download_release_binary(sync, target, release)
    elapsed = time.monotonic() - t0
    speed = release.size / elapsed if elapsed > 0 else 0
    log(target.name, f"download complete in {format_elapsed(elapsed)} ({format_bytes(int(speed))}/s)")
    try:
        backup_path = target.local_path.with_suffix(target.local_path.suffix + ".bak")
        if backup_path.exists():
            backup_path.unlink()

        if target.local_path.exists():
            log(target.name, f"backing up existing file to {backup_path.name}")
            os.replace(target.local_path, backup_path)

        try:
            os.replace(temp_path, target.local_path)
        except Exception:
            if backup_path.exists() and not target.local_path.exists():
                os.replace(backup_path, target.local_path)
            raise

        if backup_path.exists():
            backup_path.unlink()

        write_metadata(target.metadata_path, target.name, release.version, target.platform, release.checksum)
        log(target.name, f"local package updated to {release.version}")
    except OSError as exc:
        raise SyncError(f"failed to replace local package for {target.name}: {exc}") from exc
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def download_release_binary(sync: SyncConfig, target: TargetConfig, release: ReleaseInfo) -> Path:
    suffix = Path(release.binary_name).suffix or ".bin"
    handle, raw_path = tempfile.mkstemp(prefix=f"{target.name}-", suffix=suffix, dir=str(target.local_path.parent))
    os.close(handle)
    temp_path = Path(raw_path)

    try:
        chunk_size = 1024 * 1024  # 1 MB
        downloaded = 0
        last_reported_pct = -10
        with request.urlopen(build_request(release.download_url), timeout=sync.timeout_seconds) as response:
            with temp_path.open("wb") as output:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    output.write(chunk)
                    downloaded += len(chunk)
                    if release.size > 0:
                        pct = int(downloaded * 100 / release.size)
                        if pct >= last_reported_pct + 10:
                            last_reported_pct = pct - (pct % 10)
                            log(target.name, f"download progress: {last_reported_pct}% ({format_bytes(downloaded)}/{format_bytes(release.size)})")
    except (OSError, urlerror.URLError) as exc:
        temp_path.unlink(missing_ok=True)
        raise SyncError(f"failed to download release binary for {target.name}: {exc}") from exc

    log(target.name, f"verifying checksum …")
    actual_checksum = sha256_file(temp_path)
    if actual_checksum != release.checksum:
        temp_path.unlink(missing_ok=True)
        raise SyncError(
            f"download checksum mismatch for {target.name}: expected {release.checksum}, got {actual_checksum}"
        )
    log(target.name, f"checksum verified: {actual_checksum[:16]}…")
    return temp_path


def build_cos_client(cos: CosConfigData) -> Any:
    validate_cos_settings(cos)
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ModuleNotFoundError as exc:
        raise SyncError("qcloud_cos is not installed. Run: pip install -r requirements.txt") from exc

    config = CosConfig(
        Region=cos.region,
        SecretId=cos.secret_id,
        SecretKey=cos.secret_key,
        Token=cos.token,
        Scheme=cos.scheme,
    )
    return CosS3Client(config)


def upload_to_cos(client: Any, cos: CosConfigData, target: TargetConfig) -> None:
    normalized = normalize_cos_key(target.cos_key)
    file_size = target.local_path.stat().st_size
    log(target.name, (
        f"uploading to cos://{cos.bucket}/{normalized} "
        f"({format_bytes(file_size)}, "
        f"PartSize={UPLOAD_PART_SIZE_MB}MB, "
        f"Threads={UPLOAD_MAX_THREADS}, "
        f"MaxRetries={UPLOAD_MAX_RETRIES})"
    ))

    last_reported_pct: list[int] = [-10]

    def progress_callback(consumed: int, total: int) -> None:
        if total <= 0:
            return
        pct = int(consumed * 100 / total)
        if pct >= last_reported_pct[0] + 10:
            last_reported_pct[0] = pct - (pct % 10)
            log(target.name, f"upload progress: {last_reported_pct[0]}% ({format_bytes(consumed)}/{format_bytes(total)})")

    for attempt in range(1, UPLOAD_MAX_RETRIES + 1):
        last_reported_pct[0] = -10
        try:
            t0 = time.monotonic()
            client.upload_file(
                Bucket=cos.bucket,
                LocalFilePath=str(target.local_path),
                Key=normalized,
                PartSize=UPLOAD_PART_SIZE_MB,
                MAXThread=UPLOAD_MAX_THREADS,
                EnableMD5=True,
                progress_callback=progress_callback,
            )
            elapsed = time.monotonic() - t0
            speed = file_size / elapsed if elapsed > 0 else 0
            log(target.name, f"upload complete in {format_elapsed(elapsed)} ({format_bytes(int(speed))}/s)")
            return
        except Exception as exc:
            if attempt < UPLOAD_MAX_RETRIES:
                wait = 2 ** attempt  # exponential backoff: 2s, 4s
                log(target.name, f"upload attempt {attempt}/{UPLOAD_MAX_RETRIES} failed: {exc}", level="warn")
                log(target.name, f"retrying in {wait}s …")
                time.sleep(wait)
            else:
                raise SyncError(
                    f"failed to upload to COS key {normalized} after {UPLOAD_MAX_RETRIES} attempts: {exc}"
                ) from exc


def validate_cos_settings(cos: CosConfigData) -> None:
    require_non_empty(cos.bucket.strip(), "Tencent COS bucket")
    require_non_empty(cos.region.strip(), "Tencent COS region")
    require_non_empty(cos.secret_id.strip(), "Tencent COS SecretId")
    require_non_empty(cos.secret_key.strip(), "Tencent COS SecretKey")


def print_status(sync: SyncConfig, target: TargetConfig, local: LocalInfo, release: ReleaseInfo) -> None:
    log(target.name, f"channel: {sync.channel}")
    log(target.name, f"platform: {target.platform}")
    log(target.name, f"local file: {target.local_path}")
    log(target.name, f"cos key: {target.cos_key}")
    log(target.name, f"latest remote version: {release.version}")
    if local.exists:
        local_version = local.version or "unknown"
        local_source = local.version_source or "unresolved"
        log(target.name, f"local version: {local_version} ({local_source})")
        log(target.name, f"local sha256: {local.checksum}")
        if local.matches_latest:
            log(target.name, "local package already matches the latest release.")
        else:
            log(target.name, "local package is behind or unrecognized; update will be applied.")
    else:
        log(target.name, "local package does not exist; latest release will be downloaded.")


def load_toml_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise SyncError(f"failed to read config file: {path} ({exc})") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SyncError(f"failed to parse config file: {path} ({exc})") from exc


def resolve_path(base_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def default_metadata_path(local_path: Path) -> str:
    return str(local_path.parent / f"{local_path.name}.meta.json")


def env_or_value(env_name: str, configured: Any, default: Any) -> Any:
    raw = os.getenv(env_name)
    if raw is not None:
        return raw
    if configured is not None:
        return configured
    return default


def require_non_empty(value: str, label: str) -> str:
    if value:
        return value
    raise SyncError(f"{label} is required. Fill it in config or export the matching environment variable.")


def empty_to_none(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return value


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}


def normalize_cos_key(key: str) -> str:
    normalized = key.strip().lstrip("/")
    if not normalized:
        raise SyncError("Tencent COS object key cannot be empty.")
    return normalized


def resolve_secret_value(value: Any) -> str:
    text = str(value).strip()
    if text.startswith("env:"):
        env_name = text[4:].strip()
        if not env_name:
            raise SyncError("invalid env: reference in config")
        return os.getenv(env_name, "")
    return text


def fetch_text(url: str, timeout_seconds: int) -> str:
    try:
        with request.urlopen(build_request(url), timeout=timeout_seconds) as response:
            return response.read().decode("utf-8")
    except (OSError, UnicodeDecodeError, urlerror.URLError) as exc:
        raise SyncError(f"failed to fetch text from {url}: {exc}") from exc


def fetch_json(url: str, timeout_seconds: int) -> dict[str, Any]:
    try:
        with request.urlopen(build_request(url), timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8")
        return json.loads(payload)
    except (OSError, UnicodeDecodeError, urlerror.URLError, json.JSONDecodeError) as exc:
        raise SyncError(f"failed to fetch json from {url}: {exc}") from exc


def build_request(url: str) -> request.Request:
    return request.Request(
        url,
        headers={
            "User-Agent": "claude-package-sync/2.0",
            "Accept": "*/*",
        },
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SyncError(f"failed to read file for sha256: {path} ({exc})") from exc
    return digest.hexdigest().lower()


if __name__ == "__main__":
    raise SystemExit(main())
