#!/usr/bin/env python3
"""Set up a doc-hosting proof of concept deployment on Juju + microk8s.

Subcommands (``all`` runs setup -> build -> deploy):

* ``setup``:  install the required snaps (juju, microk8s, lxd, rockcraft,
  charmcraft), enable the microk8s addons (hostpath-storage, registry, dns)
  and bootstrap a Juju controller on microk8s. Idempotent.
* ``build``:  export the Python requirements, pack the FastAPI rock, push it
  to the microk8s local registry and pack the charm.
* ``deploy``: create the ``doc-hosting`` model, deploy MinIO (S3 storage
  backend) and the s3-integrator charm (which provides the ``s3`` interface
  to the doc-hosting-api charm, configured with the MinIO endpoint and
  credentials), deploy the doc-hosting-api charm, integrate everything,
  configure the publish token, wait for the applications to become active
  and write the connection details to ``.juju-deploy.env``.
* ``teardown``: destroy the model (add ``--controller`` to also destroy the
  Juju controller).

Note: MinIO's own ``s3-credentials`` endpoint only publishes
endpoint/credentials (no bucket), while the 12-factor charm's S3 integration
requires a bucket; the s3-integrator charm (track 2) fills that gap and also
creates the configured bucket.
"""

from __future__ import annotations

import argparse
import glob
import grp
import json
import os
import pathlib
import pwd
import re
import secrets
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MODEL = "doc-hosting"
CONTROLLER = "doc-hosting-controller"
CONTROLLER_NAMESPACE = f"controller-{CONTROLLER}"
APP = "doc-hosting-api"
MINIO = "minio"
MINIO_CHANNEL = "latest/edge"
S3_INTEGRATOR = "s3-integrator"
S3_INTEGRATOR_CHANNEL = "2/stable"
BUCKET = "doc-hosting"
MINIO_PORT = 9000
IMAGE_REPOSITORY = "localhost:32000/doc-hosting-api"
IMAGE_TAG = "0.1"
APP_IMAGE = f"{IMAGE_REPOSITORY}:{IMAGE_TAG}"
ENV_FILE = REPO_ROOT / ".juju-deploy.env"
WAIT_TIMEOUT = 900  # seconds
WAIT_INTERVAL = 10  # seconds
MICROK8S_READY_TIMEOUT = "300s"
MICROK8S_CHANNEL = "1.34-strict/stable"
MICROK8S_MINOR = "34"
BOOTSTRAP_TIMEOUT = 1800  # seconds; first-time controller image pulls can be slow
JUJU_CAAS_IMAGE_REPO = "public.ecr.aws/juju"
DEBUG = False


class DeployError(RuntimeError):
    """Raised on any deployment failure; reported to stderr with exit code 1."""


def run(
    cmd: list[str],
    *,
    cwd: pathlib.Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    interactive: bool = False,
    capture_output: bool | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, streaming output in interactive or debug mode."""
    merged_env = {**os.environ, **(env or {})}
    should_capture = (
        capture_output if capture_output is not None else not (interactive or DEBUG)
    )
    print(
        f"$ {' '.join(cmd)}" + (f"  (in {cwd})" if cwd else ""),
        flush=True,
    )
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=merged_env,
        capture_output=should_capture,
        text=True,
    )
    if check and result.returncode != 0:
        if not should_capture:
            raise DeployError(
                f"command failed ({' '.join(cmd)}), exit code {result.returncode}"
            )
        raise DeployError(
            f"command failed ({' '.join(cmd)}), exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def run_json(cmd: list[str], **kwargs: Any) -> Any:
    """Run a command and parse its JSON stdout without streaming JSON output."""
    kwargs["capture_output"] = True
    return json.loads(run(cmd, **kwargs).stdout)


def snap_installed(name: str) -> bool:
    result = subprocess.run(
        ["snap", "list", name], capture_output=True, text=True, check=False
    )
    return result.returncode == 0


def authenticate_sudo() -> None:
    """Acquire sudo credentials while its prompt is visible to the user."""
    if os.geteuid() == 0:
        return
    if not sys.stdin.isatty():
        raise DeployError(
            "setup needs an interactive terminal to request administrator access"
        )
    print("Administrator access is required to provision the local environment.")
    run(["sudo", "-v"], interactive=True)


def ensure_microk8s_group() -> None:
    """Ensure the user is configured for strict MicroK8s and can use it now."""
    username = pwd.getpwuid(os.getuid()).pw_name
    group_name = "snap_microk8s"
    account_groups = subprocess.run(
        ["id", "-nG", username],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if group_name not in account_groups:
        run(
            ["sudo", "usermod", "-aG", group_name, username],
            interactive=True,
        )
        raise DeployError(
            f"{username} was added to {group_name}. Log out and back in, then "
            "rerun `uv run scripts/deploy.py setup`."
        )

    current_group_ids = {*os.getgroups(), os.getgid()}
    current_groups = {grp.getgrgid(group_id).gr_name for group_id in current_group_ids}
    if group_name not in current_groups:
        raise DeployError(
            f"{username} is configured as a member of {group_name}, but this "
            "session has not picked it up. Log out and back in, then rerun "
            "`uv run scripts/deploy.py setup`."
        )


def install_snaps() -> None:
    """Install juju, microk8s, lxd, rockcraft and charmcraft snaps as needed."""
    snaps: list[tuple[str, list[str]]] = [
        ("lxd", ["sudo", "snap", "install", "lxd"]),
        ("juju", ["sudo", "snap", "install", "juju", "--channel", "3.6/stable"]),
        (
            "microk8s",
            ["sudo", "snap", "install", "microk8s", "--channel", MICROK8S_CHANNEL],
        ),
        (
            "rockcraft",
            ["sudo", "snap", "install", "rockcraft", "--channel", "latest/edge", "--classic"],
        ),
        (
            "charmcraft",
            ["sudo", "snap", "install", "charmcraft", "--channel", "latest/edge", "--classic"],
        ),
    ]
    for name, cmd in snaps:
        if snap_installed(name):
            print(f"snap {name}: already installed")
            continue
        run(cmd, interactive=True)

    # LXD needs initialising before it can be used (used by the builders).
    lxc = subprocess.run(
        ["lxc", "storage", "list", "--format=json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if lxc.returncode != 0:
        run(["sudo", "lxd", "init", "--auto"], interactive=True)

    ensure_microk8s_group()


def ensure_microk8s_version() -> None:
    """Reject an existing cluster that is not on the supported Kubernetes minor."""
    version = run_json(["microk8s", "kubectl", "version", "--output=json"])
    server_version = version.get("serverVersion") or {}
    minor = re.sub(r"\D.*$", "", str(server_version.get("minor", "")))
    if minor == MICROK8S_MINOR:
        return

    installed = server_version.get("gitVersion") or "unknown"
    raise DeployError(
        f"the existing MicroK8s cluster is Kubernetes {installed}, but this "
        f"project requires the {MICROK8S_CHANNEL} snap channel. Setup does not "
        "upgrade an existing cluster automatically because Kubernetes upgrades "
        "must proceed one minor release at a time. For this disposable local "
        "environment, remove it with `sudo snap remove microk8s --purge`, then "
        "rerun setup for a clean installation. To preserve workloads, back them "
        "up and follow the MicroK8s one-minor-at-a-time upgrade procedure instead."
    )


def deployment_ready(namespace: str, deployment: str) -> bool:
    """Return whether all desired replicas of a deployment are available."""
    info = run_json(
        [
            "microk8s",
            "kubectl",
            "get",
            "deployment",
            deployment,
            f"--namespace={namespace}",
            "--output=json",
        ]
    )
    desired = (info.get("spec") or {}).get("replicas", 1)
    status = info.get("status") or {}
    return (
        status.get("observedGeneration") == (info.get("metadata") or {}).get("generation")
        and status.get("updatedReplicas", 0) == desired
        and status.get("availableReplicas", 0) == desired
        and status.get("unavailableReplicas", 0) == 0
    )


def addon_diagnostics(deployment: str, selector: str) -> str:
    """Collect pod and event details for an unhealthy MicroK8s addon."""
    commands = [
        [
            "microk8s",
            "kubectl",
            "describe",
            "deployment",
            deployment,
            "--namespace=kube-system",
        ],
        [
            "microk8s",
            "kubectl",
            "get",
            "pods",
            "--namespace=kube-system",
            f"--selector={selector}",
            "--output=wide",
        ],
        [
            "microk8s",
            "kubectl",
            "describe",
            "pods",
            "--namespace=kube-system",
            f"--selector={selector}",
        ],
        [
            "microk8s",
            "kubectl",
            "logs",
            "--namespace=kube-system",
            f"--selector={selector}",
            "--all-containers=true",
            "--prefix=true",
            "--tail=200",
            "--ignore-errors=true",
        ],
        [
            "microk8s",
            "kubectl",
            "logs",
            "--namespace=kube-system",
            f"--selector={selector}",
            "--all-containers=true",
            "--prefix=true",
            "--tail=200",
            "--previous=true",
            "--ignore-errors=true",
        ],
        [
            "microk8s",
            "kubectl",
            "get",
            "events",
            "--namespace=kube-system",
            "--sort-by=.metadata.creationTimestamp",
        ],
    ]
    sections = []
    for command in commands:
        result = run(command, check=False, capture_output=True)
        output = (result.stdout or "") + (result.stderr or "")
        sections.append(f"$ {' '.join(command)}\n{output.strip()}")
    return "\n\n".join(sections)


def wait_for_addon_deployment(deployment: str, selector: str) -> None:
    """Wait for an addon, restarting one stale or failed rollout before giving up."""
    if deployment_ready("kube-system", deployment):
        print(f"MicroK8s addon deployment {deployment}: ready")
        return

    status_command = [
        "microk8s",
        "kubectl",
        "rollout",
        "status",
        "--namespace=kube-system",
        f"deployment/{deployment}",
        f"--timeout={MICROK8S_READY_TIMEOUT}",
    ]
    result = run(status_command, check=False)
    if result.returncode == 0 or deployment_ready("kube-system", deployment):
        return

    # Re-enabling an addon does not change its pod template, so Kubernetes can
    # retain a ProgressDeadlineExceeded condition from an earlier failed start.
    # A single restart resets that rollout and replaces a stuck pod.
    print(f"MicroK8s addon {deployment} is not ready; restarting it once")
    run(
        [
            "microk8s",
            "kubectl",
            "rollout",
            "restart",
            "--namespace=kube-system",
            f"deployment/{deployment}",
        ],
        interactive=True,
    )
    retry = run(status_command, check=False)
    if retry.returncode == 0 or deployment_ready("kube-system", deployment):
        return

    diagnostics = addon_diagnostics(deployment, selector)
    raise DeployError(
        f"MicroK8s addon deployment {deployment!r} did not become ready after "
        f"one restart.\nKubernetes diagnostics:\n{diagnostics}\n"
        "Look for FailedScheduling, ImagePullBackOff, readiness probe failures, "
        "or node pressure in the events above. Run `sudo microk8s inspect` for "
        "a full cluster report."
    )


def setup_microk8s() -> None:
    """Enable required addons and wait until their workloads are usable."""
    run(["microk8s", "status", "--wait-ready"], interactive=True)
    ensure_microk8s_version()
    run(["sudo", "microk8s", "enable", "hostpath-storage"], interactive=True)
    run(["sudo", "microk8s", "enable", "registry"], interactive=True)
    run(["sudo", "microk8s", "enable", "dns"], interactive=True)
    run(
        [
            "microk8s",
            "kubectl",
            "wait",
            "node",
            "--all",
            "--for=condition=Ready",
            f"--timeout={MICROK8S_READY_TIMEOUT}",
        ],
        interactive=True,
    )
    wait_for_addon_deployment("coredns", "k8s-app=kube-dns")
    wait_for_addon_deployment(
        "hostpath-provisioner", "k8s-app=hostpath-provisioner"
    )

    storage_class = run_json(
        [
            "microk8s",
            "kubectl",
            "get",
            "storageclass",
            "microk8s-hostpath",
            "--output=json",
        ]
    )
    annotations = (storage_class.get("metadata") or {}).get("annotations") or {}
    if annotations.get("storageclass.kubernetes.io/is-default-class") != "true":
        raise DeployError(
            "MicroK8s storage class 'microk8s-hostpath' is not the default; "
            "Juju needs a default storage class for its controller volume"
        )


def controller_exists() -> bool:
    result = subprocess.run(
        ["juju", "controllers", "--format=json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    controllers = json.loads(result.stdout).get("controllers") or {}
    return CONTROLLER in controllers


def namespace_exists(namespace: str) -> bool:
    """Return whether a namespace exists in the local MicroK8s cluster."""
    result = subprocess.run(
        ["microk8s", "kubectl", "get", "namespace", namespace],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    output = (result.stdout + result.stderr).lower()
    if "not found" in output:
        return False
    raise DeployError(
        f"could not inspect MicroK8s namespace {namespace!r}:\n"
        f"{result.stderr or result.stdout}"
    )


def controller_bootstrap_diagnostics() -> str:
    """Collect Kubernetes scheduling, storage and image diagnostics."""
    commands = [
        ["microk8s", "kubectl", "get", "nodes", "--output=wide"],
        [
            "microk8s",
            "kubectl",
            "get",
            "pod,statefulset,pvc,service",
            "--namespace",
            CONTROLLER_NAMESPACE,
            "--output=wide",
        ],
        [
            "microk8s",
            "kubectl",
            "describe",
            "pod",
            "controller-0",
            "--namespace",
            CONTROLLER_NAMESPACE,
        ],
        [
            "microk8s",
            "kubectl",
            "describe",
            "pvc",
            "--namespace",
            CONTROLLER_NAMESPACE,
        ],
        [
            "microk8s",
            "kubectl",
            "get",
            "events",
            "--namespace",
            CONTROLLER_NAMESPACE,
            "--sort-by=.metadata.creationTimestamp",
        ],
    ]
    sections = []
    for command in commands:
        result = run(command, check=False, capture_output=True)
        output = (result.stdout or "") + (result.stderr or "")
        sections.append(f"$ {' '.join(command)}\n{output.strip()}")
    return "\n\n".join(sections)


def bootstrap_controller() -> None:
    """Bootstrap Juju, preserving and diagnosing failed Kubernetes resources."""
    command = [
        "juju",
        "bootstrap",
        "microk8s",
        CONTROLLER,
        "--config",
        f"bootstrap-timeout={BOOTSTRAP_TIMEOUT}",
        "--config",
        f"caas-image-repo={JUJU_CAAS_IMAGE_REPO}",
        "--debug",
        "--verbose",
        "--keep-broken",
    ]
    result = run(command, check=False)
    if result.returncode == 0:
        return

    diagnostics = ""
    if namespace_exists(CONTROLLER_NAMESPACE):
        diagnostics = controller_bootstrap_diagnostics()
    raise DeployError(
        f"Juju controller bootstrap failed, exit code {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        + (f"\nKubernetes diagnostics:\n{diagnostics}" if diagnostics else "")
        + "\nThe failed resources were kept for inspection. After resolving the "
        "reported scheduling, storage, or image-pull problem, run "
        "`uv run scripts/deploy.py teardown --controller` and retry setup."
    )


def setup() -> None:
    """Install tooling, set up microk8s and bootstrap the Juju controller."""
    authenticate_sudo()
    install_snaps()
    setup_microk8s()
    if controller_exists():
        print(f"controller {CONTROLLER}: already bootstrapped")
        return
    if namespace_exists(CONTROLLER_NAMESPACE):
        raise DeployError(
            f"MicroK8s contains controller namespace {CONTROLLER_NAMESPACE!r}, "
            f"but the local Juju client has no controller named {CONTROLLER!r}. "
            "This usually means an earlier bootstrap created cluster resources "
            "but did not finish registering the controller locally. If the "
            "controller is disposable, remove it with "
            "`uv run scripts/deploy.py teardown --controller`, then rerun setup."
        )
    bootstrap_controller()


def find_newest(pattern: str, cwd: pathlib.Path) -> pathlib.Path:
    matches = [
        pathlib.Path(p)
        for p in glob.glob(str(cwd / pattern))
        if pathlib.Path(p).is_file()
    ]
    if not matches:
        raise DeployError(f"no file matching {pattern} in {cwd}")
    return max(matches, key=lambda p: p.stat().st_mtime)


def build() -> None:
    """Export requirements, pack the rock, push it to the registry, pack the charm."""
    run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-hashes",
            "--no-emit-project",
            "--format",
            "requirements.txt",
            "--output-file",
            "requirements.txt",
        ],
        cwd=REPO_ROOT,
    )
    run(
        ["rockcraft", "pack"],
        cwd=REPO_ROOT,
        env={"ROCKCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS": "true"},
    )
    rock = find_newest("doc-hosting-api_*.rock", REPO_ROOT)
    run(
        [
            "rockcraft.skopeo",
            "copy",
            "--insecure-policy",
            "--dest-tls-verify=false",
            f"oci-archive:{rock}",
            f"docker://{APP_IMAGE}",
        ]
    )
    run(
        ["charmcraft", "pack"],
        cwd=REPO_ROOT / "charm",
        env={"CHARMCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS": "true"},
    )
    charm = find_newest("doc-hosting-api_*.charm", REPO_ROOT / "charm")
    print(f"built {rock.name} (pushed to {APP_IMAGE}) and {charm.name}")


def model_exists() -> bool:
    models = run_json(["juju", "models", "--format=json"]).get("models", [])
    return any(
        model.get("short-name") == MODEL or model.get("name", "").endswith(f"/{MODEL}")
        for model in models
    )


def app_names() -> set[str]:
    status = run_json(["juju", "status", "--format=json", "-m", MODEL])
    return set(status.get("applications", {}).keys())


def juju_status() -> dict[str, Any]:
    return run_json(["juju", "status", "--format=json", "-m", MODEL])


def generate_credentials() -> tuple[str, str]:
    """Generate MinIO credentials (secret key must be at least 8 characters)."""
    access_key = "doc-hosting-" + secrets.token_hex(4)
    secret_key = secrets.token_urlsafe(24)
    return access_key, secret_key


def config_value(config: dict[str, Any], name: str) -> str:
    """Extract an option value from `juju config <app> --format=json` output."""
    option = config.get(name)
    if isinstance(option, dict):
        value = option.get("value")
        return str(value) if value is not None else ""
    return str(option) if option is not None else ""


def minio_credentials() -> tuple[str, str]:
    """Return the MinIO credentials, generating them for a new deployment."""
    if MINIO in app_names():
        config = run_json(["juju", "config", MINIO, "--format=json", "-m", MODEL])
        access_key = config_value(config, "access-key")
        secret_key = config_value(config, "secret-key")
        if access_key and secret_key:
            return access_key, secret_key
    return generate_credentials()


def publish_token() -> str:
    """Reuse the token recorded in the env file, or generate a new one."""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("API_TOKEN="):
                token = line.partition("=")[2].strip()
                if token:
                    return token
    return secrets.token_urlsafe(24)


def wait_for_apps(apps: list[str]) -> None:
    """Poll `juju status` until all apps are active (or fail loudly)."""
    deadline = time.monotonic() + WAIT_TIMEOUT
    current: dict[str, str] = {}
    while time.monotonic() < deadline:
        status = juju_status()
        current = {
            app: status.get("applications", {}).get(app, {}).get(
                "application-status", {}
            )
            for app in apps
        }
        current = {
            app: state.get("current", "unknown") if state else "pending"
            for app, state in current.items()
        }
        if all(state == "active" for state in current.values()):
            print(f"applications active: {current}")
            return
        print(f"waiting for {current} ...")
        time.sleep(WAIT_INTERVAL)
    raise DeployError(
        f"timed out after {WAIT_TIMEOUT}s waiting for {apps} to become active "
        f"(last status: {current})"
    )


def app_address(status: dict[str, Any], app: str) -> str:
    """Return a reachable address for an application from `juju status`."""
    application = status.get("applications", {}).get(app, {})
    if application.get("address"):
        return application["address"]
    for unit in application.get("units", {}).values():
        address = unit.get("address") or unit.get("public-address")
        if address:
            return address
    raise DeployError(f"no address found for {app} in `juju status`")


def relation_s3_data() -> dict[str, str]:
    """Scan s3 relation data on both sides for bucket/credentials/endpoint."""
    data: dict[str, str] = {}
    for unit in (f"{APP}/0", f"{S3_INTEGRATOR}/0"):
        result = subprocess.run(
            ["juju", "show-unit", unit, "--format=json", "-m", MODEL],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            continue
        info = json.loads(result.stdout).get(unit, {})
        for relation in info.get("relation-info", []):
            for key, value in (relation.get("application-data") or {}).items():
                if isinstance(value, str):
                    data.setdefault(key, value)
    return data


def host_reachable_endpoint(endpoint: str, minio_address: str) -> str:
    """Replace cluster-local hostnames in ``endpoint`` with a host-reachable address.

    The endpoint published over the s3 relation is the in-cluster DNS name of
    the MinIO service; the publish tooling runs on the host, so swap the
    cluster-local hostname for the address Juju reports for MinIO.
    """
    parsed = urlparse(endpoint)
    if parsed.hostname and (
        parsed.hostname.endswith(".svc.cluster.local")
        or parsed.hostname.endswith(".cluster.local")
    ):
        return urlunparse(parsed._replace(netloc=f"{minio_address}:{parsed.port or MINIO_PORT}"))
    return endpoint


def ensure_bucket(
    endpoint: str, access_key: str, secret_key: str, bucket: str | None
) -> str:
    """Return a usable bucket name, verifying or creating it via boto3."""
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )
    if bucket:
        try:
            client.head_bucket(Bucket=bucket)
            return bucket
        except ClientError:
            print(f"bucket {bucket!r} from the s3 relation not found; creating it")
            client.create_bucket(Bucket=bucket)
            return bucket
    buckets = [b["Name"] for b in client.list_buckets().get("Buckets", [])]
    if buckets:
        return sorted(buckets)[0]
    bucket = "doc-hosting"
    client.create_bucket(Bucket=bucket)
    print(f"created bucket {bucket} (none was provided by the s3 relation)")
    return bucket


def ensure_integrated() -> None:
    """Integrate doc-hosting-api:s3 with s3-integrator, tolerating existing relations."""
    result = run(
        ["juju", "integrate", "-m", MODEL, f"{APP}:s3", f"{S3_INTEGRATOR}:s3-credentials"],
        check=False,
        capture_output=True,
    )
    output = (result.stdout + result.stderr).lower()
    if result.returncode == 0 or "already" in output:
        return
    raise DeployError(
        f"could not integrate {APP}:s3 with {S3_INTEGRATOR}:s3-credentials: "
        f"{result.stderr or result.stdout}"
    )


def configure_s3_integrator(access_key: str, secret_key: str) -> None:
    """Deploy and configure s3-integrator with the MinIO endpoint and credentials.

    Track 2 of the s3-integrator charm keeps the credentials in a Juju secret
    (the ``credentials`` config option accepts a secret URI containing
    ``access-key`` and ``secret-key``) and creates the configured bucket in
    the backend if it does not exist yet.
    """
    in_cluster_endpoint = f"http://{MINIO}.{MODEL}.svc.cluster.local:{MINIO_PORT}"
    run(
        [
            "juju",
            "deploy",
            "-m",
            MODEL,
            S3_INTEGRATOR,
            "--channel",
            S3_INTEGRATOR_CHANNEL,
        ]
    )
    secret_label = f"doc-hosting-s3-credentials-{secrets.token_hex(3)}"
    result = run(
        [
            "juju",
            "add-secret",
            "-m",
            MODEL,
            secret_label,
            f"access-key={access_key}",
            f"secret-key={secret_key}",
        ],
        capture_output=True,
    )
    match = re.search(r"secret:[0-9a-z]+", result.stdout)
    if not match:
        raise DeployError(
            f"could not parse the secret URI from `juju add-secret` output: {result.stdout!r}"
        )
    secret_uri = match.group(0)
    run(["juju", "grant-secret", "-m", MODEL, secret_label, S3_INTEGRATOR])
    run(
        [
            "juju",
            "config",
            "-m",
            MODEL,
            S3_INTEGRATOR,
            f"endpoint={in_cluster_endpoint}",
            f"bucket={BUCKET}",
            f"credentials={secret_uri}",
        ]
    )


def deploy() -> None:
    """Deploy the stack and write the connection details to the env file."""
    if not model_exists():
        run(["juju", "add-model", MODEL])
    apps = app_names()

    access_key, secret_key = minio_credentials()
    if MINIO not in apps:
        run(
            [
                "juju",
                "deploy",
                "-m",
                MODEL,
                MINIO,
                "--channel",
                MINIO_CHANNEL,
                "--config",
                f"access-key={access_key}",
                "--config",
                f"secret-key={secret_key}",
            ]
        )
    else:
        print(f"{MINIO}: already deployed")

    if S3_INTEGRATOR not in apps:
        configure_s3_integrator(access_key, secret_key)
    else:
        print(f"{S3_INTEGRATOR}: already deployed")

    if APP not in apps:
        charm = find_newest("doc-hosting-api_*.charm", REPO_ROOT / "charm")
        run(
            [
                "juju",
                "deploy",
                "-m",
                MODEL,
                str(charm),
                APP,
                "--resource",
                f"app-image={APP_IMAGE}",
            ]
        )
    else:
        print(f"{APP}: already deployed")

    ensure_integrated()

    token = publish_token()
    run(["juju", "config", "-m", MODEL, APP, f"publish-token={token}"])

    wait_for_apps([MINIO, S3_INTEGRATOR, APP])

    status = juju_status()
    api_address = app_address(status, APP)
    minio_address = app_address(status, MINIO)
    api_url = f"http://{api_address}:8080"

    relation_data = relation_s3_data()
    bucket = relation_data.get("bucket") or BUCKET
    endpoint = relation_data.get("endpoint") or f"http://{minio_address}:{MINIO_PORT}"
    if "://" not in endpoint:
        endpoint = f"http://{endpoint}"
    endpoint = host_reachable_endpoint(endpoint, minio_address)
    relation_access_key = relation_data.get("access-key") or access_key
    relation_secret_key = relation_data.get("secret-key") or secret_key
    bucket = ensure_bucket(endpoint, relation_access_key, relation_secret_key, bucket)

    ENV_FILE.write_text(
        "\n".join(
            [
                f"API_URL={api_url}",
                f"API_TOKEN={token}",
                f"S3_ENDPOINT={endpoint}",
                f"S3_ACCESS_KEY={relation_access_key}",
                f"S3_SECRET_KEY={relation_secret_key}",
                f"S3_BUCKET={bucket}",
                "S3_REGION=us-east-1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"\nconnection details written to {ENV_FILE}\n")
    print("Quickstart:")
    print(f"  source {ENV_FILE}")
    print("  uv sync --group docs")
    print("  uv run --group docs sphinx-build -b dirhtml docs docs/_build/dirhtml")
    print(
        "  uv run scripts/publish.py --env-file .juju-deploy.env "
        "--build-dir docs/_build/dirhtml"
    )
    print(f"  curl {api_url}/docs/en/latest/")
    print(
        "\nIf the ClusterIP addresses above are not reachable from your machine, "
        f"use port forwarding instead, e.g.:\n"
        f"  microk8s kubectl port-forward -n {MODEL} svc/{MINIO} 9000:9000\n"
        f"  microk8s kubectl port-forward -n {MODEL} pod/{APP}-0 8080:8080\n"
        "(then set S3_ENDPOINT=http://localhost:9000 and API_URL=http://localhost:8080)"
    )


def delete_orphaned_controller() -> None:
    """Remove this project's unregistered controller directly from MicroK8s."""
    if not namespace_exists(CONTROLLER_NAMESPACE):
        print(f"controller namespace {CONTROLLER_NAMESPACE}: not present")
        return

    namespace = run_json(
        [
            "microk8s",
            "kubectl",
            "get",
            "namespace",
            CONTROLLER_NAMESPACE,
            "--output=json",
        ]
    )
    metadata = namespace.get("metadata") or {}
    annotations = metadata.get("annotations") or {}
    labels = metadata.get("labels") or {}
    is_juju_controller = (
        annotations.get("controller.juju.is/is-controller") == "true"
        or labels.get("model.juju.is/name") == "controller"
        or labels.get("juju-model") == "controller"
    )
    if not is_juju_controller:
        raise DeployError(
            f"refusing to delete namespace {CONTROLLER_NAMESPACE!r}: it is not "
            "marked as a Juju controller"
        )

    run(
        [
            "microk8s",
            "kubectl",
            "delete",
            "namespace",
            CONTROLLER_NAMESPACE,
            "--wait=true",
        ],
        interactive=True,
    )
    run(
        [
            "microk8s",
            "kubectl",
            "delete",
            "clusterrolebinding",
            CONTROLLER_NAMESPACE,
            "--ignore-not-found",
        ],
        interactive=True,
    )


def teardown(destroy_controller: bool) -> None:
    """Destroy the model and optionally its controller, including orphan recovery."""
    if controller_exists():
        if model_exists():
            run(
                [
                    "juju",
                    "destroy-model",
                    MODEL,
                    "--force",
                    "--no-prompt",
                    "--destroy-storage",
                ]
            )
        else:
            print(f"model {MODEL}: not present")
        if not destroy_controller:
            return
        run(
            [
                "juju",
                "destroy-controller",
                CONTROLLER,
                "--destroy-all-models",
                "--no-prompt",
            ]
        )
        return

    print(f"controller {CONTROLLER}: not registered with the local Juju client")
    if destroy_controller:
        delete_orphaned_controller()
    elif namespace_exists(CONTROLLER_NAMESPACE):
        raise DeployError(
            f"cannot destroy model {MODEL!r} through Juju because controller "
            f"{CONTROLLER!r} is not locally registered. To remove the disposable "
            "orphaned controller, rerun with `teardown --controller`."
        )


def main(argv: list[str] | None = None) -> int:
    global DEBUG

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["setup", "build", "deploy", "all", "teardown"],
        help="subcommand to run (default: all)",
    )
    parser.add_argument(
        "--controller",
        action="store_true",
        help="teardown: also destroy the Juju controller",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="stream command output to the console",
    )
    args = parser.parse_args(argv)
    DEBUG = args.debug
    try:
        if args.command == "setup":
            setup()
        elif args.command == "build":
            build()
        elif args.command == "deploy":
            deploy()
        elif args.command == "teardown":
            teardown(args.controller)
        else:
            setup()
            build()
            deploy()
        return 0
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
