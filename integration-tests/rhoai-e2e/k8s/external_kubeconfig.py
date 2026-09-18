"""Upload local kubeconfig as a tenant Secret for external-cluster rhoai-e2e runs."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from suite.constants import (
    ANNOTATION_CLUSTER,
    ANNOTATION_CLUSTER_KEY,
    DEFAULT_CLUSTER_IDLE_POLL_SEC,
    DEFAULT_CLUSTER_IDLE_WAIT_SEC,
    LABEL_CLUSTER,
)
from suite.errors import AppError
from suite.pipelinerun_naming import is_rhoai_e2e_pipelinerun_name
from install.kubeconfig_cluster_label import cluster_label_from_kubeconfig, cluster_lock_key_from_kubeconfig
from .oc_util import filter_warning_lines, run_cmd

RUN_OWNER_LABEL = "rhoai-e2e.run-owner"
RUN_OWNER_ANNOTATION = "rhoai-e2e.run-owner"
MANAGED_BY_LABEL = "rhoai-e2e.external-kubeconfig"
MANAGED_BY_VALUE = "rhoai_e2e.py"
EXTERNAL_CLUSTER_VERIFY_TIMEOUT_S = 30.0
TEKTON_EXTERNAL_CLUSTER_VERIFY_TIMEOUT_S = 60.0
RHOAI_IDMS_SOURCE = "registry.redhat.io/rhoai"
RHOAI_IDMS_MIRROR = "quay.io/rhoai"


def idms_has_rhoai_mirror(spec: dict) -> bool:
    for entry in spec.get("imageDigestMirrors") or []:
        if entry.get("source") != RHOAI_IDMS_SOURCE:
            continue
        if RHOAI_IDMS_MIRROR in (entry.get("mirrors") or []):
            return True
    return False


def external_cluster_has_rhoai_idms(
    kubeconfig_path: Path | str,
    *,
    timeout: float = EXTERNAL_CLUSTER_VERIFY_TIMEOUT_S,
) -> bool:
    path = Path(kubeconfig_path).expanduser().resolve()
    proc = run_cmd(
        ["oc", "--kubeconfig", str(path), "get", "imagedigestmirrorset", "-o", "json"],
        capture=True,
        check=False,
        timeout=timeout,
    )
    if proc.returncode != 0:
        return False
    try:
        items = json.loads(proc.stdout or "{}").get("items") or []
    except json.JSONDecodeError:
        return False
    return any(idms_has_rhoai_mirror(item.get("spec") or {}) for item in items)


def external_cluster_is_hypershift_managed(
    kubeconfig_path: Path | str,
    *,
    timeout: float = EXTERNAL_CLUSTER_VERIFY_TIMEOUT_S,
) -> bool:
    path = Path(kubeconfig_path).expanduser().resolve()
    proc = run_cmd(
        [
            "oc",
            "--kubeconfig",
            str(path),
            "get",
            "imagedigestmirrorset",
            "cluster",
            "-o",
            "jsonpath={.metadata.labels.hypershift\\.openshift\\.io/managed}",
        ],
        capture=True,
        check=False,
        timeout=timeout,
    )
    return proc.returncode == 0 and (proc.stdout or "").strip() == "true"


def external_cluster_rosa_hcp_pull_ready(
    kubeconfig_path: Path | str,
    *,
    timeout: float = EXTERNAL_CLUSTER_VERIFY_TIMEOUT_S,
) -> bool:
    path = Path(kubeconfig_path).expanduser().resolve()
    base = ["oc", "--kubeconfig", str(path)]
    if run_cmd([*base, "get", "ns", "kyverno"], capture=True, check=False, timeout=timeout).returncode != 0:
        return False
    for name in ("sync-secrets", "add-imagepullsecrets", "replace-rhoai-registry"):
        proc = run_cmd(
            [*base, "get", "clusterpolicy", name, "-o", "jsonpath={.status.ready}"],
            capture=True,
            check=False,
            timeout=timeout,
        )
        if proc.returncode != 0 or (proc.stdout or "").strip().lower() != "true":
            return False
    return (
        run_cmd(
            [*base, "get", "secret", "pull-secret-quay", "-n", "openshift-config"],
            capture=True,
            check=False,
            timeout=timeout,
        ).returncode
        == 0
    )


def verify_external_cluster_rhoai_idms_mirror(
    kubeconfig_path: Path | str,
    *,
    timeout: float = EXTERNAL_CLUSTER_VERIFY_TIMEOUT_S,
) -> None:
    """Check IDMS/Kyverno mirror state (diagnostic; CLI trigger skips this — see prepare-cluster-registry)."""
    path = Path(kubeconfig_path).expanduser().resolve()
    if external_cluster_has_rhoai_idms(path, timeout=timeout):
        print(f"✓ External cluster IDMS mirror configured ({RHOAI_IDMS_SOURCE} → {RHOAI_IDMS_MIRROR})")
        return
    if external_cluster_is_hypershift_managed(path, timeout=timeout):
        if external_cluster_rosa_hcp_pull_ready(path, timeout=timeout):
            print("✓ External HyperShift cluster has ROSA HCP Kyverno pull setup")
            return
        print(
            "INFO HyperShift external cluster: ROSA HCP Kyverno pull setup runs in "
            "external-cluster-ready / patch-cluster-pull-secret before install/tests."
        )
        return
    raise AppError(
        "External cluster is missing the rhoai IDMS mirror required for OLM bundle-unpack "
        f"({RHOAI_IDMS_SOURCE} → {RHOAI_IDMS_MIRROR}). "
        "On HyperShift guest clusters, rhoai-e2e applies Jenkins-style Kyverno policies. "
        "For rhoai_e2e.py triggers, prepare-cluster-registry configures mirror in-pipeline.",
        2,
    )


def default_secret_name(run_owner: str, kubeconfig_path: Path | str | None = None) -> str:
    """Derive a DNS-safe Secret name from cluster label, else ``oc whoami``."""
    if kubeconfig_path:
        cluster = cluster_label_from_kubeconfig(kubeconfig_path)
        if cluster:
            prefix = "rhoai-e2e-kubeconfig-"
            owner = re.sub(r"[^a-z0-9-]", "-", (run_owner or "user").lower())
            owner = re.sub(r"-+", "-", owner).strip("-")[:16] or "user"
            max_suffix = 63 - len(prefix) - len(owner) - 1
            safe = re.sub(r"[^a-z0-9-]", "-", cluster.lower())
            safe = re.sub(r"-+", "-", safe).strip("-")[:max_suffix].strip("-") or "cluster"
            return f"{prefix}{safe}-{owner}"
    safe = re.sub(r"[^a-z0-9-]", "-", (run_owner or "user").lower())
    safe = re.sub(r"-+", "-", safe).strip("-")[:40] or "user"
    return f"rhoai-e2e-kubeconfig-{safe}"


def validate_kubeconfig_path(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_file():
        raise AppError(f"--external-kubeconfig must be an existing file: {p}", 2)
    return p.resolve()


def kubeconfig_cluster_server(
    kubeconfig_path: Path | str,
    *,
    timeout: float = EXTERNAL_CLUSTER_VERIFY_TIMEOUT_S,
) -> str:
    """Return API server URL from *kubeconfig_path*, or empty when unknown."""
    path = Path(kubeconfig_path).expanduser().resolve()
    proc = run_cmd(
        [
            "oc",
            "--kubeconfig",
            str(path),
            "config",
            "view",
            "--minify",
            "-o",
            "jsonpath={.clusters[0].cluster.server}",
        ],
        capture=True,
        check=False,
        timeout=timeout,
    )
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def verify_external_cluster_login(
    kubeconfig_path: Path | str,
    *,
    timeout: float = EXTERNAL_CLUSTER_VERIFY_TIMEOUT_S,
) -> str:
    """Return ``oc whoami`` for *kubeconfig_path*; raise AppError when not logged in."""
    path = Path(kubeconfig_path).expanduser().resolve()
    if not path.is_file():
        raise AppError(f"External kubeconfig not found: {path}", 2)
    proc = run_cmd(
        ["oc", "--kubeconfig", str(path), "whoami", "--request-timeout=30s"],
        capture=True,
        check=False,
        timeout=timeout,
    )
    who = (proc.stdout or "").strip()
    if proc.returncode != 0 or not who or who == "system:anonymous":
        err = filter_warning_lines(f"{proc.stdout or ''}\n{proc.stderr or ''}").strip()[:500]
        server = kubeconfig_cluster_server(path, timeout=timeout)
        target = f"external cluster {server}" if server else f"external kubeconfig {path}"
        login = (
            f"oc --kubeconfig {path} login --server={server} --web"
            if server
            else f"oc --kubeconfig {path} login --web"
        )
        raise AppError(
            f"External cluster login required for {target} "
            f"(oc --kubeconfig {path} whoami failed): {err or who or proc.returncode}\n"
            "This is separate from the Konflux KUBECONFIG used to trigger rhoai_e2e.py. "
            f"Re-authenticate to the external cluster, then retry:\n  {login}",
            1,
        )
    return who


def verify_external_cluster_secret(
    *,
    namespace: str,
    secret_name: str,
    timeout: float = EXTERNAL_CLUSTER_VERIFY_TIMEOUT_S,
) -> str:
    """Load tenant Secret kubeconfig and verify API login."""
    import base64
    import tempfile

    name = (secret_name or "").strip()
    ns = (namespace or "").strip()
    if not name or not ns:
        raise AppError("namespace and secret name required to verify external kubeconfig Secret", 2)
    proc = run_cmd(
        [
            "oc",
            "get",
            "secret",
            name,
            "-n",
            ns,
            "-o",
            "jsonpath={.data.kubeconfig}",
        ],
        capture=True,
        check=False,
        timeout=timeout,
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        detail = filter_warning_lines(f"{proc.stdout or ''}\n{proc.stderr or ''}").strip()[:500]
        raise AppError(
            f"Could not read external kubeconfig Secret {name!r} in {ns}: {detail or proc.returncode}",
            2,
        )
    try:
        raw = base64.b64decode(proc.stdout.strip())
    except ValueError as exc:
        raise AppError(f"Secret {name!r} kubeconfig data is not valid base64", 2) from exc
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".kubeconfig") as tf:
            tf.write(raw)
            tmp_path = tf.name
        return verify_external_cluster_login(tmp_path, timeout=timeout)
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


def _user_uses_exec_auth(user: object) -> bool:
    return isinstance(user, dict) and isinstance(user.get("exec"), dict)


def _token_from_exec_user(kubeconfig_path: Path, user: dict) -> str:
    exec_cfg = user.get("exec")
    if not isinstance(exec_cfg, dict):
        return ""
    command = str(exec_cfg.get("command") or "oc").strip() or "oc"
    args = [str(a) for a in (exec_cfg.get("args") or [])]
    env = os.environ.copy()
    env["KUBECONFIG"] = str(kubeconfig_path)
    for item in exec_cfg.get("env") or []:
        if isinstance(item, dict) and item.get("name"):
            env[str(item["name"])] = str(item.get("value", ""))
    try:
        proc = subprocess.run(
            [command, *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=EXTERNAL_CLUSTER_VERIFY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError(
            f"Exec credential plugin ({command}) timed out after "
            f"{EXTERNAL_CLUSTER_VERIFY_TIMEOUT_S}s",
            2,
        ) from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:500]
        raise AppError(
            f"Could not resolve bearer token via exec auth ({command}): {err or proc.returncode}",
            2,
        )
    try:
        cred = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AppError("Exec credential plugin returned non-JSON output", 2) from exc
    status = cred.get("status") if isinstance(cred, dict) else None
    if not isinstance(status, dict):
        raise AppError("Exec credential plugin JSON missing status", 2)
    token = str(status.get("token") or "").strip()
    if not token:
        raise AppError(
            "Exec credential plugin returned empty token; refresh external cluster login and retry.",
            2,
        )
    return token


def _load_kubeconfig_doc(kubeconfig_path: Path) -> dict:
    import yaml

    try:
        doc = yaml.safe_load(kubeconfig_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AppError(f"Cannot read kubeconfig {kubeconfig_path}: {exc}", 2) from exc
    except yaml.YAMLError as exc:
        raise AppError(f"Invalid kubeconfig YAML {kubeconfig_path}: {exc}", 2) from exc
    if not isinstance(doc, dict):
        raise AppError(f"Invalid kubeconfig (expected mapping): {kubeconfig_path}", 2)
    return doc


def _kubeconfig_context_user_names(doc: dict) -> list[tuple[str, str]]:
    contexts = doc.get("contexts") if isinstance(doc.get("contexts"), list) else []
    out: list[tuple[str, str]] = []
    for entry in contexts:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
        user_name = str(context.get("user") or "").strip()
        if name:
            out.append((name, user_name))
    return out


def _kubeconfig_user_has_client_cert(doc: dict, user_name: str) -> bool:
    users = doc.get("users") if isinstance(doc.get("users"), list) else []
    user_entry = next((u for u in users if isinstance(u, dict) and u.get("name") == user_name), None)
    if not isinstance(user_entry, dict):
        return False
    user = user_entry.get("user") if isinstance(user_entry.get("user"), dict) else {}
    return bool(
        user.get("client-certificate")
        or user.get("client-certificate-data")
        or user.get("client-key")
        or user.get("client-key-data")
    )


def _admin_context_heuristic_score(context_name: str, user_name: str) -> int:
    blob = f"{context_name} {user_name}".lower()
    score = 0
    if "cluster-admin" in blob:
        score += 100
    if "htpasswd-cluster-admin" in blob:
        score += 80
    if user_name.lower().startswith("htpasswd-"):
        score += 40
    return score


def _write_kubeconfig_current_context(doc: dict, context_name: str) -> Path:
    import yaml

    contexts = _kubeconfig_context_user_names(doc)
    if not any(name == context_name for name, _ in contexts):
        raise AppError(f"Kubeconfig has no context {context_name!r}", 2)
    switched = dict(doc)
    switched["current-context"] = context_name
    tmp = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".kubeconfig", delete=False)
    try:
        yaml.safe_dump(switched, tmp, default_flow_style=False)
        tmp.flush()
        tmp.close()
        return Path(tmp.name)
    except OSError:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def _context_has_cluster_admin(kubeconfig_path: Path, context_name: str) -> bool:
    from steps.tekton_util import _oc_has_cluster_admin, _resolve_oc_binary

    doc = _load_kubeconfig_doc(kubeconfig_path)
    current = str(doc.get("current-context") or "").strip()
    temp_path: Path | None = None
    try:
        if context_name != current:
            temp_path = _write_kubeconfig_current_context(doc, context_name)
            path = temp_path
        else:
            path = kubeconfig_path
        oc = _resolve_oc_binary(os.environ)
        if not oc:
            return False
        env = {**os.environ, "KUBECONFIG": str(path)}
        return _oc_has_cluster_admin(oc, env)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def resolve_external_kubeconfig_context(
    kubeconfig_path: Path,
    *,
    preferred_context: str = "",
    require_cluster_admin: bool = True,
) -> tuple[Path, bool]:
    """Return kubeconfig path with a cluster-admin context selected for Tekton upload.

    When *require_cluster_admin* is true (default for ``--external-kubeconfig`` triggers),
    scan contexts in the file and prefer a cluster-admin identity over the current context.
    Returns ``(path, ephemeral)``; unlink *ephemeral* temp copies when done.
    """
    doc = _load_kubeconfig_doc(kubeconfig_path)
    current = str(doc.get("current-context") or "").strip()
    contexts = _kubeconfig_context_user_names(doc)
    if not contexts:
        raise AppError(f"Kubeconfig has no contexts: {kubeconfig_path}", 2)

    preferred = (preferred_context or "").strip()
    if preferred and not any(name == preferred for name, _ in contexts):
        raise AppError(
            f"--external-kubeconfig-context {preferred!r} not found in {kubeconfig_path}",
            2,
        )

    if not require_cluster_admin and not preferred:
        return kubeconfig_path, False

    candidates: list[tuple[int, str]] = []
    if preferred:
        candidates = [(0, preferred)]
    else:
        for name, user_name in contexts:
            score = _admin_context_heuristic_score(name, user_name)
            if _kubeconfig_user_has_client_cert(doc, user_name):
                score += 50
            if name == current:
                score += 10
            candidates.append((score, name))
        candidates.sort(key=lambda item: (-item[0], item[1]))

    admin_context = ""
    admin_failures: list[str] = []
    for _, name in candidates:
        if _context_has_cluster_admin(kubeconfig_path, name):
            admin_context = name
            break
        admin_failures.append(name)

    if not admin_context:
        lines = [
            "External kubeconfig has no authenticated cluster-admin context for Tekton upload.",
            f"Current context: {current or '(unset)'}",
        ]
        if preferred:
            lines.append(f"Requested --external-kubeconfig-context: {preferred!r}")
        else:
            lines.append(f"Contexts checked: {', '.join(admin_failures) or '(none)'}")
        lines.append(
            "Re-authenticate a cluster-admin context (e.g. htpasswd-cluster-admin-user), "
            "switch with oc config use-context, or pass --external-kubeconfig-context."
        )
        raise AppError("\n".join(lines), 2)

    if admin_context == current:
        return kubeconfig_path, False

    print(
        f"INFO External kubeconfig: using context {admin_context!r} (cluster-admin) "
        f"instead of current {current!r}",
        flush=True,
    )
    return _write_kubeconfig_current_context(doc, admin_context), True


def materialize_kubeconfig_for_tekton(
    kubeconfig_path: Path,
    *,
    preferred_context: str = "",
    require_cluster_admin: bool = True,
) -> tuple[Path, bool]:
    """Return a kubeconfig Tekton pods can use (token auth, no local ``oc`` exec).

    When the source uses ``user.exec`` (typical after ``oc login --web``), resolve a
    bearer token with the local ``oc`` CLI and write a minimal kubeconfig copy.
    Caller must delete the returned path when it differs from *kubeconfig_path*.
    """
    import yaml

    context_path, context_ephemeral = resolve_external_kubeconfig_context(
        kubeconfig_path,
        preferred_context=preferred_context,
        require_cluster_admin=require_cluster_admin,
    )
    try:
        try:
            doc = yaml.safe_load(context_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise AppError(f"Cannot read kubeconfig {context_path}: {exc}", 2) from exc
        except yaml.YAMLError as exc:
            raise AppError(f"Invalid kubeconfig YAML {context_path}: {exc}", 2) from exc
        if not isinstance(doc, dict):
            raise AppError(f"Invalid kubeconfig (expected mapping): {context_path}", 2)

        contexts = doc.get("contexts") if isinstance(doc.get("contexts"), list) else []
        users = doc.get("users") if isinstance(doc.get("users"), list) else []
        current = str(doc.get("current-context") or "").strip()
        ctx = next((c for c in contexts if isinstance(c, dict) and c.get("name") == current), None)
        if not isinstance(ctx, dict):
            return context_path, context_ephemeral

        context = ctx.get("context") if isinstance(ctx.get("context"), dict) else {}
        user_name = str(context.get("user") or "").strip()
        cluster_name = str(context.get("cluster") or "").strip()
        user_entry = next((u for u in users if isinstance(u, dict) and u.get("name") == user_name), None)
        if not isinstance(user_entry, dict):
            return context_path, context_ephemeral

        user = user_entry.get("user") if isinstance(user_entry.get("user"), dict) else {}
        if not user.get("token") and not _user_uses_exec_auth(user):
            return context_path, context_ephemeral

        from steps.tekton_util import _resolve_bearer_token_from_kubeconfig

        env = {**os.environ, "KUBECONFIG": str(context_path)}
        token = _resolve_bearer_token_from_kubeconfig(context_path, env)
        if not token and _user_uses_exec_auth(user):
            token = _token_from_exec_user(context_path, user)
        embedded = str(user.get("token") or "").strip()
        if not token:
            if embedded:
                return context_path, context_ephemeral
            raise AppError(
                "Could not resolve bearer token for Tekton upload; refresh external cluster login and retry.",
                2,
            )
        if embedded and token == embedded and not _user_uses_exec_auth(user):
            return context_path, context_ephemeral

        clusters = doc.get("clusters") if isinstance(doc.get("clusters"), list) else []
        cluster_entry = next(
            (c for c in clusters if isinstance(c, dict) and c.get("name") == cluster_name),
            None,
        )
        if not isinstance(cluster_entry, dict):
            raise AppError(f"Kubeconfig missing cluster {cluster_name!r} for context {current!r}", 2)

        materialized = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [cluster_entry],
            "contexts": [ctx],
            "current-context": current,
            "users": [{"name": user_name, "user": {"token": token}}],
        }
        tmp = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".kubeconfig", delete=False)
        try:
            yaml.safe_dump(materialized, tmp, default_flow_style=False)
            tmp.flush()
            tmp.close()
            if context_ephemeral:
                context_path.unlink(missing_ok=True)
            return Path(tmp.name), True
        except OSError:
            Path(tmp.name).unlink(missing_ok=True)
            raise
    except Exception:
        if context_ephemeral:
            context_path.unlink(missing_ok=True)
        raise


def ensure_external_kubeconfig_secret(
    *,
    namespace: str,
    kubeconfig_path: Path,
    secret_name: str,
    run_owner: str,
    preferred_context: str = "",
) -> str:
    """Create or update a generic Secret with key ``kubeconfig``; return secret name."""
    name = (secret_name or "").strip() or default_secret_name(run_owner, kubeconfig_path)
    upload_path, ephemeral = materialize_kubeconfig_for_tekton(
        kubeconfig_path,
        preferred_context=preferred_context,
    )
    try:
        if ephemeral:
            print(
                "Materialized external kubeconfig with bearer token for Tekton "
                "(prefers 24h rhoai-e2e-cluster-admin SA when cluster-admin; "
                "source used oc exec auth; opendatahub-tests image has no oc binary).",
                flush=True,
            )
        who = verify_external_cluster_login(upload_path)
        print(f"External cluster preflight OK as {who}")
        proc = run_cmd(
            [
                "oc",
                "create",
                "secret",
                "generic",
                name,
                f"--from-file=kubeconfig={upload_path}",
                "-n",
                namespace,
                "--dry-run=client",
                "-o",
                "yaml",
            ],
            capture=True,
            check=True,
        )
    finally:
        if ephemeral:
            upload_path.unlink(missing_ok=True)
    apply = run_cmd(
        ["oc", "apply", "-n", namespace, "-f", "-"],
        capture=True,
        check=False,
        input_text=proc.stdout,
    )
    filtered = filter_warning_lines(f"{apply.stdout}\n{apply.stderr}")
    if filtered.strip():
        print(filtered)
    if apply.returncode != 0:
        raise AppError(f"Failed to apply external kubeconfig Secret {name!r} in {namespace}")

    cluster_label = cluster_label_from_kubeconfig(kubeconfig_path)
    cluster_key = cluster_lock_key_from_kubeconfig(kubeconfig_path)
    annotate_argv = [
        "oc",
        "annotate",
        "secret",
        name,
        "-n",
        namespace,
        f"{RUN_OWNER_ANNOTATION}={run_owner}",
    ]
    if cluster_label:
        annotate_argv.append(f"{ANNOTATION_CLUSTER}={cluster_label}")
    if cluster_key:
        annotate_argv.append(f"{ANNOTATION_CLUSTER_KEY}={cluster_key}")
    annotate_argv.append("--overwrite")
    ann = run_cmd(annotate_argv, capture=True, check=False)
    lbl_argv = [
        "oc",
        "label",
        "secret",
        name,
        "-n",
        namespace,
        f"{RUN_OWNER_LABEL}={run_owner}",
        f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}",
    ]
    if cluster_label:
        from runners.report.pipelinerun_metadata import sanitize_k8s_label_value

        cl = sanitize_k8s_label_value(cluster_label)
        if cl:
            lbl_argv.append(f"{LABEL_CLUSTER}={cl}")
    lbl_argv.append("--overwrite")
    lbl = run_cmd(lbl_argv, capture=True, check=False)
    for proc in (ann, lbl):
        msg = filter_warning_lines(f"{proc.stdout}\n{proc.stderr}").strip()
        if proc.returncode != 0 and msg:
            print(f"  WARN secret metadata: {msg}")
    print(f"External kubeconfig Secret: {name} (namespace {namespace})")
    return name


def sync_external_kubeconfig_secret_cluster_metadata(
    *,
    namespace: str,
    secret_name: str,
    kubeconfig_path: Path | str,
) -> None:
    """Refresh rhoai-e2e.cluster / cluster-key annotations after kubeconfig data changes."""
    ns = (namespace or "").strip()
    name = (secret_name or "").strip()
    if not ns or not name:
        return
    cluster_label = cluster_label_from_kubeconfig(kubeconfig_path)
    cluster_key = cluster_lock_key_from_kubeconfig(kubeconfig_path)
    annotate_argv = ["oc", "annotate", "secret", name, "-n", ns, "--overwrite"]
    if cluster_label:
        annotate_argv.append(f"{ANNOTATION_CLUSTER}={cluster_label}")
    if cluster_key:
        annotate_argv.append(f"{ANNOTATION_CLUSTER_KEY}={cluster_key}")
    if len(annotate_argv) <= 5:
        return
    proc = run_cmd(annotate_argv, capture=True, check=False)
    msg = filter_warning_lines(f"{proc.stdout}\n{proc.stderr}").strip()
    if proc.returncode != 0 and msg:
        print(f"  WARN secret cluster metadata: {msg}")


def _tenant_secret_annotation(*, namespace: str, secret_name: str, annotation_key: str) -> str:
    name = (secret_name or "").strip()
    ns = (namespace or "").strip()
    if not name or not ns:
        return ""
    proc = run_cmd(
        [
            "oc",
            "get",
            "secret",
            name,
            "-n",
            ns,
            "-o",
            f"jsonpath={{.metadata.annotations['{annotation_key}']}}",
        ],
        capture=True,
        check=False,
        timeout=30,
    )
    if proc.returncode == 0:
        return (proc.stdout or "").strip()
    return ""


@contextmanager
def _tenant_secret_kubeconfig_file(*, namespace: str, secret_name: str):
    """Yield a temp kubeconfig path decoded from a tenant Secret, or None."""
    import base64

    name = (secret_name or "").strip()
    ns = (namespace or "").strip()
    if not name or not ns:
        yield None
        return
    for key in ("kubeconfig", "config"):
        proc = run_cmd(
            [
                "oc",
                "get",
                "secret",
                name,
                "-n",
                ns,
                "-o",
                f"jsonpath={{.data.{key}}}",
            ],
            capture=True,
            check=False,
            timeout=30,
        )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            continue
        try:
            raw = base64.b64decode(proc.stdout.strip())
        except ValueError:
            continue
        path = ""
        try:
            with tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".kubeconfig") as tf:
                tf.write(raw)
                path = tf.name
            yield Path(path)
            return
        finally:
            if path:
                Path(path).unlink(missing_ok=True)
    yield None


def _value_from_tenant_secret(
    *,
    namespace: str,
    secret_name: str,
    annotation_key: str,
    from_kubeconfig: Callable[[Path], str],
    normalize: bool = False,
) -> str:
    def _finish(value: str) -> str:
        text = (value or "").strip()
        return _normalize_cluster_id(text) if normalize and text else text

    value = _tenant_secret_annotation(
        namespace=namespace,
        secret_name=secret_name,
        annotation_key=annotation_key,
    )
    if value:
        return _finish(value)
    with _tenant_secret_kubeconfig_file(namespace=namespace, secret_name=secret_name) as path:
        if path is not None:
            derived = from_kubeconfig(path)
            if derived:
                return _finish(derived)
    return ""


def cluster_label_from_tenant_secret(*, namespace: str, secret_name: str) -> str:
    """Context/cluster name from Secret annotation or embedded kubeconfig (best-effort)."""
    return _value_from_tenant_secret(
        namespace=namespace,
        secret_name=secret_name,
        annotation_key=ANNOTATION_CLUSTER,
        from_kubeconfig=cluster_label_from_kubeconfig,
    )


def cluster_lock_key_from_tenant_secret(*, namespace: str, secret_name: str) -> str:
    """Normalized API hostname from Secret annotation or embedded kubeconfig."""
    return _value_from_tenant_secret(
        namespace=namespace,
        secret_name=secret_name,
        annotation_key=ANNOTATION_CLUSTER_KEY,
        from_kubeconfig=cluster_lock_key_from_kubeconfig,
        normalize=True,
    )


def _pipelinerun_cluster_source_param(item: dict) -> str:
    spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
    for param in spec.get("params") or []:
        if not isinstance(param, dict) or param.get("name") != "CLUSTER_SOURCE":
            continue
        return str(param.get("value") or "").strip()
    return ""


def _normalize_cluster_id(value: str) -> str:
    return (value or "").strip().lower()


def cluster_label_from_secret_name(secret_name: str) -> str:
    """Derive cluster id from a tenant kubeconfig Secret name (best-effort)."""
    secret = (secret_name or "").strip()
    if not secret:
        return ""
    for prefix in ("rhoai-e2e-kubeconfig-", "kubeconfig-"):
        if secret.startswith(prefix):
            return secret[len(prefix) :].strip("-") or secret
    return secret


def _resolve_external_cluster_field(
    *,
    namespace: str,
    cluster_source: str,
    explicit_value: str = "",
    tenant_resolver: Callable[..., str],
    secret_name_fallback: Callable[[str], str] | None = None,
) -> str:
    explicit = _normalize_cluster_id(explicit_value)
    if explicit:
        return explicit
    source = (cluster_source or "").strip()
    if not source:
        return ""
    from suite.its_trigger_params import is_external_cluster_source

    if not is_external_cluster_source(source):
        return ""
    value = tenant_resolver(namespace=namespace, secret_name=source)
    if value:
        return _normalize_cluster_id(value)
    if secret_name_fallback:
        return _normalize_cluster_id(secret_name_fallback(source))
    return ""


def resolve_cluster_id_for_external_cluster(
    *,
    namespace: str,
    cluster_source: str,
    cluster_id: str = "",
) -> str:
    """Physical cluster id for single-flight locking (label on PR / Secret annotation)."""
    return _resolve_external_cluster_field(
        namespace=namespace,
        cluster_source=cluster_source,
        explicit_value=cluster_id,
        tenant_resolver=cluster_label_from_tenant_secret,
        secret_name_fallback=cluster_label_from_secret_name,
    )


def resolve_cluster_lock_key_for_external_cluster(
    *,
    namespace: str,
    cluster_source: str,
    cluster_lock_key: str = "",
) -> str:
    """Canonical API hostname for single-flight locking."""
    return _resolve_external_cluster_field(
        namespace=namespace,
        cluster_source=cluster_source,
        explicit_value=cluster_lock_key,
        tenant_resolver=cluster_lock_key_from_tenant_secret,
    )


_PIPELINE_RUN_WIND_DOWN_REASONS = frozenset(
    {
        "StoppedRunningFinally",
        "PipelineRunStopping",
        "PipelineRunCancelled",
        "Cancelled",
        "StoppedRunCancelled",
        "TaskRunCancelled",
    }
)


def _pipelinerun_succeeded_reason(item: dict) -> str:
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    for cond in status.get("conditions") or []:
        if not isinstance(cond, dict) or cond.get("type") != "Succeeded":
            continue
        return str(cond.get("reason") or "").strip()
    return ""


def _pipelinerun_is_active(item: dict) -> bool:
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    if status.get("completionTime"):
        return False
    reason = _pipelinerun_succeeded_reason(item)
    if reason in _PIPELINE_RUN_WIND_DOWN_REASONS:
        return False
    if "cancel" in reason.lower() or "stopping" in reason.lower():
        return False
    return True


def _pipelinerun_normalized_field(item: dict, *, bucket: str, key: str) -> str:
    meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    fields = meta.get(bucket) if isinstance(meta.get(bucket), dict) else {}
    return _normalize_cluster_id(str(fields.get(key) or ""))


def _pipelinerun_cluster_label(item: dict) -> str:
    return _pipelinerun_normalized_field(item, bucket="labels", key=LABEL_CLUSTER)


def _pipelinerun_cluster_key(item: dict) -> str:
    return _pipelinerun_normalized_field(item, bucket="annotations", key=ANNOTATION_CLUSTER_KEY)


def _cached_secret_cluster_field(
    pr_source: str,
    cache: dict[str, str],
    *,
    namespace: str,
    resolver: Callable[..., str],
) -> str:
    if pr_source not in cache:
        cache[pr_source] = resolver(namespace=namespace, cluster_source=pr_source)
    return cache[pr_source]


def _pipelinerun_matches_external_cluster(
    item: dict,
    *,
    namespace: str,
    cluster_id: str,
    cluster_key: str,
    cluster_source: str,
    secret_cluster_cache: dict[str, str],
    secret_lock_key_cache: dict[str, str],
) -> bool:
    pr_source = _pipelinerun_cluster_source_param(item)
    target_source = (cluster_source or "").strip()
    target_id = _normalize_cluster_id(cluster_id)
    target_key = _normalize_cluster_id(cluster_key)
    if target_source and pr_source == target_source:
        return True
    if target_key:
        pr_key = _pipelinerun_cluster_key(item)
        if pr_key and pr_key == target_key:
            return True
        if pr_source and _cached_secret_cluster_field(
            pr_source,
            secret_lock_key_cache,
            namespace=namespace,
            resolver=resolve_cluster_lock_key_for_external_cluster,
        ) == target_key:
            return True
    if not target_id:
        return False
    pr_label = _pipelinerun_cluster_label(item)
    if pr_label and pr_label == target_id:
        return True
    if pr_source:
        if _cached_secret_cluster_field(
            pr_source,
            secret_cluster_cache,
            namespace=namespace,
            resolver=resolve_cluster_id_for_external_cluster,
        ) == target_id:
            return True
        derived = _normalize_cluster_id(cluster_label_from_secret_name(pr_source))
        if derived == target_id or derived.startswith(f"{target_id}-") or target_id.startswith(f"{derived}-"):
            return True
    return False


def _list_rhoai_e2e_pipelinerun_items(*, namespace: str) -> list[dict] | None:
    ns = (namespace or "").strip()
    if not ns:
        return []
    proc = run_cmd(
        ["oc", "get", "pipelinerun", "-n", ns, "-o", "json"],
        capture=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return []
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def list_active_pipelineruns_for_external_cluster(
    *,
    namespace: str,
    cluster_source: str,
    cluster_id: str = "",
    exclude_name: str = "",
) -> list[str] | None:
    """Incomplete rhoai-e2e PipelineRuns on the same physical external cluster.

    Matches ``rhoai-e2e.cluster-key`` (API hostname), ``rhoai-e2e.cluster`` label,
    ``CLUSTER_SOURCE`` param, or resolved Secret cluster id. Returns None when the Konflux
    API cannot be queried.
    """
    from suite.its_trigger_params import is_external_cluster_source

    source = (cluster_source or "").strip()
    if not is_external_cluster_source(source):
        return []
    target_id = resolve_cluster_id_for_external_cluster(
        namespace=namespace,
        cluster_source=source,
        cluster_id=cluster_id,
    )
    target_key = resolve_cluster_lock_key_for_external_cluster(
        namespace=namespace,
        cluster_source=source,
    )
    items = _list_rhoai_e2e_pipelinerun_items(namespace=namespace)
    if items is None:
        return None
    exclude = (exclude_name or "").strip()
    secret_cluster_cache: dict[str, str] = {}
    secret_lock_key_cache: dict[str, str] = {}
    active: list[str] = []
    for item in items:
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        name = str(meta.get("name") or "").strip()
        if not name or not is_rhoai_e2e_pipelinerun_name(name):
            continue
        if name == exclude:
            continue
        if not _pipelinerun_is_active(item):
            continue
        if not _pipelinerun_matches_external_cluster(
            item,
            namespace=namespace,
            cluster_id=target_id,
            cluster_key=target_key,
            cluster_source=source,
            secret_cluster_cache=secret_cluster_cache,
            secret_lock_key_cache=secret_lock_key_cache,
        ):
            continue
        active.append(name)
    return sorted(active)


def wait_for_external_cluster_idle(
    *,
    namespace: str,
    cluster_source: str,
    cluster_id: str = "",
    exclude_pipelinerun: str = "",
    force: bool = False,
    timeout_sec: int = DEFAULT_CLUSTER_IDLE_WAIT_SEC,
    poll_interval_sec: int = DEFAULT_CLUSTER_IDLE_POLL_SEC,
) -> None:
    """Wait until no other rhoai-e2e run holds the same external cluster (Jenkins resource-lock style).

    No-op for EPHC or empty CLUSTER_SOURCE. When *force* is true, skip the check. When *timeout_sec*
    is 0, fail immediately if another run is active (legacy assert behavior).
    """
    from suite.its_trigger_params import is_external_cluster_source

    source = (cluster_source or "").strip()
    if not is_external_cluster_source(source) or force:
        return
    ns = (namespace or "").strip()
    if not ns:
        raise AppError("Konflux namespace is required to verify external cluster availability.", 1)
    target_id = resolve_cluster_id_for_external_cluster(
        namespace=ns,
        cluster_source=source,
        cluster_id=cluster_id,
    )
    poll = max(5, int(poll_interval_sec or DEFAULT_CLUSTER_IDLE_POLL_SEC))
    deadline = time.monotonic() + max(0, int(timeout_sec or 0))
    first_log = True
    while True:
        active = list_active_pipelineruns_for_external_cluster(
            namespace=ns,
            cluster_source=source,
            cluster_id=target_id,
            exclude_name=(exclude_pipelinerun or "").strip(),
        )
        if active is None:
            raise AppError(
                f"Cannot verify that external cluster {source!r} is idle (Konflux API query failed). "
                "Refusing to start another rhoai-e2e run on a shared external kubeconfig.",
                1,
            )
        if not active:
            if not first_log:
                print(
                    f"External cluster idle: cluster={target_id or source!r} "
                    f"(CLUSTER_SOURCE={source!r})",
                    flush=True,
                )
            return
        preview = ", ".join(active[:3])
        if len(active) > 3:
            preview = f"{preview} (+{len(active) - 3} more)"
        if timeout_sec <= 0:
            raise AppError(_external_cluster_busy_message(source, target_id, preview, active[0]), 1)
        if time.monotonic() >= deadline:
            raise AppError(
                f"Timed out after {timeout_sec}s waiting for external cluster "
                f"{target_id or source!r} to become idle. Still active: {preview}.",
                1,
            )
        if first_log:
            print(
                f"Waiting for external cluster {target_id or source!r} "
                f"(poll every {poll}s; active: {preview})",
                flush=True,
            )
            first_log = False
        else:
            print(f"  still waiting — active PipelineRun(s): {preview}", flush=True)
        time.sleep(poll)


def _external_cluster_busy_message(
    cluster_source: str,
    cluster_id: str,
    preview: str,
    watch_pr: str,
) -> str:
    cluster_ref = cluster_id or cluster_source
    return (
        f"External cluster {cluster_ref!r} is busy: active PipelineRun(s): {preview}. "
        "Only one rhoai-e2e run at a time is allowed per external cluster. "
        "Wait for the run to finish, pass --force-cluster-run to override, or watch with: "
        f"python3 integration-tests/rhoai-e2e/rhoai_e2e.py -w {watch_pr}"
    )


def assert_external_cluster_idle(
    *,
    namespace: str,
    cluster_source: str,
    cluster_id: str = "",
    exclude_pipelinerun: str = "",
    force: bool = False,
) -> None:
    """Fail immediately when another rhoai-e2e PipelineRun still uses the same external cluster."""
    wait_for_external_cluster_idle(
        namespace=namespace,
        cluster_source=cluster_source,
        cluster_id=cluster_id,
        exclude_pipelinerun=exclude_pipelinerun,
        force=force,
        timeout_sec=0,
    )


def assert_external_cluster_lock_queryable(
    *,
    namespace: str,
    cluster_source: str,
    cluster_id: str = "",
    force: bool = False,
) -> None:
    """Fail fast when Konflux cannot query external-cluster locks (CLI trigger path).

    Idle waiting is deferred to the pipeline ``external-cluster-ready`` task.
    """
    from suite.its_trigger_params import is_external_cluster_source

    source = (cluster_source or "").strip()
    if not is_external_cluster_source(source) or force:
        return
    ns = (namespace or "").strip()
    if not ns:
        raise AppError("Konflux namespace is required to verify external cluster availability.", 1)
    target_id = resolve_cluster_id_for_external_cluster(
        namespace=ns,
        cluster_source=source,
        cluster_id=cluster_id,
    )
    active = list_active_pipelineruns_for_external_cluster(
        namespace=ns,
        cluster_source=source,
        cluster_id=target_id,
    )
    if active is None:
        raise AppError(
            f"Cannot verify external cluster lock for {target_id or source!r} "
            f"(Konflux API query failed). Refusing to trigger until lock state is known.",
            1,
        )


def list_active_pipelineruns_for_cluster_source(
    *,
    namespace: str,
    cluster_source: str,
    exclude_name: str = "",
) -> list[str] | None:
    """PipelineRuns still active with the same ``CLUSTER_SOURCE`` param (narrow helper)."""
    return list_active_pipelineruns_for_external_cluster(
        namespace=namespace,
        cluster_source=cluster_source,
        exclude_name=exclude_name,
    )


def delete_external_kubeconfig_secret(
    *,
    namespace: str,
    secret_name: str,
    exclude_pipelinerun: str = "",
) -> None:
    if not secret_name:
        return
    others = list_active_pipelineruns_for_cluster_source(
        namespace=namespace,
        cluster_source=secret_name,
        exclude_name=exclude_pipelinerun,
    )
    if others is None:
        print(
            f"  WARN Keeping Secret {secret_name} (could not verify other PipelineRuns on Konflux)"
        )
        return
    if others:
        preview = ", ".join(others[:3])
        suffix = "…" if len(others) > 3 else ""
        print(
            f"  Keeping Secret {secret_name} ({len(others)} other PipelineRun(s) still active: "
            f"{preview}{suffix})"
        )
        return
    proc = run_cmd(
        ["oc", "delete", "secret", secret_name, "-n", namespace, "--ignore-not-found"],
        capture=True,
        check=False,
    )
    if proc.returncode == 0 and "deleted" in (proc.stdout or "").lower():
        print(f"  Deleted Secret {secret_name}")
