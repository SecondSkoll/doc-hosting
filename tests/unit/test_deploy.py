"""Unit tests for local Juju deployment state and orphan recovery."""

from __future__ import annotations

import subprocess

import pytest

from scripts import deploy


def completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Build a subprocess result for command mocks."""
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_run_captures_output_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(kwargs) or completed(),
    )
    monkeypatch.setattr(deploy, "DEBUG", False)

    deploy.run(["rockcraft", "pack"])

    assert calls[0]["capture_output"] is True


def test_run_streams_output_in_debug_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(kwargs) or completed(),
    )
    monkeypatch.setattr(deploy, "DEBUG", True)

    deploy.run(["rockcraft", "pack"])

    assert calls[0]["capture_output"] is False


def test_run_json_still_captures_output_in_debug_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(kwargs)
        or completed(stdout='{"ready": true}'),
    )
    monkeypatch.setattr(deploy, "DEBUG", True)

    assert deploy.run_json(["example", "--format=json"]) == {"ready": True}
    assert calls[0]["capture_output"] is True


def test_namespace_exists(monkeypatch):
    monkeypatch.setattr(deploy.subprocess, "run", lambda *args, **kwargs: completed())
    assert deploy.namespace_exists("present") is True

    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda *args, **kwargs: completed(1, stderr='namespace "missing" not found'),
    )
    assert deploy.namespace_exists("missing") is False


def test_namespace_exists_reports_unexpected_error(monkeypatch):
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda *args, **kwargs: completed(1, stderr="connection refused"),
    )
    with pytest.raises(deploy.DeployError, match="connection refused"):
        deploy.namespace_exists("unknown")


def test_ensure_microk8s_version_accepts_supported_minor(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run_json",
        lambda command: {
            "serverVersion": {"minor": "34+", "gitVersion": "v1.34.9"}
        },
    )

    deploy.ensure_microk8s_version()


def test_ensure_microk8s_version_rejects_stale_cluster(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run_json",
        lambda command: {
            "serverVersion": {"minor": "28", "gitVersion": "v1.28.15"}
        },
    )

    with pytest.raises(deploy.DeployError, match="snap remove microk8s --purge"):
        deploy.ensure_microk8s_version()


def test_deployment_ready_requires_current_available_replicas(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run_json",
        lambda command: {
            "metadata": {"generation": 3},
            "spec": {"replicas": 1},
            "status": {
                "observedGeneration": 3,
                "updatedReplicas": 1,
                "availableReplicas": 1,
            },
        },
    )
    assert deploy.deployment_ready("kube-system", "coredns") is True


def test_wait_for_addon_accepts_ready_deployment_without_rollout(monkeypatch):
    monkeypatch.setattr(deploy, "deployment_ready", lambda namespace, name: True)
    commands = []
    monkeypatch.setattr(deploy, "run", lambda command, **kwargs: commands.append(command))

    deploy.wait_for_addon_deployment("coredns", "k8s-app=kube-dns")

    assert commands == []


def test_wait_for_addon_restarts_failed_rollout_once(monkeypatch):
    readiness = iter([False, False, True])
    monkeypatch.setattr(
        deploy, "deployment_ready", lambda namespace, name: next(readiness)
    )
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return completed(1, stderr="progress deadline exceeded")

    monkeypatch.setattr(deploy, "run", fake_run)

    deploy.wait_for_addon_deployment("coredns", "k8s-app=kube-dns")

    assert any("restart" in command for command, _ in commands)


def test_wait_for_addon_reports_diagnostics_after_retry(monkeypatch):
    monkeypatch.setattr(deploy, "deployment_ready", lambda namespace, name: False)
    monkeypatch.setattr(
        deploy,
        "run",
        lambda command, **kwargs: completed(1, stderr="progress deadline exceeded"),
    )
    monkeypatch.setattr(
        deploy,
        "addon_diagnostics",
        lambda deployment, selector: "ImagePullBackOff: DNS unavailable",
    )

    with pytest.raises(deploy.DeployError, match="ImagePullBackOff"):
        deploy.wait_for_addon_deployment("coredns", "k8s-app=kube-dns")


def test_setup_rejects_orphaned_controller(monkeypatch):
    monkeypatch.setattr(deploy, "authenticate_sudo", lambda: None)
    monkeypatch.setattr(deploy, "install_snaps", lambda: None)
    monkeypatch.setattr(deploy, "setup_microk8s", lambda: None)
    monkeypatch.setattr(deploy, "controller_exists", lambda: False)
    monkeypatch.setattr(deploy, "namespace_exists", lambda namespace: True)
    commands = []
    monkeypatch.setattr(deploy, "run", lambda command, **kwargs: commands.append(command))

    with pytest.raises(deploy.DeployError, match="teardown --controller"):
        deploy.setup()
    assert commands == []


def test_bootstrap_controller_uses_resilient_options(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return completed()

    monkeypatch.setattr(deploy, "run", fake_run)

    deploy.bootstrap_controller()

    command, kwargs = commands[0]
    assert f"bootstrap-timeout={deploy.BOOTSTRAP_TIMEOUT}" in command
    assert f"caas-image-repo={deploy.JUJU_CAAS_IMAGE_REPO}" in command
    assert "--keep-broken" in command
    assert kwargs == {"check": False}


def test_bootstrap_controller_includes_cluster_diagnostics(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run",
        lambda command, **kwargs: completed(1, stderr="controller pod pending"),
    )
    monkeypatch.setattr(deploy, "namespace_exists", lambda namespace: True)
    monkeypatch.setattr(
        deploy,
        "controller_bootstrap_diagnostics",
        lambda: "FailedScheduling: insufficient memory",
    )

    with pytest.raises(deploy.DeployError, match="insufficient memory"):
        deploy.bootstrap_controller()


def test_delete_orphaned_controller_verifies_and_removes_resources(monkeypatch):
    monkeypatch.setattr(deploy, "namespace_exists", lambda namespace: True)
    monkeypatch.setattr(
        deploy,
        "run_json",
        lambda command: {
            "metadata": {
                "annotations": {"controller.juju.is/is-controller": "true"}
            }
        },
    )
    commands = []
    monkeypatch.setattr(
        deploy,
        "run",
        lambda command, **kwargs: commands.append((command, kwargs)),
    )

    deploy.delete_orphaned_controller()

    assert commands[0][0][2:5] == ["delete", "namespace", deploy.CONTROLLER_NAMESPACE]
    assert commands[1][0][2:5] == [
        "delete",
        "clusterrolebinding",
        deploy.CONTROLLER_NAMESPACE,
    ]
    assert all(kwargs["interactive"] for _, kwargs in commands)


def test_delete_orphaned_controller_refuses_unmarked_namespace(monkeypatch):
    monkeypatch.setattr(deploy, "namespace_exists", lambda namespace: True)
    monkeypatch.setattr(deploy, "run_json", lambda command: {"metadata": {}})

    with pytest.raises(deploy.DeployError, match="refusing to delete"):
        deploy.delete_orphaned_controller()


def test_teardown_recovers_unregistered_controller_only_when_explicit(monkeypatch):
    monkeypatch.setattr(deploy, "controller_exists", lambda: False)
    monkeypatch.setattr(deploy, "namespace_exists", lambda namespace: True)
    deleted = []
    monkeypatch.setattr(deploy, "delete_orphaned_controller", lambda: deleted.append(True))

    with pytest.raises(deploy.DeployError, match="teardown --controller"):
        deploy.teardown(False)
    assert deleted == []

    deploy.teardown(True)
    assert deleted == [True]


def test_project_secret_is_reused_from_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".juju-deploy.env"
    monkeypatch.setattr(deploy, "ENV_FILE", env_file)
    env_file.write_text("PROJECT_SECRET=stored-secret\n", encoding="utf-8")

    assert deploy.project_secret() == "stored-secret"


def test_project_secret_is_generated_and_stable(monkeypatch, tmp_path):
    env_file = tmp_path / ".juju-deploy.env"
    monkeypatch.setattr(deploy, "ENV_FILE", env_file)

    first = deploy.project_secret()
    assert first
    env_file.write_text(f"PROJECT_SECRET={first}\n", encoding="utf-8")
    assert deploy.project_secret() == first


def test_deploy_or_refresh_app_deploys_new_application(monkeypatch, tmp_path):
    charm = tmp_path / "doc-hosting-api.charm"
    charm.touch()
    monkeypatch.setattr(deploy, "find_newest", lambda pattern, cwd: charm)
    commands = []
    monkeypatch.setattr(deploy, "run", lambda command, **kwargs: commands.append(command))

    deploy.deploy_or_refresh_app(set())

    assert commands == [
        [
            "juju",
            "deploy",
            "-m",
            deploy.MODEL,
            str(charm),
            deploy.APP,
            "--resource",
            f"app-image={deploy.APP_IMAGE}",
        ]
    ]


def test_deploy_or_refresh_app_updates_existing_resource(monkeypatch, tmp_path):
    charm = tmp_path / "doc-hosting-api.charm"
    charm.touch()
    monkeypatch.setattr(deploy, "find_newest", lambda pattern, cwd: charm)
    commands = []
    monkeypatch.setattr(deploy, "run", lambda command, **kwargs: commands.append(command))

    deploy.deploy_or_refresh_app({deploy.APP})

    assert commands == [
        [
            "juju",
            "refresh",
            "-m",
            deploy.MODEL,
            deploy.APP,
            "--path",
            str(charm),
            "--resource",
            f"app-image={deploy.APP_IMAGE}",
        ]
    ]
