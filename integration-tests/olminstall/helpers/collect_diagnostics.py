#!/usr/bin/env python3
"""Collect failure diagnostics from the target cluster.

Env (required):
    OPERATOR_NAMESPACE
    DIAG_MANIFEST_RESULT -- Tekton result file path
Env (optional):
    DIAG_DIR -- output directory (default /diag)
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

_OLMINSTALL = Path(__file__).resolve().parent.parent
if str(_OLMINSTALL) not in sys.path:
    sys.path.insert(0, str(_OLMINSTALL))

from helpers.tekton_util import require_env, run, write_result

_OC = shutil.which("oc") or "oc"


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

    # Marketplace jobs summary
    lines: list[str] = []
    lines.append("=== jobs openshift-marketplace (wide) ===")
    r = _oc(["get", "jobs", "-n", "openshift-marketplace", "-o", "wide"])
    lines.append(r.stdout or "")

    lines.append("=== bundle-unpack job spec (image + SA) ===")
    r = _oc(["get", "jobs", "-n", "openshift-marketplace", "-o", "yaml"])
    if r.stdout:
        for line in r.stdout.splitlines():
            stripped = line.strip()
            if any(stripped.startswith(k) for k in ("image:", "serviceAccountName:", "activeDeadlineSeconds:")):
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
    r = _oc(["get", "sa", "-n", "openshift-marketplace",
             "-o", "custom-columns=NAME:.metadata.name,PULL_SECRETS:.imagePullSecrets"])
    lines.append(r.stdout or "")

    (diag_dir / "marketplace-jobs-summary.txt").write_text("\n".join(lines), encoding="utf-8")

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

    mkt = diag_dir / "marketplace-jobs-summary.txt"
    if mkt.exists():
        manifest_lines.append("=== marketplace-jobs-summary (first 120 lines) ===")
        manifest_lines.extend(mkt.read_text(encoding="utf-8", errors="replace").splitlines()[:120])

    raw = "\n".join(manifest_lines).encode("utf-8", errors="replace")[:3584]
    manifest = raw.decode("utf-8", errors="ignore")
    write_result(result_path, manifest)
    if inspect_failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
