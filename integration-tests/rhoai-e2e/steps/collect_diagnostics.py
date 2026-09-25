#!/usr/bin/env python3
"""Collect failure diagnostics from the target cluster.

Primary output: RHOAI triage report (status, events, pod logs since PipelineRun start,
issues summary) plus DSC/DSCi yaml. OLM snapshots when install ran. ``oc adm inspect`` on
install or pipeline failure. Triage is published as
``{product}-{version?}-{cluster?}-diagnostic-{datetime}.log`` (no separate tgz).

Env (required):
    OPERATOR_NAMESPACE
    DIAG_MANIFEST_RESULT -- Tekton result file path
Env (optional):
    DIAG_DIR -- output directory (default /diag)
    PRODUCT -- pipeline product (test-only skips OLM detail defaults)
    INSTALL_OPERATOR_*_STATUS -- Tekton install task status
    INSTALL_DEP_OPERATORS_STATUS -- Tekton install-dep-operators task status
    PIPELINE_RUN_STATUS -- overall pipeline status (Failed enables adm inspect)
    PIPELINE_RUN_START_TIME -- optional RFC3339 hint; resolved from in-cluster PipelineRun when unset
    PIPELINE_RUN_NAME -- Tekton PipelineRun name (for in-cluster creationTimestamp lookup)
    DIAG_COLLECT_POD_LOGS -- default true (RHOAI triage pod logs)
    DIAG_COLLECT_OLM_DETAIL -- default true when install ran (PRODUCT is rhoai/odh)
    DIAG_COLLECT_ADM_INSPECT -- default true when install or pipeline Failed
    DIAG_POD_LOG_MAX_BYTES -- max bytes per namespace for workload logs (default 524288)
    DIAG_ISSUES_SUMMARY_MAX_LINES -- issues summary line cap (default 500)
    TESTS_SHARED_DIR -- tests-shared workspace root for diagnostic log OCI upload
      (writes ``.collect-diagnostics-done`` marker for publish-results upload ordering)
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from install.kubeconfig_cluster_label import cluster_label_from_kubeconfig
from runners.report.pipelinerun_metadata import infer_installed_product
from steps.rhoai_triage import resolve_logs_since_time, run_rhoai_triage
from steps.tekton_util import clamp_tekton_result, require_env, run, write_result
from steps.tests_payload import mark_collect_diagnostics_done, tests_payload_results_dir
from suite.conforma_gate import CONFORMA_GATE_SKIP
from suite.pipelinerun_naming import build_diagnostic_artifact_log_name

# Single OCI artifact (publish-results uploads tests-payload/results/*.log).
_TRIAGE_ARTIFACT_SECTIONS: tuple[tuple[str, str], ...] = (
    ("triage/issues-summary.txt", "ISSUES SUMMARY"),
    ("triage/status-report.txt", "STATUS REPORT"),
    ("triage/dependency-status-report.txt", "DEPENDENCY INSTALL STATUS"),
    ("triage/events.txt", "EVENTS"),
    ("triage/dependency-events.txt", "DEPENDENCY INSTALL EVENTS"),
    ("triage/operator-highlights.txt", "OPERATOR LOG HIGHLIGHTS"),
    ("triage/dependency-operator-highlights.txt", "DEPENDENCY OPERATOR LOG HIGHLIGHTS"),
)
_TRIAGE_POD_LOG_DIRS: tuple[tuple[str, str], ...] = (
    ("triage/operator-logs", "OPERATOR POD LOG"),
    ("triage/workload-logs", "WORKLOAD POD LOG"),
    ("triage/dependency-logs", "DEPENDENCY INSTALL POD LOG"),
)

_OC = shutil.which("oc") or "oc"
_DEFAULT_KUBECONFIG = Path("/credentials/kubeconfig")
# Keep manifest tiny: Tekton termination message includes all task results (4096 B total).
_MANIFEST_MAX = 512
_STEP_LOG_SECTION_MAX_LINES = 500
_STEP_LOG_POD_EXCERPT_LINES = 40
_STEP_LOG_POD_FILES_MAX = 20


def _truthy(raw: str | None, *, default: bool = False) -> bool:
    if raw is None:
        return default
    val = raw.strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def _install_ran() -> bool:
    product = os.environ.get("PRODUCT", "").strip().lower()
    from suite.constants import product_installs_operator

    return product_installs_operator(product)


def _install_task_failed() -> bool:
    for env_name in (
        "INSTALL_OPERATOR_RHOAI_STATUS",
        "INSTALL_OPERATOR_RHOAI_EXTERNAL_STATUS",
        "INSTALL_OPERATOR_ODH_STATUS",
        "INSTALL_OPERATOR_ODH_EXTERNAL_STATUS",
        "INSTALL_OPERATOR_STATUS",
        "INSTALL_OPERATOR_EXTERNAL_STATUS",
    ):
        if os.environ.get(env_name, "").strip() == "Failed":
            return True
    return False


def _pipeline_failed() -> bool:
    return os.environ.get("PIPELINE_RUN_STATUS", "").strip() == "Failed"


def _should_collect_adm_inspect() -> bool:
    return _install_task_failed() or _pipeline_failed()


def _oc(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return run([_OC, *args], check=False, capture=True, **kwargs)  # type: ignore[arg-type]


def _oc_to_file(args: list[str], dest: Path) -> None:
    r = _oc(args)
    if r.returncode != 0:
        invoc = " ".join(shlex.quote(x) for x in [_OC, *args])
        stderr = r.stderr or ""
        stdout = r.stdout or ""
        blob = (
            f"OC COMMAND FAILED: exitcode={r.returncode}\n"
            f"COMMAND: {invoc}\n"
            f"STDERR:\n{stderr}\n"
            f"STDOUT:\n{stdout}\n"
        )
        dest.write_text(blob, encoding="utf-8")
    else:
        dest.write_text(r.stdout or "", encoding="utf-8")


def _collect_rhoai_cr_status(diag_dir: Path) -> None:
    cr_dir = diag_dir / "rhoai-cr-status"
    cr_dir.mkdir(parents=True, exist_ok=True)
    _oc_to_file(["get", "dsc", "-A", "-o", "yaml"], cr_dir / "dsc.yaml")
    _oc_to_file(["get", "dsci", "-A", "-o", "yaml"], cr_dir / "dsci.yaml")
    _oc_to_file(["describe", "dsc", "-A"], cr_dir / "dsc-describe.txt")
    _oc_to_file(["describe", "dsci", "-A"], cr_dir / "dsci-describe.txt")


def _collect_olm_detail(diag_dir: Path, operator_ns: str) -> None:
    _oc_to_file(["get", "csv", "-n", operator_ns, "-o", "yaml"], diag_dir / "csv.yaml")
    _oc_to_file(["describe", "sub", "-n", operator_ns], diag_dir / "subscription-describe.txt")

    lines: list[str] = []
    lines.append("=== jobs openshift-marketplace (wide) ===")
    r = _oc(["get", "jobs", "-n", "openshift-marketplace", "-o", "wide"])
    lines.append(r.stdout or "")

    lines.append("=== bundle-unpack job spec (image + SA) ===")
    r = _oc(["get", "jobs", "-n", "openshift-marketplace", "-o", "yaml"])
    if r.stdout:
        for line in r.stdout.splitlines():
            stripped = line.strip()
            if any(
                stripped.startswith(k)
                for k in ("image:", "serviceAccountName:", "activeDeadlineSeconds:")
            ):
                lines.append(line)

    lines.append("=== bundle-unpack job events (trimmed) ===")
    r = _oc(["get", "jobs", "-n", "openshift-marketplace", "-o", "jsonpath={.items[*].metadata.name}"])
    job_names = (r.stdout or "").split()
    for job in job_names:
        lines.append(f"Job: {job}")
        desc = _oc(["describe", "job", job, "-n", "openshift-marketplace"])
        if desc.stdout:
            for dl in desc.stdout.splitlines():
                if any(kw in dl for kw in ("Events", "Image", "Status", "Message", "Reason")):
                    lines.append(dl)
        lines.append("Job logs (last 20 lines):")
        logs = _oc(["logs", f"job/{job}", "-n", "openshift-marketplace", "--tail=20"])
        lines.append(logs.stdout or "  (no logs)")

    lines.append("=== SAs openshift-marketplace (pull secrets) ===")
    r = _oc(
        [
            "get",
            "sa",
            "-n",
            "openshift-marketplace",
            "-o",
            "custom-columns=NAME:.metadata.name,PULL_SECRETS:.imagePullSecrets",
        ]
    )
    lines.append(r.stdout or "")

    (diag_dir / "marketplace-jobs-summary.txt").write_text("\n".join(lines), encoding="utf-8")


def _collect_adm_inspect(diag_dir: Path, operator_ns: str) -> bool:
    inspect_dest = diag_dir / "inspect-ns-operator"
    inspect_dest.mkdir(parents=True, exist_ok=True)
    inspect_log = inspect_dest / "adm-inspect.log"
    ir = _oc(["adm", "inspect", f"ns/{operator_ns}", f"--dest-dir={inspect_dest}"])
    inspect_log.write_text(
        f"exit={ir.returncode}\nSTDERR:\n{ir.stderr or ''}\nSTDOUT:\n{ir.stdout or ''}\n",
        encoding="utf-8",
    )
    if ir.returncode != 0:
        (inspect_dest / "FAILED").write_text(
            f"oc adm inspect exited {ir.returncode}; see adm-inspect.log\n",
            encoding="utf-8",
        )
        print(
            f"WARN: oc adm inspect failed (exit {ir.returncode}); see {inspect_log}",
            file=sys.stderr,
        )
        return False
    return True


def _append_snippet(lines: list[str], path: Path, *, label: str, max_lines: int) -> None:
    if not path.is_file():
        return
    lines.append(f"=== {label} (first {max_lines} lines) ===")
    lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines()[:max_lines])


def _build_manifest(diag_dir: Path, *, adm_inspect_failed: bool, operator_ns: str) -> str:
    lines: list[str] = []
    if adm_inspect_failed:
        lines.append("=== DIAGNOSTICS PARTIAL FAILURE (adm inspect) ===")

    triage = diag_dir / "triage"
    _append_snippet(lines, triage / "issues-summary.txt", label="issues summary", max_lines=40)
    _append_snippet(lines, triage / "status-report.txt", label="status report", max_lines=35)
    _append_snippet(
        lines, triage / "dependency-status-report.txt", label="dependency install status", max_lines=35
    )
    _append_snippet(lines, triage / "operator-highlights.txt", label="operator highlights", max_lines=25)
    _append_snippet(
        lines,
        triage / "dependency-operator-highlights.txt",
        label="dependency operator highlights",
        max_lines=25,
    )

    workload_summary = triage / "workload-logs" / "collection-summary.txt"
    _append_snippet(lines, workload_summary, label="workload pod logs index", max_lines=20)

    for rel in (
        "rhoai-cr-status/dsc-describe.txt",
        "subscription-describe.txt",
        "marketplace-jobs-summary.txt",
    ):
        path = diag_dir / rel
        if not path.is_file():
            continue
        label = rel.replace("/", " ")
        _append_snippet(lines, path, label=label, max_lines=15)

    if adm_inspect_failed:
        lines.append(
            f"oc adm inspect ns/{operator_ns} failed; see inspect-ns-operator/adm-inspect.log"
        )

    raw = "\n".join(lines).encode("utf-8", errors="replace")[:_MANIFEST_MAX]
    return raw.decode("utf-8", errors="ignore")


def _append_pod_log_files(chunks: list[str], log_dir: Path, *, section_title: str) -> None:
    if not log_dir.is_dir():
        return
    for log_file in sorted(log_dir.rglob("*.log")):
        if log_file.name == "collection-summary.log":
            continue
        rel = log_file.relative_to(log_dir)
        chunks.append(f"\n{'=' * 20} {section_title}: {rel} {'=' * 20}\n\n")
        chunks.append(log_file.read_text(encoding="utf-8", errors="replace"))
        if not chunks[-1].endswith("\n"):
            chunks.append("\n")


def _diagnostic_artifact_log_name(
    *,
    since_time: str,
    pipeline_product: str,
    operator_name: str,
    operator_version: str,
    kubeconfig: str,
) -> str:
    """Return ``{product}-{version?}-{cluster?}-diagnostic-{datetime}.log``."""
    installed = infer_installed_product(operator_name, operator_version)
    cluster_label = cluster_label_from_kubeconfig(kubeconfig) if kubeconfig else ""
    return build_diagnostic_artifact_log_name(
        since_time=since_time,
        installed_product=installed,
        operator_version=operator_version,
        cluster_label=cluster_label,
        pipeline_product=pipeline_product,
    )


def _stage_triage_for_artifacts(
    diag_dir: Path,
    *,
    since_time: str,
    pipeline_product: str,
    operator_name: str,
    operator_version: str,
    kubeconfig: str,
) -> Path | None:
    """Merge triage reports and pod logs into one diagnostic log for OCI upload."""
    shared_raw = os.environ.get("TESTS_SHARED_DIR", "").strip()
    if not shared_raw:
        return None

    results_dir = tests_payload_results_dir(Path(shared_raw))
    results_dir.mkdir(parents=True, exist_ok=True)

    chunks: list[str] = []
    for rel_src, title in _TRIAGE_ARTIFACT_SECTIONS:
        src = diag_dir / rel_src
        if not src.is_file():
            continue
        chunks.append(f"\n{'=' * 20} {title} {'=' * 20}\n\n")
        chunks.append(src.read_text(encoding="utf-8", errors="replace"))
        if not chunks[-1].endswith("\n"):
            chunks.append("\n")

    for rel_dir, section_title in _TRIAGE_POD_LOG_DIRS:
        _append_pod_log_files(chunks, diag_dir / rel_dir, section_title=section_title)

    if not chunks:
        return None

    dest = results_dir / _diagnostic_artifact_log_name(
        since_time=since_time,
        pipeline_product=pipeline_product,
        operator_name=operator_name,
        operator_version=operator_version,
        kubeconfig=kubeconfig,
    )
    dest.write_text("".join(chunks), encoding="utf-8")
    print(f"Staged triage for OCI artifacts: {dest.name} under {results_dir}")
    return dest


def _print_file_excerpt_to_step_log(
    path: Path,
    *,
    title: str,
    max_lines: int | None = None,
) -> None:
    if not path.is_file():
        return
    print(f"\n{'=' * 20} {title} {'=' * 20}\n", flush=True)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    limit = max_lines if max_lines is not None else len(lines)
    for line in lines[:limit]:
        print(line, flush=True)
    if len(lines) > limit:
        print(
            f"... ({len(lines) - limit} more lines in OCI artifact {path.name})",
            flush=True,
        )


def _print_pod_log_excerpts_to_step_log(diag_dir: Path) -> None:
    pod_files: list[Path] = []
    for rel_dir, _ in _TRIAGE_POD_LOG_DIRS:
        log_dir = diag_dir / rel_dir
        if not log_dir.is_dir():
            continue
        for log_file in sorted(log_dir.rglob("*.log")):
            if log_file.name == "collection-summary.log":
                continue
            pod_files.append(log_file)
    if not pod_files:
        return
    print(f"\n{'=' * 20} POD LOG EXCERPTS {'=' * 20}\n", flush=True)
    print(
        f"(first {_STEP_LOG_POD_EXCERPT_LINES} lines per pod; "
        f"full logs in OCI artifact — max {_STEP_LOG_POD_FILES_MAX} pods in step log)\n",
        flush=True,
    )
    for log_file in pod_files[:_STEP_LOG_POD_FILES_MAX]:
        rel = log_file.relative_to(diag_dir)
        _print_file_excerpt_to_step_log(
            log_file,
            title=str(rel),
            max_lines=_STEP_LOG_POD_EXCERPT_LINES,
        )
    if len(pod_files) > _STEP_LOG_POD_FILES_MAX:
        print(
            f"... ({len(pod_files) - _STEP_LOG_POD_FILES_MAX} more pod logs in OCI artifact only)",
            flush=True,
        )


def _print_triage_to_step_log(diag_dir: Path, *, artifact_name: str) -> None:
    """Echo diagnostic report sections to Tekton step log (Konflux UI)."""
    print("=== COLLECT-DIAGNOSTICS REPORT (Tekton step log) ===", flush=True)
    print(f"Full artifact: {artifact_name}", flush=True)
    for rel_src, title in _TRIAGE_ARTIFACT_SECTIONS:
        _print_file_excerpt_to_step_log(
            diag_dir / rel_src,
            title=title,
            max_lines=_STEP_LOG_SECTION_MAX_LINES,
        )
    olm_summary = diag_dir / "marketplace-jobs-summary.txt"
    if olm_summary.is_file():
        _print_file_excerpt_to_step_log(
            olm_summary,
            title="OLM MARKETPLACE JOBS",
            max_lines=_STEP_LOG_SECTION_MAX_LINES,
        )
    _print_pod_log_excerpts_to_step_log(diag_dir)


def _detect_operator_version(operator_ns: str, operator_name: str) -> str:
    from install.install_and_verify import pick_succeeded_csv_version

    return pick_succeeded_csv_version(operator_ns, operator_name) or ""


def _resolve_kubeconfig() -> str:
    """Resolve target-cluster kubeconfig (Tekton env, credentials volume, or tests-shared)."""
    candidates: list[Path] = []
    env_path = os.environ.get("KUBECONFIG", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(_DEFAULT_KUBECONFIG)
    shared = os.environ.get("TESTS_SHARED_DIR", "").strip()
    if shared:
        candidates.append(Path(shared) / "credentials" / "kubeconfig")

    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return str(path)
    return env_path or str(_DEFAULT_KUBECONFIG)


def main() -> int:
    operator_ns = require_env("OPERATOR_NAMESPACE")
    result_path = require_env("DIAG_MANIFEST_RESULT")
    op_ver_path = os.environ.get("OPERATOR_VERSION_PATH", "").strip()
    operator_name = os.environ.get("OPERATOR_NAME", "rhods-operator").strip()
    shared_dir = os.environ.get("TESTS_SHARED_DIR", "").strip()
    staged_name = ""
    exit_code = 1
    try:
        kubeconfig = _resolve_kubeconfig()
        os.environ["KUBECONFIG"] = kubeconfig
        if not Path(kubeconfig).is_file():
            if (os.environ.get("CONFORMA_GATE") or "").strip().lower() == CONFORMA_GATE_SKIP:
                msg = "skipped: CONFORMA_GATE=skip (no target cluster)"
                print(f"collect-diagnostics: {msg}")
                write_result(result_path, msg)
                if op_ver_path:
                    write_result(op_ver_path, "(n/a)")
                exit_code = 0
                return exit_code
            msg = f"collect-diagnostics: no kubeconfig at {kubeconfig}"
            print(f"ERROR: {msg}", file=sys.stderr)
            write_result(result_path, f"error: {msg}")
            if op_ver_path:
                write_result(op_ver_path, "(unknown)")
            return 1

        diag_dir = Path(os.environ.get("DIAG_DIR", "/diag").strip())
        diag_dir.mkdir(parents=True, exist_ok=True)

        install_ran = _install_ran()
        collect_pods = _truthy(os.environ.get("DIAG_COLLECT_POD_LOGS"), default=True)
        collect_olm = _truthy(os.environ.get("DIAG_COLLECT_OLM_DETAIL"), default=install_ran)
        collect_inspect = _truthy(
            os.environ.get("DIAG_COLLECT_ADM_INSPECT"),
            default=_should_collect_adm_inspect(),
        )

        since_raw = os.environ.get("PIPELINE_RUN_START_TIME", "").strip()
        try:
            logs_since_time = resolve_logs_since_time(since_raw or None)
        except ValueError as exc:
            msg = f"collect-diagnostics: {exc}"
            print(f"ERROR: {msg}", file=sys.stderr)
            write_result(result_path, f"error: {msg}")
            if op_ver_path:
                write_result(op_ver_path, "(unknown)")
            return 1

        print(
            f"collect-diagnostics: triage={collect_pods} since-time={logs_since_time} olm={collect_olm} "
            f"adm_inspect={collect_inspect} install_ran={install_ran} "
            f"pipeline_failed={_pipeline_failed()}"
        )
        print(f"Writing diagnostics under {diag_dir}...")

        issues_path = diag_dir / "triage" / "issues-summary.txt"
        errors: list[str] = []
        product = os.environ.get("PRODUCT", "").strip()
        op_ver = _detect_operator_version(operator_ns, operator_name)
        artifact_label = _diagnostic_artifact_log_name(
            since_time=logs_since_time,
            pipeline_product=product,
            operator_name=operator_name,
            operator_version=op_ver or "",
            kubeconfig=kubeconfig,
        )
        if collect_pods:
            issues_path = run_rhoai_triage(
                diag_dir, operator_ns=operator_ns, logs_since_time=logs_since_time
            )
            if not issues_path.is_file():
                errors.append("triage did not produce issues-summary.txt")
        _collect_rhoai_cr_status(diag_dir)
        if collect_olm:
            _collect_olm_detail(diag_dir, operator_ns)
        if collect_inspect and not _collect_adm_inspect(diag_dir, operator_ns):
            errors.append(f"oc adm inspect ns/{operator_ns} failed")

        if collect_pods:
            staged = _stage_triage_for_artifacts(
                diag_dir,
                since_time=logs_since_time,
                pipeline_product=product,
                operator_name=operator_name,
                operator_version=op_ver or "",
                kubeconfig=kubeconfig,
            )
            if staged is not None:
                staged_name = staged.name
                _print_triage_to_step_log(diag_dir, artifact_name=staged.name)
            elif shared_dir:
                errors.append(f"failed to stage {artifact_label} for OCI upload")
                _print_triage_to_step_log(diag_dir, artifact_name=artifact_label)
            else:
                _print_triage_to_step_log(diag_dir, artifact_name=artifact_label)

        manifest = _build_manifest(
            diag_dir,
            adm_inspect_failed=any("adm inspect" in e for e in errors),
            operator_ns=operator_ns,
        )
        if errors:
            manifest = "=== COLLECT-DIAGNOSTICS ERRORS ===\n" + "\n".join(errors) + "\n\n" + manifest
        manifest = clamp_tekton_result(manifest, max_bytes=_MANIFEST_MAX)
        write_result(result_path, manifest)
        if op_ver_path:
            write_result(op_ver_path, op_ver or "(unknown)")
            print(f"Operator version: {op_ver or '(unknown)'}")
        if errors:
            for err in errors:
                print(f"ERROR: {err}", file=sys.stderr)
            exit_code = 1
        else:
            exit_code = 0
        return exit_code
    finally:
        if shared_dir:
            status = "failed" if exit_code != 0 else "done"
            marker = mark_collect_diagnostics_done(
                shared_dir,
                artifact_name=staged_name,
                status=status,
            )
            print(f"collect-diagnostics marker: {marker}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
