from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import os
import re

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.runtime.containers import ContainerManager

LOG = logging.getLogger("runtime.codex_update")

AUTO_UPDATE_ENV = "CAIRN_CODEX_NATIVE_AUTO_UPDATE"
TARGET_VERSION_ENV = "CAIRN_CODEX_NATIVE_VERSION"
TIMEOUT_ENV = "CAIRN_CODEX_NATIVE_UPDATE_TIMEOUT"
DEFAULT_TIMEOUT_SECONDS = 180
LOG_PREVIEW_LIMIT = 1200

PROBE_SCRIPT = r"""
set -eu
current="$(codex --version | awk 'NF {print $NF; exit}')"
target="${CAIRN_CODEX_NATIVE_VERSION:-latest}"
if [ "$target" = "latest" ]; then
  target="$(npm view @openai/codex version)"
fi
printf 'current=%s\n' "$current"
printf 'target=%s\n' "$target"
if [ "$current" = "$target" ]; then
  printf 'status=up_to_date\n'
  exit 0
fi
printf 'status=needs_update\n'
"""

INSTALL_SCRIPT = r"""
set -eu
target="${CAIRN_CODEX_NATIVE_VERSION:?}"
sudo npm install -g "@openai/codex@$target"
updated="$(codex --version | awk 'NF {print $NF; exit}')"
printf 'updated=%s\n' "$updated"
if [ "$updated" != "$target" ]; then
  printf 'status=version_mismatch\n'
  exit 1
fi
printf 'status=updated\n'
"""


@dataclass(slots=True)
class CodexNativeUpdateResult:
    checked: bool
    updated: bool = False
    current_version: str | None = None
    target_version: str | None = None
    image: str | None = None
    skipped_reason: str | None = None
    error: str | None = None


def has_codex_native_workers(config: DispatchConfig) -> bool:
    return any(
        worker.type == "codex" and worker.env.get("CODEX_AUTH_MODE") == "chatgpt"
        for worker in config.workers
    )


def ensure_codex_native_auto_updated(
    config: DispatchConfig,
    container_manager: ContainerManager,
) -> CodexNativeUpdateResult:
    if not has_codex_native_workers(config):
        return CodexNativeUpdateResult(checked=False, skipped_reason="no_codex_native_workers")

    if _auto_update_disabled():
        LOG.info("skip Codex native auto-update because %s is disabled", AUTO_UPDATE_ENV)
        return CodexNativeUpdateResult(checked=False, skipped_reason="disabled")

    container_name: str | None = None
    target_setting = os.environ.get(TARGET_VERSION_ENV) or "latest"
    timeout_seconds = _update_timeout_seconds()
    LOG.info(
        "[*] Codex native auto-update: image=%s target=%s timeout=%ss",
        config.container.image,
        target_setting,
        timeout_seconds,
    )
    try:
        container_name = container_manager.create_maintenance_container()
        probe = container_manager.build_exec_process(
            container_name,
            {TARGET_VERSION_ENV: target_setting},
            ["sh", "-lc", PROBE_SCRIPT],
            timeout_seconds=timeout_seconds,
        )
        probe.start()
        result = probe.communicate(timeout=timeout_seconds + 15)
        fields = _parse_fields(result.stdout)
        current = fields.get("current")
        target = fields.get("target")
        status = fields.get("status")
        if result.returncode != 0:
            error = _preview(result.stderr or result.stdout)
            LOG.warning(
                "Codex native auto-update failed code=%s current=%s target=%s error=%s",
                result.returncode,
                current or "-",
                target or target_setting,
                error or "-",
            )
            return CodexNativeUpdateResult(
                checked=True,
                current_version=current,
                target_version=target,
                error=error,
            )
        if status == "up_to_date":
            LOG.info("Codex native CLI already up to date version=%s", current or target or "-")
            return CodexNativeUpdateResult(
                checked=True,
                current_version=current,
                target_version=target,
            )
        if status != "needs_update":
            error = f"unexpected update status: {status or '-'}"
            LOG.warning("Codex native auto-update failed %s", error)
            return CodexNativeUpdateResult(
                checked=True,
                current_version=current,
                target_version=target,
                error=error,
            )

        image = _updated_image_name(
            config.container.image,
            container_manager.container_image_id(container_name),
            target,
        )
        if container_manager.image_exists(image):
            container_manager.use_image(image)
            LOG.info(
                "Codex native CLI using cached updated image current=%s target=%s image=%s",
                current or "-",
                target or "-",
                image,
            )
            return CodexNativeUpdateResult(
                checked=True,
                current_version=current,
                target_version=target,
                image=image,
            )

        install = container_manager.build_exec_process(
            container_name,
            {TARGET_VERSION_ENV: target or target_setting},
            ["sh", "-lc", INSTALL_SCRIPT],
            timeout_seconds=timeout_seconds,
        )
        install.start()
        install_result = install.communicate(timeout=timeout_seconds + 15)
        install_fields = _parse_fields(install_result.stdout)
        if install_result.returncode != 0 or install_fields.get("status") != "updated":
            error = _preview(install_result.stderr or install_result.stdout)
            LOG.warning(
                "Codex native auto-update failed during install code=%s current=%s target=%s error=%s",
                install_result.returncode,
                current or "-",
                target or target_setting,
                error or "-",
            )
            return CodexNativeUpdateResult(
                checked=True,
                current_version=current,
                target_version=target,
                error=error,
            )

        image = _commit_updated_image(container_manager, container_name, image)
        container_manager.use_image(image)
        LOG.info(
            "Codex native CLI updated current=%s target=%s image=%s",
            current or "-",
            target or "-",
            image,
        )
        return CodexNativeUpdateResult(
            checked=True,
            updated=True,
            current_version=current,
            target_version=target,
            image=image,
        )
    except Exception as exc:
        LOG.warning("Codex native auto-update crashed error=%s", exc)
        return CodexNativeUpdateResult(checked=True, target_version=target_setting, error=str(exc))
    finally:
        if container_name is not None:
            container_manager.remove_container(container_name, force=True)


def _commit_updated_image(
    container_manager: ContainerManager,
    container_name: str,
    image: str,
) -> str:
    repository, _, tag = image.rpartition(":")
    return container_manager.commit_container(container_name, repository=repository, tag=tag)


def _updated_image_name(
    base_image: str,
    base_image_id: str,
    target_version: str | None,
) -> str:
    version = _safe_tag_part(target_version or "unknown")
    base_hash = hashlib.sha256(f"{base_image}\n{base_image_id}".encode("utf-8")).hexdigest()[:12]
    repository = "cairn-worker-codex-native"
    tag = f"{base_hash}-codex-{version}"
    return f"{repository}:{tag}"


def _parse_fields(stdout: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and re.fullmatch(r"[a-z_]+", key):
            fields[key] = value
    return fields


def _safe_tag_part(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return sanitized or "unknown"


def _auto_update_disabled() -> bool:
    value = os.environ.get(AUTO_UPDATE_ENV)
    return value is not None and value.strip().lower() in {"0", "false", "no", "off", "disabled"}


def _update_timeout_seconds() -> int:
    value = os.environ.get(TIMEOUT_ENV)
    if value is None or not value.strip():
        return DEFAULT_TIMEOUT_SECONDS
    try:
        parsed = int(value)
    except ValueError:
        LOG.warning("invalid %s=%r, using %ss", TIMEOUT_ENV, value, DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS
    if parsed <= 0:
        LOG.warning("invalid %s=%r, using %ss", TIMEOUT_ENV, value, DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS
    return parsed


def _preview(text: str, limit: int = LOG_PREVIEW_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."
