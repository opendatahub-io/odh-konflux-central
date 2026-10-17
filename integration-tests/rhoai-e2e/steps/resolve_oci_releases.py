#!/usr/bin/env python3
"""Write OpenShift CI ``releases`` JSON and OCP minor for provision-ephemeral-cluster.

Env:
  OVERRIDE — optional ``OCP_VERSION_PREFIX`` (e.g. ``4.21`` or ``4.21.``).
  RHOAI_FBC_NAME — e.g. ``rhoai-fbc-fragment-ocp-421`` (used when OVERRIDE is empty).
  OCP_RELEASE_CHANNEL — ``stable`` (default), ``candidate`` (EC), or ``nightly``.
  DEFAULT_MINOR — fallback minor when FBC name has no OCP suffix (default ``4.21``).
  MINOR_RESULT_PATH — Tekton result file for the resolved minor (``4.21``).
  CHANNEL_RESULT_PATH — Tekton result file for the normalized channel (``stable``/``candidate``/``nightly``).
  RELEASES_RESULT_PATH — Tekton result file for ci-operator ``releases`` JSON.
"""

from __future__ import annotations

import json
import os
import sys

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from steps.tekton_util import require_env, write_result
from suite.its_trigger_params import ocp_install_prefix
from suite.rhoai_fbc_ocp import ocp_minor_from_rhoai_fbc_name

DEFAULT_OCI_OCP_MINOR = "4.21"
OCI_RELEASE_CHANNELS = ("stable", "candidate", "nightly")


def resolve_ocp_minor(
    override_raw: str,
    rhoai_fbc_name: str,
    default_minor: str = DEFAULT_OCI_OCP_MINOR,
) -> str:
    """Prefer pipeline prefix, then FBC component suffix, then *default_minor*."""
    prefix = ocp_install_prefix(override_raw)
    if prefix:
        return prefix
    from_fbc = ocp_minor_from_rhoai_fbc_name(rhoai_fbc_name)
    if from_fbc:
        return from_fbc
    fallback = (default_minor or "").strip() or DEFAULT_OCI_OCP_MINOR
    return fallback


def normalize_ocp_release_channel(raw: str) -> str:
    """Return ``stable``, ``candidate``, or ``nightly``."""
    ch = (raw or "stable").strip().lower() or "stable"
    if ch not in OCI_RELEASE_CHANNELS:
        raise ValueError(
            f"OCP_RELEASE_CHANNEL must be one of {', '.join(OCI_RELEASE_CHANNELS)}, got {raw!r}"
        )
    return ch


def releases_json(ocp_minor: str, ocp_channel: str = "stable") -> str:
    """ci-operator ``releases`` for hypershift-hostedcluster-workflow."""
    ch = normalize_ocp_release_channel(ocp_channel)
    if ch == "nightly":
        payload: dict = {
            "latest": {
                "candidate": {
                    "product": "ocp",
                    "stream": "nightly",
                    "version": ocp_minor,
                }
            }
        }
    else:
        payload = {
            "latest": {
                "release": {
                    "channel": ch,
                    "version": ocp_minor,
                    "architecture": "multi",
                }
            }
        }
    return json.dumps(payload, separators=(",", ":"))


def main() -> int:
    override_raw = os.environ.get("OVERRIDE", "")
    fbc_name = os.environ.get("RHOAI_FBC_NAME", "")
    default_minor = os.environ.get("DEFAULT_MINOR", DEFAULT_OCI_OCP_MINOR)
    ocp_channel = normalize_ocp_release_channel(os.environ.get("OCP_RELEASE_CHANNEL", "stable"))
    minor = resolve_ocp_minor(override_raw, fbc_name, default_minor=default_minor)
    print(
        f"OpenShift CI ephemeral OCP minor: {minor} channel: {ocp_channel}",
        flush=True,
    )
    write_result(require_env("MINOR_RESULT_PATH"), minor)
    write_result(require_env("CHANNEL_RESULT_PATH"), ocp_channel)
    write_result(require_env("RELEASES_RESULT_PATH"), releases_json(minor, ocp_channel))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
