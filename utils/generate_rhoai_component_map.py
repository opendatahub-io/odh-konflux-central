#!/usr/bin/env python3

import argparse
import json
import os
import re
import sys

import yaml

ENV_PREFIX = "RELATED_IMAGE_"
ENV_SUFFIX = "_IMAGE"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate RHOAI component map from RHOAI-Build-Config bundle-patch.yaml"
    )
    parser.add_argument(
        "--rhoai-bundle",
        required=True,
        help="Path to RHOAI bundle-patch.yaml",
    )
    parser.add_argument(
        "--odh-map",
        required=True,
        help="Path to existing ODH component_repo_map.json",
    )
    parser.add_argument(
        "--rhoai-branch",
        required=True,
        help="RHOAI branch name (e.g., rhoai-3.5) — used as fallback-tag",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output file path (default: config/component_repo_map_rhoai.json)",
    )
    return parser.parse_args()


def extract_quay_repo(image_value):
    """Extract quay repo path from image value, stripping quay.io/ prefix and @sha256: digest or :tag."""
    without_scheme = image_value.split("quay.io/", 1)[-1]
    # Strip @sha256: digest
    without_digest = without_scheme.split("@")[0]
    # Strip :tag if present
    without_tag = without_digest.rsplit(":", 1)[0]
    return without_tag


def env_name_to_component(env_name):
    """Derive component name from env var name.

    RELATED_IMAGE_ODH_DASHBOARD_IMAGE -> odh-dashboard
    RELATED_IMAGE_ODH_KSERVE_CONTROLLER_IMAGE -> odh-kserve-controller
    """
    stripped = env_name
    if stripped.startswith(ENV_PREFIX):
        stripped = stripped[len(ENV_PREFIX):]
    if stripped.endswith(ENV_SUFFIX):
        stripped = stripped[:-len(ENV_SUFFIX)]
    return stripped.lower().replace("_", "-")


def build_reverse_lookup(odh_map):
    """Build a reverse lookup: component_name -> repo_name from the ODH map."""
    lookup = {}
    for repo_name, components in odh_map.items():
        for comp_name in components:
            lookup[comp_name] = repo_name
    return lookup


def find_odh_component(derived_name, reverse_lookup):
    """Find the ODH component name and repo for a derived RHOAI component name.

    Tries multiple matching strategies:
    1. Exact match with -ci suffix: odh-dashboard -> odh-dashboard-ci
    2. Without odh- prefix + -ci suffix: odh-kserve-controller -> kserve-controller-ci
    3. Exact match without -ci (some components don't have -ci suffix)
    4. Without odh- prefix, without -ci
    """
    candidates = [
        f"{derived_name}-ci",
        f"{derived_name}-ubi9-ci",
    ]
    if derived_name.startswith("odh-"):
        without_odh = derived_name[4:]
        candidates.append(f"{without_odh}-ci")
        candidates.append(f"{without_odh}-ubi9-ci")

    candidates.append(derived_name)
    if derived_name.startswith("odh-"):
        candidates.append(derived_name[4:])

    # Fuzzy: find components that start with the derived name
    for comp_name in reverse_lookup:
        if comp_name.startswith(f"{derived_name}-") and comp_name not in candidates:
            candidates.append(comp_name)

    for candidate in candidates:
        if candidate in reverse_lookup:
            return candidate, reverse_lookup[candidate]

    return None, None


def main():
    args = parse_args()

    # Parse RHOAI bundle-patch
    with open(args.rhoai_bundle) as f:
        rhoai_patch = yaml.safe_load(f)

    related_images = rhoai_patch.get("patch", {}).get("relatedImages", [])
    if not related_images:
        print("ERROR: No relatedImages found in RHOAI bundle-patch", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(related_images)} entries in RHOAI bundle-patch", file=sys.stderr)

    # Load ODH component map
    with open(args.odh_map) as f:
        odh_map = json.load(f)

    reverse_lookup = build_reverse_lookup(odh_map)

    # Process each RHOAI entry
    rhoai_components = {}
    unmatched = []

    for entry in related_images:
        env_name = entry.get("name", "")
        image_value = entry.get("value", "")

        if not env_name.startswith(ENV_PREFIX) or not image_value:
            continue

        rhoai_quay = extract_quay_repo(image_value)
        derived_name = env_name_to_component(env_name)

        odh_comp_name, repo_name = find_odh_component(derived_name, reverse_lookup)

        if odh_comp_name is None:
            unmatched.append((env_name, derived_name, rhoai_quay))
            continue

        if repo_name not in rhoai_components:
            rhoai_components[repo_name] = {}
        rhoai_components[repo_name][odh_comp_name] = rhoai_quay

    # Build output
    result = {
        "fallback-tag": args.rhoai_branch,
        "components": dict(sorted(rhoai_components.items())),
    }

    # Sort components within each repo
    for repo in result["components"]:
        result["components"][repo] = dict(sorted(result["components"][repo].items()))

    # Determine output path
    output_path = args.output
    if output_path is None:
        output_path = os.path.join(os.path.dirname(args.odh_map), "component_repo_map_rhoai.json")

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")

    print(json.dumps(result, indent=2))

    # Report stats
    matched_count = sum(len(v) for v in rhoai_components.values())
    print(f"\nMatched: {matched_count} components across {len(rhoai_components)} repos", file=sys.stderr)

    if unmatched:
        print(f"\nUnmatched ({len(unmatched)} entries — RHOAI-only, not in ODH map):", file=sys.stderr)
        for env_name, derived, quay in unmatched:
            print(f"  {derived} -> {quay}", file=sys.stderr)


if __name__ == "__main__":
    main()
