from __future__ import annotations

from dataclasses import dataclass, field

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.runtime.codex_update import ensure_codex_native_auto_updated, has_codex_native_workers
from cairn.dispatcher.runtime.process import ProcessResult

from conftest import make_config


def _native_config():
    config = make_config()
    worker = WorkerConfig.model_validate(
        {
            "name": "codex-native",
            "type": "codex",
            "task_types": ["bootstrap", "reason", "explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "CODEX_MODEL": "gpt-test",
                "CODEX_AUTH_MODE": "chatgpt",
            },
        }
    )
    return config.model_copy(update={"workers": [worker]})


@dataclass
class FakeProcess:
    result: ProcessResult
    started: bool = False

    def start(self) -> None:
        self.started = True

    def communicate(self, timeout: int) -> ProcessResult:
        return self.result


@dataclass
class FakeContainerManager:
    processes: list[FakeProcess]
    cached_images: set[str] = field(default_factory=set)
    created: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    build_envs: list[dict[str, str]] = field(default_factory=list)
    build_commands: list[list[str]] = field(default_factory=list)
    build_timeouts: list[int | None] = field(default_factory=list)
    committed: list[tuple[str, str, str]] = field(default_factory=list)
    image: str | None = None

    def create_maintenance_container(self) -> str:
        self.created.append("maintenance")
        return "maintenance"

    def build_exec_process(self, container_name, env, command, timeout_seconds=None):
        self.build_envs.append(env)
        self.build_commands.append(command)
        self.build_timeouts.append(timeout_seconds)
        return self.processes.pop(0)

    def container_image_id(self, name: str) -> str:
        return "sha256:base-image"

    def image_exists(self, image: str) -> bool:
        return image in self.cached_images

    def commit_container(self, name: str, *, repository: str, tag: str) -> str:
        self.committed.append((name, repository, tag))
        return f"{repository}:{tag}"

    def use_image(self, image: str) -> None:
        self.image = image

    def remove_container(self, name: str, *, force: bool = True) -> None:
        self.removed.append(name)


def test_codex_native_detection_requires_chatgpt_mode() -> None:
    assert not has_codex_native_workers(make_config())
    assert has_codex_native_workers(_native_config())


def test_codex_native_auto_update_skips_without_native_worker() -> None:
    manager = FakeContainerManager([FakeProcess(ProcessResult(0, "", ""))])

    result = ensure_codex_native_auto_updated(make_config(), manager)  # type: ignore[arg-type]

    assert not result.checked
    assert result.skipped_reason == "no_codex_native_workers"
    assert manager.created == []


def test_codex_native_auto_update_commits_updated_image(monkeypatch) -> None:
    monkeypatch.delenv("CAIRN_CODEX_NATIVE_AUTO_UPDATE", raising=False)
    monkeypatch.delenv("CAIRN_CODEX_NATIVE_VERSION", raising=False)
    probe_stdout = "\n".join(
        [
            "current=0.118.0",
            "target=0.139.0",
            "status=needs_update",
        ]
    )
    install_stdout = "\n".join(["updated=0.139.0", "status=updated"])
    manager = FakeContainerManager(
        [
            FakeProcess(ProcessResult(0, probe_stdout, "")),
            FakeProcess(ProcessResult(0, install_stdout, "")),
        ]
    )

    result = ensure_codex_native_auto_updated(_native_config(), manager)  # type: ignore[arg-type]

    assert result.checked
    assert result.updated
    assert result.current_version == "0.118.0"
    assert result.target_version == "0.139.0"
    assert manager.created == ["maintenance"]
    assert manager.removed == ["maintenance"]
    assert manager.committed[0][0] == "maintenance"
    assert manager.image is not None
    assert manager.image.startswith("cairn-worker-codex-native:")
    assert manager.image.endswith("codex-0.139.0")
    assert manager.build_envs == [
        {"CAIRN_CODEX_NATIVE_VERSION": "latest"},
        {"CAIRN_CODEX_NATIVE_VERSION": "0.139.0"},
    ]
    assert manager.build_timeouts == [180, 180]


def test_codex_native_auto_update_reuses_cached_image(monkeypatch) -> None:
    monkeypatch.delenv("CAIRN_CODEX_NATIVE_AUTO_UPDATE", raising=False)
    probe_stdout = "\n".join(["current=0.118.0", "target=0.139.0", "status=needs_update"])
    manager = FakeContainerManager([FakeProcess(ProcessResult(0, probe_stdout, ""))])
    cached = "cairn-worker-codex-native:cdf67d635307-codex-0.139.0"
    manager.cached_images.add(cached)

    result = ensure_codex_native_auto_updated(_native_config(), manager)  # type: ignore[arg-type]

    assert result.checked
    assert not result.updated
    assert result.image == cached
    assert manager.image == cached
    assert manager.committed == []
    assert manager.processes == []


def test_codex_native_auto_update_keeps_image_when_up_to_date(monkeypatch) -> None:
    monkeypatch.delenv("CAIRN_CODEX_NATIVE_AUTO_UPDATE", raising=False)
    stdout = "\n".join(["current=0.139.0", "target=0.139.0", "status=up_to_date"])
    manager = FakeContainerManager([FakeProcess(ProcessResult(0, stdout, ""))])

    result = ensure_codex_native_auto_updated(_native_config(), manager)  # type: ignore[arg-type]

    assert result.checked
    assert not result.updated
    assert manager.committed == []
    assert manager.image is None
