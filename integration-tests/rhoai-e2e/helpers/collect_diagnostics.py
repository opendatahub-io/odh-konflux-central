#!/usr/bin/env python3
"""Collect failure diagnostics from the target cluster.

Env (required):
    OPERATOR_NAMESPACE
    DIAG_MANIFEST_RESULT -- Tekton result file path
Env (optional):
    DIAG_DIR -- output directory (default /diag)
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

_RHOAI_E2E = Path(__file__).resolve().parent.parent
if str(_RHOAI_E2E) not in sys.path:
    sys.path.insert(0, str(_RHOAI_E2E))

from helpers.tekton_util import require_env, run, write_result

_OC = shutil.which("oc") or "oc"
_MARKETPLACE_NAMESPACE = "openshift-marketplace"
_BUNDLE_UNPACK_LABEL = "operatorframework.io/bundle-unpack-ref"


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


def _catalog_sources(operator_namespace: str) -> list[tuple[str, str]]:
    result = _oc(["get", "subscriptions", "-n", operator_namespace, "-o", "json"])
    if result.returncode == 0:
        try:
            items = json.loads(result.stdout or "{}").get("items") or []
        except json.JSONDecodeError:
            items = []
        sources = {
            (
                str((item.get("spec") or {}).get("sourceNamespace") or _MARKETPLACE_NAMESPACE).strip(),
                str((item.get("spec") or {}).get("source") or "").strip(),
            )
            for item in items
            if isinstance(item, dict)
        }
        sources = {(namespace, name) for namespace, name in sources if namespace}
        if sources:
            return sorted(sources)
    return [(_MARKETPLACE_NAMESPACE, "")]


def main() -> int:
    ns = require_env("OPERATOR_NAMESPACE")
    result_path = require_env("DIAG_MANIFEST_RESULT")
    diag_dir = Path(os.environ.get("DIAG_DIR", "/diag").strip())
    diag_dir.mkdir(parents=True, exist_ok=True)

    print(f"Writing heavy diagnostics under {diag_dir} (not streaming full YAML to pipeline log)...")

    inspect_dest = diag_dir / "inspect-ns-operator"
    inspect_dest.mkdir(parents=True, exist_ok=True)
    inspect_log = inspect_dest / "adm-inspect.log"
    ir = _oc(["adm", "inspect", f"ns/{ns}", f"--dest-dir={inspect_dest}"])
    inspect_log.write_text(
        f"exit={ir.returncode}\nSTDERR:\n{ir.stderr or ''}\nSTDOUT:\n{ir.stdout or ''}\n",
        encoding="utf-8",
    )
    inspect_failed = ir.returncode != 0
    if inspect_failed:
        (inspect_dest / "FAILED").write_text(
            f"oc adm inspect exited {ir.returncode}; see adm-inspect.log\n",
            encoding="utf-8",
        )
        print(
            f"ERROR: oc adm inspect failed (exit {ir.returncode}); see {inspect_log}",
            file=sys.stderr,
        )
    _oc_to_file(["get", "csv", "-n", ns, "-o", "yaml"], diag_dir / "csv.yaml")
    _oc_to_file(["describe", "sub", "-n", ns], diag_dir / "subscription-describe.txt")
    _oc_to_file(
        ["get", "installplans,csv,operatorgroups", "-n", ns, "-o", "yaml"],
        diag_dir / "operator-install-resources.yaml",
    )

    lines: list[str] = []
    for source_namespace, catalog_name in _catalog_sources(ns):
        lines.append(f"=== CatalogSource {catalog_name or '<all>'} in {source_namespace} ===")
        catalog_args = ["get", "catalogsources", "-n", source_namespace]
        if catalog_name:
            catalog_args = ["get", "catalogsource", catalog_name, "-n", source_namespace]
        catalog = _oc([*catalog_args, "-o", "yaml"])
        lines.extend([catalog.stdout or "", catalog.stderr or ""])

        jobs_args = ["get", "jobs", "-n", source_namespace, "-l", _BUNDLE_UNPACK_LABEL]
        lines.append(f"=== bundle-unpack jobs in {source_namespace} (wide) ===")
        jobs_wide = _oc([*jobs_args, "-o", "wide"])
        lines.extend([jobs_wide.stdout or "", jobs_wide.stderr or ""])
        jobs_json = _oc([*jobs_args, "-o", "json"])
        try:
            jobs = json.loads(jobs_json.stdout or "{}").get("items") or []
        except json.JSONDecodeError:
            jobs = []
        for item in jobs:
            if not isinstance(item, dict):
                continue
            job = str((item.get("metadata") or {}).get("name") or "").strip()
            if not job:
                continue
            lines.append(f"=== Job {job} in {source_namespace} ===")
            for args in (
                ["describe", "job", job, "-n", source_namespace],
                ["get", "pods", "-n", source_namespace, "-l", f"job-name={job}", "-o", "wide"],
                ["describe", "pods", "-n", source_namespace, "-l", f"job-name={job}"],
            ):
                result = _oc(args)
                lines.extend([result.stdout or "", result.stderr or ""])
            pods_result = _oc(
                ["get", "pods", "-n", source_namespace, "-l", f"job-name={job}", "-o", "json"]
            )
            try:
                pods = json.loads(pods_result.stdout or "{}").get("items") or []
            except json.JSONDecodeError:
                pods = []
            for pod_item in pods:
                if not isinstance(pod_item, dict):
                    continue
                pod = str((pod_item.get("metadata") or {}).get("name") or "").strip()
                if not pod:
                    continue
                for container in ("pull", "extract"):
                    lines.append(f"=== logs {pod} container {container} ===")
                    result = _oc(
                        ["logs", pod, "-n", source_namespace, "-c", container, "--tail=100"]
                    )
                    lines.extend([result.stdout or "", result.stderr or ""])
        lines.append(f"=== events in {source_namespace} ===")
        events = _oc(["get", "events", "-n", source_namespace, "--sort-by=.lastTimestamp"])
        lines.extend([events.stdout or "", events.stderr or ""])
        lines.append(f"=== service accounts in {source_namespace} ===")
        service_accounts = _oc(
            ["get", "sa", "-n", source_namespace,
             "-o", "custom-columns=NAME:.metadata.name,PULL_SECRETS:.imagePullSecrets"]
        )
        lines.extend([service_accounts.stdout or "", service_accounts.stderr or ""])

    summary_path = diag_dir / "olm-bundle-unpack-summary.txt"
    summary_path.write_text("\n".join(line for line in lines if line), encoding="utf-8")

    # Bundle beside diag_dir so "tar -C diag_dir ." never archives the growing output file.
    bundle = diag_dir.parent / "diagnostics-bundle.tgz"
    tar_r = run(["tar", "czf", str(bundle), "-C", str(diag_dir), "."], check=False, capture=True)
    if tar_r.returncode != 0:
        tail = ((tar_r.stderr or "") + (tar_r.stdout or "")).strip()[:2000]
        print(
            f"ERROR: tar diagnostics bundle failed (exit {tar_r.returncode}): {tail or '(no output)'}",
            file=sys.stderr,
        )
    elif bundle.is_file() and bundle.stat().st_size > 0:
        run(["du", "-sh", str(diag_dir), str(bundle)], check=False)
        print(f"Diagnostics bundle: {bundle} (copy from collect-diagnostics TaskRun pod if needed).")
    else:
        print(f"ERROR: tar did not produce a non-empty {bundle.name}", file=sys.stderr)

    # Build manifest result (truncated to 3584 bytes)
    manifest_lines: list[str] = []
    if inspect_failed:
        manifest_lines.append("=== DIAGNOSTICS PARTIAL FAILURE ===")
        manifest_lines.append(
            f"oc adm inspect ns/{ns} failed (exit {ir.returncode}); "
            f"see {inspect_dest}/adm-inspect.log and {inspect_dest}/FAILED"
        )
    manifest_lines.append(f"=== {diag_dir} file listing ===")
    for f in sorted(diag_dir.rglob("*")):
        if f.is_file():
            manifest_lines.append(f"  {f} ({f.stat().st_size} bytes)")

    sub_desc = diag_dir / "subscription-describe.txt"
    if sub_desc.exists():
        manifest_lines.append("=== subscription-describe (first 80 lines) ===")
        manifest_lines.extend(sub_desc.read_text(encoding="utf-8", errors="replace").splitlines()[:80])

    olm_summary = diag_dir / "olm-bundle-unpack-summary.txt"
    if olm_summary.exists():
        manifest_lines.append("=== olm-bundle-unpack-summary (first 120 lines) ===")
        manifest_lines.extend(olm_summary.read_text(encoding="utf-8", errors="replace").splitlines()[:120])

    raw = "\n".join(manifest_lines).encode("utf-8", errors="replace")[:3584]
    manifest = raw.decode("utf-8", errors="ignore")
    write_result(result_path, manifest)
    if inspect_failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
