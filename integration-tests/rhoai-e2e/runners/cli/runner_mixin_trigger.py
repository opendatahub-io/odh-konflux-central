"""Trigger/snapshot helpers mixin for RhoaiE2ERunner."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from suite.constants import (
    ANNOTATION_RUN_OWNER,
    DEFAULT_RHOAI_E2E_PIPELINE_TIMEOUT,
    DEFAULT_UPSTREAM_KONFLUX_GIT,
    ITS_TEST_GATES_PARAM_DEFAULT,
    KONFLUX_INTEGRATION_SERVICE_ACCOUNT,
    RHOAI_E2E_EPHC_ITS_NAME,
    RHOAI_FBCF_IMAGE_REF_PATTERN,
)
from suite.its_trigger_params import (
    is_external_cluster_source,
    resolve_cluster_source_for_trigger,
    validate_cluster_source,
)
from suite.trigger_param_registry import (
    build_trigger_context_from_runner,
    build_trigger_explicit_from_args,
    read_committed_its_params,
    resolve_trigger_patch_plan,
    trigger_param_names,
    trigger_params_to_clear_on_stage,
)
from suite.its_registry import integration_test_scenario_application
from suite.rhoai_fbc_ocp import rhoai_fbc_name_from_ocp_minor, rhoai_fbc_name_from_rhoai_version
from k8s.cluster_ocp_version import cluster_ocp_minor_from_kubeconfig
from suite.pipelinerun_naming import build_rhoai_e2e_generate_prefix, default_pipelinerun_generate_prefix, is_rhoai_e2e_pipelinerun_name
from suite.snapshot_catalog_line import rhoai_catalog_version_from_fbc_source
from suite.errors import AppError
from k8s.external_kubeconfig import (
    assert_external_cluster_lock_queryable,
    validate_kubeconfig_path,
)
from k8s.oc_util import filter_warning_lines, parse_json_output, run_cmd
from .rhoai_channel import resolve_rhoai_update_channel
from .runner_support import (
    format_rhoai_e2e_watch_cli,
    pipelinerun_external_cluster_id,
    spin_while,
)


class RunnerTriggerMixin:
    @staticmethod
    def _yq_upsert_its_param(path: Path | str, name: str, value: str) -> None:
        """Replace one ITS ``spec.params`` entry by name (delete then append)."""
        path_str = str(path)
        env_key = f"YQ_{name}"
        run_cmd(
            ["yq", "e", f'del(.spec.params[] | select(.name == "{name}"))', "-i", path_str],
            capture=True,
            check=True,
        )
        run_cmd(
            [
                "yq",
                "e",
                f'.spec.params += [{{"name":"{name}","value":strenv({env_key})}}]',
                "-i",
                path_str,
            ],
            capture=True,
            check=True,
            env={**os.environ, env_key: value},
        )

    def _yq_patch_its_konflux_git(
        self,
        tmp_path: Path | str,
        *,
        konflux_repo: str = "",
        konflux_branch: str = "",
    ) -> None:
        """Patch resolverRef and SCRIPTS_REPO_* params on a staged ITS manifest."""
        path_str = str(tmp_path)
        if konflux_repo:
            run_cmd(
                [
                    "yq",
                    "e",
                    '(.spec.resolverRef.params[] | select(.name == "url")).value = strenv(YQ_KONFLUX_REPO)',
                    "-i",
                    path_str,
                ],
                capture=True,
                check=True,
                env={**os.environ, "YQ_KONFLUX_REPO": konflux_repo},
            )
            self._yq_upsert_its_param(tmp_path, "SCRIPTS_REPO_URL", konflux_repo)
        if konflux_branch:
            run_cmd(
                [
                    "yq",
                    "e",
                    '(.spec.resolverRef.params[] | select(.name == "revision")).value = strenv(YQ_KONFLUX_BRANCH)',
                    "-i",
                    path_str,
                ],
                capture=True,
                check=True,
                env={**os.environ, "YQ_KONFLUX_BRANCH": konflux_branch},
            )
            self._yq_upsert_its_param(tmp_path, "SCRIPTS_REPO_REVISION", konflux_branch)

    def _clear_registry_params_from_staged_its(self, tmp_path: Path | str) -> None:
        names = sorted(trigger_params_to_clear_on_stage())
        expr = " or ".join(f'.name == "{n}"' for n in names)
        run_cmd(
            ["yq", "e", f"del(.spec.params[] | select({expr}))", "-i", str(tmp_path)],
            capture=True,
            check=True,
        )

    def _patch_its_cli_override_params(
        self, tmp_path: Path | str, odh_overrides: bool
    ) -> tuple[str, dict[str, str], str, str]:
        """Upsert trigger params on staged ITS via ``trigger_param_registry``."""
        secret = (self.external_kubeconfig_secret or "").strip()
        if not secret and self._external_kubeconfig_its_override():
            secret = self._resolve_external_kubeconfig_secret()
        committed = read_committed_its_params(self.its_file)
        its_params: dict[str, str] = {}
        for name in trigger_param_names():
            value = self._read_its_param(name, path=tmp_path)
            if value:
                its_params[name] = value
        explicit = build_trigger_explicit_from_args(self.args)
        for key, value in (getattr(self.args, "trigger_explicit", {}) or {}).items():
            if value is not None:
                explicit[key] = value
        ctx = build_trigger_context_from_runner(
            self,
            external_secret=secret,
            odh_overrides=odh_overrides,
            committed_its_params=committed,
        )
        values, patch_plan = resolve_trigger_patch_plan(
            ctx,
            its_params=its_params,
            explicit=explicit,
        )
        cluster_source = values.get("CLUSTER_SOURCE", "")
        validate_cluster_source(cluster_source)
        for name in trigger_param_names():
            if patch_plan.get(name):
                self._yq_upsert_its_param(tmp_path, name, values.get(name, ""))
        if self.args.konflux_repo:
            self._yq_set_resolver_ref_param(
                tmp_path, "url", "YQ_RESOLVER_URL", self.args.konflux_repo
            )
        if self.args.konflux_branch:
            self._yq_set_resolver_ref_param(
                tmp_path, "revision", "YQ_RESOLVER_REV", self.args.konflux_branch
            )
        version_display = {
            "RHOAI_VERSION": values.get("RHOAI_VERSION", ""),
            "OCP_VERSION": values.get("OCP_VERSION", ""),
            "RHOAI_FBC_IMAGE": values.get("RHOAI_FBC_IMAGE", ""),
        }
        return (
            cluster_source,
            version_display,
            values.get("RHOAI_FBC_NAME", ""),
            values.get("UPDATE_CHANNEL", "stable"),
        )

    @staticmethod
    def _yq_append_its_param(tmp_path: str, name: str, value: str) -> None:
        run_cmd(
            [
                "yq",
                "e",
                f'.spec.params += [{{"name":"{name}","value":strenv(YQ_APPEND_VALUE)}}]',
                "-i",
                tmp_path,
            ],
            capture=True,
            check=True,
            env={**os.environ, "YQ_APPEND_VALUE": value},
        )

    @staticmethod
    def _yq_set_resolver_ref_param(tmp_path: str, param_name: str, env_key: str, value: str) -> None:
        run_cmd(
            [
                "yq",
                "e",
                f'(.spec.resolverRef.params[] | select(.name == "{param_name}")).value = strenv({env_key})',
                "-i",
                tmp_path,
            ],
            capture=True,
            check=True,
            env={**os.environ, env_key: value},
        )

    def _resolve_rhoai_konflux_app(self) -> None:
        """Set ``resolved_app`` from Konflux application naming (channel + UI version labels)."""
        if self.resolved_app or self.args.product != "rhoai":
            return
        explicit_app = (self.args.app or "").strip()
        if getattr(self.args, "konflux_app_explicit", False) and explicit_app:
            self.resolved_app = explicit_app
            return
        if (self.args.image or "").strip() and self.image:
            if self._resolve_rhoai_konflux_app_from_image():
                return
        if not self.args.version:
            return
        prefix = f"rhoai-v{self.args.version.replace('.', '-')}"
        apps = [a for a in self.get_applications("rhoai-tenant") if re.match(rf"^{re.escape(prefix)}(-|$)", a)]
        if not apps:
            return
        best_ts = ""
        best_app = ""
        for app in apps:
            ts, _img, _snap_meta = self.latest_matching_image("rhoai-tenant", app, RHOAI_FBCF_IMAGE_REF_PATTERN)
            if ts > best_ts:
                best_ts = ts
                best_app = app
        if best_app:
            self.resolved_app = best_app

    def _resolve_rhoai_konflux_app_from_image(self) -> bool:
        """Match ``resolved_app`` to the Konflux app that published the explicit FBC digest."""
        digest_m = re.search(r"sha256:[a-f0-9]{64}", (self.image or "").strip(), re.IGNORECASE)
        if not digest_m:
            return False
        digest = digest_m.group(0).lower()
        prefix = f"rhoai-v{self.args.version.replace('.', '-')}" if self.args.version else "rhoai-v"
        apps = [
            a
            for a in self.get_applications("rhoai-tenant")
            if not self.args.version or re.match(rf"^{re.escape(prefix)}(-|$)", a)
        ]
        for app in sorted(apps):
            _ts, img, _snap_meta = self.latest_matching_image(
                "rhoai-tenant", app, RHOAI_FBCF_IMAGE_REF_PATTERN
            )
            if img and digest in img.lower():
                self.resolved_app = app
                return True
        return False

    def _resolve_target_ocp_minor(self) -> str:
        """OCP minor for FBC fragment selection (--ocp-version or external kubeconfig)."""
        if self.args.product != "rhoai":
            return ""
        explicit = (getattr(self.args, "ocp_version", "") or "").strip()
        if explicit:
            return explicit
        path = getattr(self.args, "external_kubeconfig_path", None)
        if path is not None:
            detected = cluster_ocp_minor_from_kubeconfig(path)
            if detected:
                print(f"Detected cluster OCP {detected} from external kubeconfig")
                return detected
        return ""

    def _resolve_rhoai_fbc_on_version_apps(
        self,
        *,
        apps: list[str],
        fbc_component_name: str,
        rhoai_version_label: str,
        allow_fragment_app_fallback: bool = True,
    ) -> None:
        best_ts = ""
        for app in apps:
            ts, img, snap_meta = self.latest_named_component_image_on_application(
                "rhoai-tenant",
                app,
                fbc_component_name,
                RHOAI_FBCF_IMAGE_REF_PATTERN,
            )
            if img and ts > best_ts:
                best_ts = ts
                self.image = img
                self.resolved_app = app
                self._fbc_source_snapshot_meta = snap_meta
        if self.image:
            return
        if not allow_fragment_app_fallback:
            return
        fragment_ts = ""
        fragment_img = ""
        fragment_meta: dict[str, Any] | None = None
        ts, img, snap_meta = self.latest_named_component_image_on_application(
            "rhoai-tenant",
            fbc_component_name,
            fbc_component_name,
            RHOAI_FBCF_IMAGE_REF_PATTERN,
        )
        if img and ts > fragment_ts:
            fragment_ts = ts
            fragment_img = img
            fragment_meta = snap_meta
        if fragment_img:
            self.image = fragment_img
            self.resolved_app = fbc_component_name
            self._fbc_source_snapshot_meta = fragment_meta
            return
        fallback_ts = ""
        fallback_img = ""
        fallback_app = ""
        fallback_meta: dict[str, Any] | None = None
        for app in apps:
            ts, img, snap_meta = self.latest_matching_image(
                "rhoai-tenant",
                app,
                RHOAI_FBCF_IMAGE_REF_PATTERN,
            )
            if img and ts > fallback_ts:
                fallback_ts = ts
                fallback_img = img
                fallback_app = app
                fallback_meta = snap_meta
        if fallback_img:
            self.image = fallback_img
            self.resolved_app = fallback_app
            self._fbc_source_snapshot_meta = fallback_meta
            return
        apps_s = ", ".join(sorted(apps))
        raise AppError(
            f"No FBCF snapshot found for RHOAI {rhoai_version_label} component {fbc_component_name!r} "
            f"(apps tried: {apps_s}, fragment app {fbc_component_name!r}; "
            f"image must match {RHOAI_FBCF_IMAGE_REF_PATTERN!r}). "
            "Pass --image <ref> or --ocp-version MAJOR.MINOR."
        )

    @staticmethod
    def _rhoai_app_version_key(app: str) -> tuple[int, ...]:
        version_m = re.match(r"^rhoai-v(\d+(?:-\d+)*)", app)
        if not version_m:
            return ()
        parts: list[int] = []
        for segment in version_m.group(1).split("-"):
            if segment.isdigit():
                parts.append(int(segment))
        return tuple(parts)

    def _snapshot_yaml_container_image(self) -> str:
        if not getattr(self, "snapshot_file", None):
            return ""
        snap_text = self.snapshot_file.read_text(encoding="utf-8")
        img_match = re.search(r"(?m)^\s+containerImage:\s+(\S+)", snap_text)
        return img_match.group(1).strip() if img_match else ""

    def _run_its_pinned_fbcf_fallback(self) -> str:
        return (
            (getattr(self, "_run_its_pinned_fbcf_image", "") or "").strip()
            or self._snapshot_yaml_container_image()
        )

    def _apply_pinned_fbcf_fallback(self, *, reason: str) -> None:
        pinned = self._run_its_pinned_fbcf_fallback()
        if not pinned or self.image:
            return
        print(f"WARN {reason} — using pinned fallback from snapshot YAML: {pinned}")
        self.image = pinned

    def _ordered_rhoai_version_stream_apps(self) -> list[str]:
        """``rhoai-v*`` applications in priority order (3.5 EA streams first)."""
        apps = [a for a in self.get_applications("rhoai-tenant") if a.startswith("rhoai-v")]
        priority = ("rhoai-v3-5-ea-2", "rhoai-v3-5-ea-1", "rhoai-v3-5")
        ordered_priority = [a for a in priority if a in apps]
        rest = sorted(a for a in apps if a not in priority)
        return list(dict.fromkeys([*ordered_priority, *rest]))

    def _resolve_rhoai_fbc_latest_for_component(self, fbc_component_name: str) -> None:
        """Newest Konflux snapshot for ``fbc_component_name``.

        Searches the FBC fragment application (e.g. ``rhoai-fbc-fragment-ocp-420`` when
        the component name matches the app) and ``rhoai-v*`` version-stream apps; picks
        the newest ``creationTimestamp`` unless ``--image`` was set earlier.
        """
        want = (fbc_component_name or "").strip()
        if not want:
            return

        best_ts = ""
        best_img = ""
        best_app = ""
        best_meta: dict[str, Any] | None = None

        for app in (want, *self._ordered_rhoai_version_stream_apps()):
            ts, img, snap_meta = self.latest_named_component_image(
                "rhoai-tenant",
                app,
                want,
                RHOAI_FBCF_IMAGE_REF_PATTERN,
            )
            if img and ts > best_ts:
                best_ts, best_img, best_app, best_meta = ts, img, app, snap_meta

        if best_img:
            self.image = best_img
            self.resolved_app = best_app
            self._fbc_source_snapshot_meta = best_meta

    def _resolve_rhoai_fbc_for_its_application(
        self,
        konflux_app: str,
        component_name: str,
    ) -> None:
        """Newest FBC image on the ITS ``spec.application`` only (fast ``--run-its`` path)."""
        want_comp = (component_name or "").strip()
        want_app = (konflux_app or component_name or "").strip()
        if not want_app or not want_comp:
            return
        _, img, snap_meta = self.latest_named_component_image_on_application(
            "rhoai-tenant",
            want_app,
            want_comp,
            RHOAI_FBCF_IMAGE_REF_PATTERN,
        )
        if not img:
            return
        self.image = img
        self.resolved_app = want_app
        self._fbc_source_snapshot_meta = snap_meta

    def resolve_image(self, odh_overrides: bool) -> None:
        if self.image:
            print(f"Using provided image: {self.image}")
            self._resolve_rhoai_konflux_app()
        elif not self.args.product:
            print(
                "INFO test-only (no --product): skipping FBC catalog resolution; "
                "SNAPSHOT omits containerImage unless --image is set; "
                "extract-fbcf-image records n/a."
            )
            return
        elif self.args.product == "rhoai" and self.args.version:
            prefix = f"rhoai-v{self.args.version.replace('.', '-')}"
            ocp_minor = self._resolve_target_ocp_minor()
            if ocp_minor:
                self.resolved_ocp_minor = ocp_minor
            fbc_name = rhoai_fbc_name_from_ocp_minor(ocp_minor) if ocp_minor else ""
            if fbc_name:
                self.resolved_rhoai_fbc_name = fbc_name
                apps = [
                    a
                    for a in self.get_applications("rhoai-tenant")
                    if re.match(rf"^{re.escape(prefix)}(-|$)", a)
                ]
                if not apps:
                    raise AppError(f"No Konflux application found matching {prefix}* in rhoai-tenant")
                primary_app = prefix if prefix in apps else sorted(apps)[0]
                ordered_apps = [primary_app] + sorted(a for a in apps if a != primary_app)
                version_stream_fbc = rhoai_fbc_name_from_rhoai_version(self.args.version)
                component_candidates: list[tuple[str, list[str]]] = []
                if version_stream_fbc:
                    component_candidates.append((version_stream_fbc, [primary_app]))
                effective_channel = (self.args.channel or "").strip() or (
                    resolve_rhoai_update_channel(
                        version=self.args.version or "",
                        resolved_app=primary_app,
                    )
                    or ""
                )
                # stable-* needs the version-stream FBC (e.g. v3-5 GA channel); ocp-4XX catalogs
                # may only publish beta/ea and will never satisfy stable-3.5 install.
                skip_ocp_fbc = effective_channel.startswith("stable-")
                if (
                    fbc_name
                    and all(comp != fbc_name for comp, _ in component_candidates)
                    and not skip_ocp_fbc
                ):
                    component_candidates.append((fbc_name, ordered_apps))
                resolved_comp = ""
                for idx, (comp, lookup_apps) in enumerate(component_candidates):
                    with spin_while(
                        f"Resolving FBCF image for RHOAI {self.args.version} "
                        f"({comp} on {', '.join(lookup_apps)})"
                    ):
                        self._resolve_rhoai_fbc_on_version_apps(
                            apps=lookup_apps,
                            fbc_component_name=comp,
                            rhoai_version_label=self.args.version,
                            allow_fragment_app_fallback=idx == len(component_candidates) - 1,
                        )
                    if self.image:
                        resolved_comp = comp
                        break
                if not self.image:
                    apps_s = ", ".join(sorted(apps))
                    comps_s = ", ".join(comp for comp, _ in component_candidates)
                    raise AppError(
                        f"No FBCF snapshot found for RHOAI {self.args.version} "
                        f"(components tried: {comps_s}; apps tried: {apps_s}). "
                        "Pass --image <ref> or check Konflux snapshots."
                    )
                if resolved_comp:
                    self.resolved_rhoai_fbc_name = resolved_comp
                print(
                    f"RHOAI {self.args.version} FBC image ({self.resolved_rhoai_fbc_name}): {self.image} "
                    f"(from {self.resolved_app})"
                )
            else:
                with spin_while(
                    f"Resolving latest FBCF image for RHOAI {self.args.version} (apps matching {prefix}*)"
                ):
                    apps = [
                        a
                        for a in self.get_applications("rhoai-tenant")
                        if re.match(rf"^{re.escape(prefix)}(-|$)", a)
                    ]
                    if not apps:
                        raise AppError(f"No Konflux application found matching {prefix}* in rhoai-tenant")
                    best_ts = ""
                    for app in apps:
                        ts, img, snap_meta = self.latest_matching_image(
                            "rhoai-tenant", app, RHOAI_FBCF_IMAGE_REF_PATTERN
                        )
                        if img and ts > best_ts:
                            best_ts = ts
                            self.image = img
                            self.resolved_app = app
                            self._fbc_source_snapshot_meta = snap_meta
                    if not self.image:
                        apps_s = ", ".join(sorted(apps))
                        raise AppError(
                            f"No FBCF snapshot found for RHOAI {self.args.version} "
                            f"(apps tried: {apps_s}; image must match {RHOAI_FBCF_IMAGE_REF_PATTERN!r}). "
                            "If Snapshots only list another image name, use --image <ref> or check Konflux application naming."
                        )
                print(f"RHOAI {self.args.version} FBCF image: {self.image} (from {self.resolved_app})")
        elif self.args.product == "rhoai" and (getattr(self, "resolved_rhoai_fbc_name", "") or "").strip():
            fbc_name = self.resolved_rhoai_fbc_name.strip()
            ocp_minor = self._resolve_target_ocp_minor()
            if ocp_minor:
                self.resolved_ocp_minor = ocp_minor
            run_its = bool((getattr(self.args, "run_its", "") or "").strip())
            if run_its:
                manifest = Path(getattr(self.args, "its_manifest_path", "") or "")
                its_app = integration_test_scenario_application(manifest) if manifest.is_file() else ""
                its_app = its_app or fbc_name
                with spin_while(
                    f"Resolving latest Konflux FBCF image for {fbc_name} (ITS application {its_app})"
                ):
                    self._resolve_rhoai_fbc_for_its_application(its_app, fbc_name)
                miss_reason = f"no Konflux snapshot found for {fbc_name} on ITS application {its_app!r}"
            else:
                with spin_while(f"Resolving latest Konflux FBCF image for {fbc_name}"):
                    self._resolve_rhoai_fbc_latest_for_component(fbc_name)
                miss_reason = (
                    f"no Konflux snapshot found for {fbc_name} "
                    f"(searched FBC app {fbc_name!r} and rhoai-v* streams)"
                )
            if self.image:
                print(f"Latest FBCF image for {fbc_name}: {self.image} (from {self.resolved_app})")
            else:
                self._apply_pinned_fbcf_fallback(reason=miss_reason)
        elif self.args.product == "rhoai":
            with spin_while("Fetching latest FBCF image across all RHOAI apps (highest version)"):
                apps = [a for a in self.get_applications("rhoai-tenant") if a.startswith("rhoai-v")]
                best_key: tuple[tuple[int, ...], str] = ((), "")
                for app in apps:
                    ts, img, snap_meta = self.latest_matching_image("rhoai-tenant", app, RHOAI_FBCF_IMAGE_REF_PATTERN)
                    version_m = re.match(r"^rhoai-v(\d+(?:-\d+)*)", app)
                    version_key = tuple(int(p) for p in version_m.group(1).split("-")) if version_m else ()
                    if img and (version_key, ts) > best_key:
                        best_key = (version_key, ts)
                        self.image = img
                        self.resolved_app = app
                        self._fbc_source_snapshot_meta = snap_meta
            if self.image:
                print(f"Latest FBCF image: {self.image} (from {self.resolved_app})")
            else:
                self._apply_pinned_fbcf_fallback(
                    reason="could not fetch latest FBCF image from Konflux",
                )

        if not self.update_channel_override and self.args.product == "rhoai":
            auto_channel = resolve_rhoai_update_channel(
                version=self.args.version or "",
                resolved_app=self.resolved_app or "",
            )
            if auto_channel:
                self.update_channel_override = auto_channel
                source = (
                    f"--rhoai-version {self.args.version}"
                    if self.args.version
                    else self.resolved_app or "rhoai app"
                )
                print(f"Auto-selected channel: {self.update_channel_override} (from {source})")


    def ensure_its_applied(self, odh_overrides: bool) -> None:
        self._render_its_for_trigger(odh_overrides)
        self._apply_rendered_its_to_cluster()

    def _render_its_for_trigger(self, odh_overrides: bool) -> None:
        print("Rendering IntegrationTestScenario params for trigger...")
        tests_baseline = getattr(self.args, "tests_catalog_default_csv", ITS_TEST_GATES_PARAM_DEFAULT)
        if not shutil.which("yq"):
            raise AppError(
                "yq is required to patch the ITS (PRODUCT, --konflux-repo, --rhoai-channel, etc.)."
            )

        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.its_apply_tmp = tmp.name
        del_names = sorted(trigger_params_to_clear_on_stage())
        expr = " or ".join(f'.name == "{n}"' for n in del_names)
        proc = run_cmd(["yq", "e", f"del(.spec.params[] | select({expr}))", str(self.its_file)], capture=True, check=True)
        Path(self.its_apply_tmp).write_text(proc.stdout, encoding="utf-8")

        cluster_source, version_display, rhoai_fbc_name, update_channel = (
            self._patch_its_cli_override_params(self.its_apply_tmp, odh_overrides)
        )
        slack_channel = (self.args.slack_channel_id or "").strip()
        fbc_version = rhoai_catalog_version_from_fbc_source(
            fbc_image=(self.image or "").strip(),
            fbc_snapshot_meta=getattr(self, "_fbc_source_snapshot_meta", None),
            resolved_app=(getattr(self, "resolved_app", "") or "").strip(),
        )
        generate_prefix = build_rhoai_e2e_generate_prefix(
            product=self.args.product,
            version=fbc_version,
            cluster_source=cluster_source,
            cluster_label=self._trigger_cluster_label(),
            target_type=self._trigger_target_type(),
            tests_csv=self.args.tests,
            components_csv=getattr(self.args, "components", "") or "",
            run_owner=self.run_owner,
        )
        self._pipelinerun_generate_prefix = generate_prefix

        print(
            "  ITS overrides:"
            f" pipelinerun_prefix={generate_prefix!r}"
            f" resolverRef={self.args.konflux_repo or '<default>'}@{self.args.konflux_branch or '<default>'}"
            f" SCRIPTS_REPO={self.args.konflux_repo or '<default>'}@{self.args.konflux_branch or '<default>'}"
            f" UPDATE_CHANNEL={update_channel}"
            f" OCP_VERSION_PREFIX={self.args.ocp_version or '<pipeline default>'}"
            f" OCP_RELEASE_CHANNEL={getattr(self.args, 'ocp_channel', '') or '<pipeline default>'}"
            f" RHOAI_VERSION={version_display['RHOAI_VERSION']}"
            f" OCP_VERSION={version_display['OCP_VERSION']}"
            f" RHOAI_FBC_NAME={rhoai_fbc_name or '<ITS default>'}"
            f" RHOAI_FBC_IMAGE={version_display['RHOAI_FBC_IMAGE']}"
            f" TEST_GATES={self.args.tests if self.args.tests != tests_baseline else '<ITS default>'}"
            f" COMPONENTS={getattr(self.args, 'components', '') or '<ITS default>'}"
            f" COMPONENT_TEST_TIMEOUT={self.args.test_timeout or '<pipeline default>'}"
            f" TEST_TAGS={getattr(self.args, 'test_tags', '') or '<pipeline default>'}"
            f" CLEANUP={'true' if self._cleanup_its_override() else '<pipeline default>'}"
            f" SLACK_CHANNEL_ID={slack_channel or '<disabled>'}"
            f" PRODUCT={self.args.product}"
            f" INSTALL_DEPENDENCIES={'true' if getattr(self.args, 'install_dependencies', False) else '<pipeline default>'}"
            f" CLUSTER_SOURCE={cluster_source or '(empty — no cluster)'}"
            f" shift_left_secret={self.smoke_aws_secret or '<catalog default at runtime>'}"
            f" OLMINSTALL_TESTS_VERSION_OVERRIDE={getattr(self.args, 'tests_rhoai_version', '') or '<probe CSV>'}"
        )

    def _apply_rendered_its_to_cluster(self) -> None:
        print("Applying IntegrationTestScenario to cluster...")
        proc = run_cmd(["oc", "apply", "-n", self.args.namespace, "-f", self.its_apply_tmp], capture=True, check=False)
        filtered = filter_warning_lines(f"{proc.stdout}\n{proc.stderr}")
        if filtered.strip():
            print(filtered, file=sys.stderr)
        if proc.returncode != 0:
            raise AppError("ITS apply failed")
        print("ITS ready")


    def _cluster_source_for_its(self) -> str:
        secret = (self.external_kubeconfig_secret or "").strip()
        if not secret and self._external_kubeconfig_its_override():
            secret = self._resolve_external_kubeconfig_secret()
        return resolve_cluster_source_for_trigger(
            product=self.args.product,
            external_secret=secret,
        )


    def _read_its_param(self, name: str, *, path: Path | str | None = None) -> str:
        source = path if path is not None else self.its_file
        proc = run_cmd(
            ["yq", "e", f'(.spec.params[] | select(.name == "{name}") | .value) // ""', str(source)],
            capture=True,
            check=True,
        )
        return proc.stdout.strip().strip('"')


    def _snapshot_git_source(self) -> tuple[str, str]:
        """HTTPS git source for Snapshot ``spec.components[].source`` (Konflux Reference column)."""
        url = (getattr(self.args, "konflux_repo", "") or "").strip()
        rev = (getattr(self.args, "konflux_branch", "") or "").strip()
        if not url:
            url = "https://github.com/opendatahub-io/odh-konflux-central.git"
        if not rev:
            rev = "main"
        return url, rev


    def _inject_snapshot_component_git_source(self, snap_yaml: str) -> str:
        """Add ``source.git`` after ``containerImage`` when missing (valid URL for Konflux UI)."""
        if re.search(r"^\s*source:\s*$", snap_yaml, flags=re.MULTILINE):
            return snap_yaml
        url, rev = self._snapshot_git_source()
        block = (
            "      source:\n"
            "        git:\n"
            f"          url: {url}\n"
            f'          revision: "{rev}"'
        )
        return re.sub(
            r"(^\s*containerImage:\s*.+$)",
            lambda m: m.group(0) + "\n" + block,
            snap_yaml,
            count=1,
            flags=re.MULTILINE,
        )


    def _build_snapshot_json(self, odh_overrides: bool) -> str:
        """Build inline SNAPSHOT JSON from snapshot template (no Snapshot CR needed)."""
        snap_yaml = self.snapshot_file.read_text(encoding="utf-8")
        comp_name_match = re.search(r"(?m)^\s+-\s+name:\s+(\S+)\s*$", snap_yaml)
        if not comp_name_match and not (getattr(self, "resolved_rhoai_fbc_name", "") or "").strip():
            raise AppError(f"Could not locate a component name in {self.snapshot_file}")
        comp_name = (getattr(self, "resolved_rhoai_fbc_name", "") or "").strip() or comp_name_match.group(1)
        img_match = re.search(r"(?m)^\s+containerImage:\s+(\S+)", snap_yaml)
        if not img_match and not self.image:
            raise AppError(f"Could not locate containerImage in {self.snapshot_file}")
        container_image = img_match.group(1) if img_match else ""
        if self.image:
            container_image = self.image.strip()
        elif not self.args.product:
            container_image = ""
        if odh_overrides:
            comp_name = "odh-operator-catalog"
        url, rev = self._snapshot_git_source()
        comp: dict[str, Any] = {
            "name": comp_name,
            "source": {"git": {"url": url, "revision": rev}},
        }
        if container_image:
            comp["containerImage"] = container_image
        return json.dumps({"application": self.args.app, "components": [comp]})


    def _read_its_params_from_tmp(self) -> dict[str, str]:
        """Read ITS params from the yq-patched tmp file as {name: value}."""
        if not self.its_apply_tmp:
            return {}
        proc = run_cmd(
            ["yq", "e", "-o=json", ".spec.params", self.its_apply_tmp],
            capture=True, check=True,
        )
        try:
            params = json.loads(proc.stdout)
        except (ValueError, TypeError):
            return {}
        if not isinstance(params, list):
            return {}
        return {p["name"]: str(p.get("value", "")) for p in params if isinstance(p, dict) and "name" in p}


    def _read_its_resolver_ref(self) -> tuple[str, str]:
        """Read resolverRef url/revision from the patched ITS tmp file."""
        if not self.its_apply_tmp:
            return DEFAULT_UPSTREAM_KONFLUX_GIT, "main"
        url = ""
        rev = ""
        proc = run_cmd(
            ["yq", "e", "-o=json", ".spec.resolverRef.params", self.its_apply_tmp],
            capture=True, check=True,
        )
        try:
            params = json.loads(proc.stdout)
        except (ValueError, TypeError):
            params = []
        for p in (params if isinstance(params, list) else []):
            if not isinstance(p, dict):
                continue
            if p.get("name") == "url":
                url = str(p.get("value", ""))
            elif p.get("name") == "revision":
                rev = str(p.get("value", ""))
        return url or DEFAULT_UPSTREAM_KONFLUX_GIT, rev or "main"


    def create_direct_pipelinerun(self, odh_overrides: bool) -> None:
        """Create PipelineRun directly (no Snapshot/Integration Service) with dynamic generateName."""
        self._cli_direct_pipelinerun = True
        snapshot_json = self._build_snapshot_json(odh_overrides)
        self._trigger_snapshot_spec = json.loads(snapshot_json)
        its_params = self._read_its_params_from_tmp()
        cluster_source = self._cluster_source_for_its()
        if cluster_source:
            its_params["CLUSTER_SOURCE"] = cluster_source
        resolver_url, resolver_rev = self._read_its_resolver_ref()
        generate_prefix = getattr(self, "_pipelinerun_generate_prefix", default_pipelinerun_generate_prefix())

        pr_params: list[dict[str, str]] = [{"name": "SNAPSHOT", "value": snapshot_json}]
        for pname, pvalue in its_params.items():
            if pname == "WAIT_FOR_CONFORMA":
                continue
            pr_params.append({"name": pname, "value": pvalue})
        # Manual CLI/--run-its: skip conforma wait (ITS auto runs use pipeline default true).
        pr_params.append({"name": "WAIT_FOR_CONFORMA", "value": "false"})

        force_cluster = bool(getattr(self.args, "force_cluster_run", False))
        if force_cluster:
            pr_params.append({"name": "FORCE_CLUSTER_RUN", "value": "true"})

        labels: dict[str, str] = {"appstudio.openshift.io/application": self.args.app}
        labels.update(self.build_rhoai_e2e_trigger_labels())
        annotations = self._build_trigger_resource_annotations()

        pr_obj: dict[str, Any] = {
            "apiVersion": "tekton.dev/v1",
            "kind": "PipelineRun",
            "metadata": {
                "generateName": generate_prefix,
                "labels": labels,
                "annotations": {k: v for k, v in annotations.items() if (v or "").strip()},
            },
            "spec": {
                "timeouts": {"pipeline": DEFAULT_RHOAI_E2E_PIPELINE_TIMEOUT},
                "taskRunTemplate": {
                    "serviceAccountName": KONFLUX_INTEGRATION_SERVICE_ACCOUNT,
                },
                "pipelineRef": {
                    "resolver": "git",
                    "params": [
                        {"name": "url", "value": resolver_url},
                        {"name": "revision", "value": resolver_rev},
                        {"name": "pathInRepo", "value": "integration-tests/rhoai-e2e/tekton/pipelines/rhoai-e2e-pipeline.yaml"},
                    ],
                },
                "params": pr_params,
                "workspaces": [
                    {
                        "name": "tests-shared",
                        "volumeClaimTemplate": {
                            "spec": {
                                "accessModes": ["ReadWriteOnce"],
                                "resources": {"requests": {"storage": "15Gi"}},
                            }
                        },
                    }
                ],
            },
        }

        pr_yaml = json.dumps(pr_obj)
        with spin_while(f"Creating PipelineRun directly (app: {self.args.app}, prefix: {generate_prefix})"):
            proc = run_cmd(
                ["oc", "create", "-n", self.args.namespace, "-f", "-", "-o", "jsonpath={.metadata.name}"],
                capture=True,
                check=True,
                input_text=pr_yaml,
            )
            self.pr = proc.stdout.strip()

        self.cleanup_snapshot_on_exit = False
        self.cleanup_external_secret_on_exit = False
        print(f"PipelineRun: {self.pr}")
        print(f"  Run owner: {self.run_owner}")
        from runners.report.pipelinerun_metadata import build_reference_text

        ref = build_reference_text(
            fbcf_image=self._trigger_fbcf_image(),
            ocp_version=(getattr(self.args, "ocp_version", "") or "").strip(),
        )
        if ref:
            print(f"  FBC catalog: {ref}")
        link = f"{self.konflux_ui}/ns/{self.args.namespace}/applications/{self.args.app}/activity/pipelineruns"
        print(f"  Konflux UI: {link}")


    def snapshot_matches_trigger(self, snap_value: str) -> bool:
        """True if this PipelineRun ``SNAPSHOT`` param is the Snapshot we created for this trigger.

        When ``_trigger_snapshot_spec`` is set (normal after ``create_direct_pipelinerun``), comparison is strict
        full-spec equality. The image/substring heuristic runs only if that spec was not captured.
        """
        if not self.snapshot_name:
            return False
        if snap_value == self.snapshot_name:
            return True
        par = self._parse_snapshot_param_as_spec(snap_value)
        if par is None:
            return False
        if par.get("application") != self.args.app:
            return False
        ref = self._trigger_snapshot_spec
        if ref:
            return par.get("application") == ref.get("application") and par.get("components") == ref.get("components")
        if self.image:
            components = par.get("components") or []
            return any((c or {}).get("containerImage") == self.image for c in components)
        return False


    def _poll_pipelinerun_for_snapshot(self, snap_created: str) -> bool:
        """Return True when a matching rhoai-e2e PipelineRun name is stored in ``self.pr``."""
        items = self.get_pipelineruns(self.args.namespace)
        cands: list[tuple[str, str]] = []
        for item in items:
            name = item.get("metadata", {}).get("name", "")
            app = item.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/application", "")
            if not is_rhoai_e2e_pipelinerun_name(name) or app != self.args.app:
                continue
            snap = ""
            for p in item.get("spec", {}).get("params", []):
                if p.get("name") == "SNAPSHOT":
                    snap = str(p.get("value", ""))
                    break
            if not self.snapshot_matches_trigger(snap):
                continue
            pr_created = (item.get("metadata", {}).get("creationTimestamp", "") or "").strip()
            if snap_created and pr_created and pr_created < snap_created:
                continue
            self._fail_fast_resolver_terminal(name)
            cands.append((item.get("metadata", {}).get("creationTimestamp", ""), name))
        if not cands:
            return False
        cands.sort()
        self.pr = cands[-1][1]
        return True


    def wait_for_pipelinerun(self) -> None:
        attempts = max(1, (self.pr_appear_timeout + 4) // 5)
        msg_prefix = f"Waiting for PipelineRun to start (snapshot: {self.snapshot_name})"
        snap_created = (self._trigger_snapshot_created_ts or "").strip()
        found = False
        with spin_while(msg_prefix):
            for attempt in range(1, attempts + 1):
                if self._poll_pipelinerun_for_snapshot(snap_created):
                    found = True
                    break
                if attempt < attempts:
                    time.sleep(5)
        if not found or not self.pr:
            self.cleanup_snapshot_on_exit = False
            watch_hint = format_rhoai_e2e_watch_cli(
                operator_install_dir=self.script_dir,
                namespace=self.args.namespace,
                app=self.args.app,
                pipelinerun="",
            )
            raise AppError(
                f"PipelineRun did not appear after {self.pr_appear_timeout}s. Check Konflux:\n"
                f"{self.konflux_ui}/ns/{self.args.namespace}/applications/{self.args.app}/activity/pipelineruns\n"
                f"Snapshot: {self.snapshot_name}\n"
                "If the Snapshot shows ``No required IntegrationTestScenarios found``, confirm "
                f"``oc get integrationtestscenario -n {self.args.namespace} {RHOAI_E2E_EPHC_ITS_NAME}`` "
                "exists and was applied by this trigger (``rhoai_e2e.py`` labels manual snapshots for "
                "the ``push`` ITS context).\n"
                f"When the run appears, follow logs with:\n  {watch_hint}"
            )

        self._finalize_trigger_pipelinerun()


    def _finalize_trigger_pipelinerun(self) -> None:
        """Annotate the trigger PipelineRun and keep Snapshot/Secret for the run lifetime."""
        self._oc_merge_annotations("pipelinerun", self.pr, self._build_trigger_resource_annotations())

        pr_lbl = ["oc", "label", "pipelinerun", self.pr, "-n", self.args.namespace]
        for key, val in self.build_rhoai_e2e_trigger_labels().items():
            pr_lbl.append(f"{key}={val}")
        pr_lbl.append("--overwrite")
        self._oc_label_required(pr_lbl, f"pipelinerun/{self.pr}")
        # PipelineRun still needs the trigger Snapshot and external kubeconfig Secret.
        self.cleanup_snapshot_on_exit = False
        self.cleanup_external_secret_on_exit = False

    def _reenable_external_secret_cleanup_on_terminal(self, final_cstat: str) -> None:
        """Re-enable CLI-created kubeconfig Secret cleanup after the run finishes."""
        if final_cstat in ("True", "False") and self._external_secret_created_by_cli:
            self.cleanup_external_secret_on_exit = True


    def _build_trigger_resource_annotations(self) -> dict[str, str]:
        ann = dict(self.build_rhoai_e2e_context_annotations())
        ann[ANNOTATION_RUN_OWNER] = self.run_owner
        return ann


    def _oc_merge_annotations(self, kind: str, name: str, annotations: dict[str, str]) -> None:
        clean = {k: v for k, v in annotations.items() if (v or "").strip()}
        if not clean:
            return
        patch = {"metadata": {"annotations": clean}}
        proc = run_cmd(
            [
                "oc",
                "patch",
                kind,
                name,
                "-n",
                self.args.namespace,
                "--type=merge",
                "-p",
                json.dumps(patch),
            ],
            capture=True,
            check=False,
        )
        if proc.returncode == 0:
            return
        detail = filter_warning_lines(f"{proc.stdout or ''}\n{proc.stderr or ''}").strip()
        raise AppError(
            f"Failed to patch {kind}/{name} annotations in namespace {self.args.namespace}: "
            f"{detail or f'oc exited {proc.returncode}'}"
        )


    def _oc_label_required(self, cmd: list[str], resource: str) -> None:
        proc = run_cmd(cmd, capture=True, check=False)
        if proc.returncode == 0:
            return
        detail = filter_warning_lines(f"{proc.stdout or ''}\n{proc.stderr or ''}").strip()
        raise AppError(
            f"Failed to label {resource} in namespace {self.args.namespace}: "
            f"{detail or f'oc exited {proc.returncode}'}"
        )


    def _oc_annotate_required(self, cmd: list[str], resource: str) -> None:
        proc = run_cmd(cmd, capture=True, check=False)
        if proc.returncode == 0:
            return
        detail = filter_warning_lines(f"{proc.stdout or ''}\n{proc.stderr or ''}").strip()
        raise AppError(
            f"Failed to annotate {resource} in namespace {self.args.namespace}: "
            f"{detail or f'oc exited {proc.returncode}'}"
        )


    def _infer_konflux_git_pipeline_ref_from_env_only(self) -> tuple[str, str]:
        """Optional (repo, revision) from env — both required; otherwise ITS keeps opendatahub-io @ ``main``."""
        env_url = (os.environ.get("OLMINSTALL_PIPELINE_REPO") or os.environ.get("KONFLUX_PIPELINE_REPO") or "").strip()
        env_rev = (os.environ.get("OLMINSTALL_PIPELINE_REVISION") or os.environ.get("KONFLUX_PIPELINE_REVISION") or "").strip()
        if env_url and env_rev:
            return env_url, env_rev
        return "", ""


    def _apply_konflux_git_inference_from_clone_or_env(self) -> None:
        """Apply ``--konflux-repo`` / ``--konflux-branch`` only from env when both are set there.

        With no CLI flags and no env pair, the committed ITS default applies: opendatahub-io/odh-konflux-central @ main.
        """
        if self.args.konflux_repo or self.args.konflux_branch:
            return
        before_repo, before_branch = self.args.konflux_repo, self.args.konflux_branch
        url, rev = self._infer_konflux_git_pipeline_ref_from_env_only()
        if url:
            self.args.konflux_repo = url
        if rev:
            self.args.konflux_branch = rev
        if self.args.konflux_repo != before_repo or self.args.konflux_branch != before_branch:
            print(
                "INFO Konflux pipeline Git resolver from OLMINSTALL_PIPELINE_* / KONFLUX_PIPELINE_* "
                f"(override with --konflux-repo / --konflux-branch): {self.args.konflux_repo} @ {self.args.konflux_branch}",
                file=sys.stderr,
            )


    def _prepare_external_cluster_before_trigger(
        self,
        *,
        owned_running: str,
        items_by_name: dict[str, dict[str, Any]],
    ) -> None:
        """Verify Konflux can evaluate cluster locks; idle wait runs in external-cluster-ready."""
        cluster_source = self._cluster_source_for_its()
        if not is_external_cluster_source(cluster_source):
            return
        force_cluster = bool(getattr(self.args, "force_cluster_run", False))
        target_cluster = self._trigger_cluster_label()
        assert_external_cluster_lock_queryable(
            namespace=self.args.namespace,
            cluster_source=cluster_source,
            cluster_id=target_cluster,
            force=force_cluster,
        )
        if not owned_running:
            return
        owned_item = items_by_name.get(owned_running) or {}
        owned_cluster = pipelinerun_external_cluster_id(
            owned_item,
            namespace=self.args.namespace,
        )
        if not owned_cluster or not target_cluster or owned_cluster != target_cluster:
            return
        watch_owned = format_rhoai_e2e_watch_cli(
            operator_install_dir=self.script_dir,
            namespace=self.args.namespace,
            app=self.args.app,
            pipelinerun=owned_running,
        )
        print(
            f"INFO External cluster {target_cluster!r} is in use by owned PipelineRun "
            f"{owned_running}; new run will queue in external-cluster-ready until idle.",
            flush=True,
        )
        print(f"  Stream existing run: {watch_owned}", flush=True)

    def run_trigger_mode(self) -> None:
        self._apply_konflux_git_inference_from_clone_or_env()
        rows: list[tuple[str, str, str, str, str]] = []
        items_by_name: dict[str, dict[str, Any]] = {}
        for item in self.get_pipelineruns(self.args.namespace):
            name = item.get("metadata", {}).get("name", "")
            app = item.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/application", "")
            if not is_rhoai_e2e_pipelinerun_name(name) or app != self.args.app:
                continue
            if item.get("status", {}).get("completionTime"):
                continue
            items_by_name[name] = item
            snap = ""
            for p in item.get("spec", {}).get("params", []):
                if p.get("name") == "SNAPSHOT":
                    snap = str(p.get("value", ""))
                    break
            owner = item.get("metadata", {}).get("annotations", {}).get("rhoai-e2e.run-owner", "")
            pipe = item.get("metadata", {}).get("labels", {}).get("tekton.dev/pipeline", "")
            rows.append((item.get("metadata", {}).get("creationTimestamp", ""), name, snap, owner, pipe))
        rows.sort(key=lambda x: x[0], reverse=True)
        owned_running = self._pick_newest_owned_pipelinerun(rows)
        self._prepare_external_cluster_before_trigger(
            owned_running=owned_running,
            items_by_name=items_by_name,
        )
        if owned_running:
            watch_owned = format_rhoai_e2e_watch_cli(
                operator_install_dir=self.script_dir,
                namespace=self.args.namespace,
                app=self.args.app,
                pipelinerun=owned_running,
            )
            print(
                f"INFO Owned rhoai-e2e PipelineRun still running: {owned_running}. "
                "Trigger mode starts a new run (your flags apply to the new run only). "
                f"To stream the existing run instead:\n  {watch_owned}"
            )
        elif rows:
            print(
                f"WARN Found active PipelineRun(s) for app '{self.args.app}' without a matching owner marker; "
                "triggering a new run."
            )
        if self.args.konflux_repo and not self.args.konflux_branch:
            print(
                "WARN --konflux-repo is set without --konflux-branch; the ITS resolver revision stays the "
                "YAML default (``main``). Pass --konflux-branch <ref> to use your fork branch.",
                file=sys.stderr,
            )
        odh_overrides = False
        self.resolve_image(odh_overrides)
        self._render_its_for_trigger(odh_overrides)
        self.create_direct_pipelinerun(odh_overrides)


