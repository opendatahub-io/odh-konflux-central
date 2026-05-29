#!/usr/bin/env python3
"""
Overlay SNAPSHOT image URIs onto operands-map.yaml and manifests-config.yaml.

For each component present in the Konflux SNAPSHOT, this script finds the
matching entry in operands-map.yaml (by comparing the image repository portion)
and replaces its value with the SNAPSHOT's image URI (which includes a digest).

It also updates manifests-config.yaml entries whose git.url matches a SNAPSHOT
component, setting git.commit to the SNAPSHOT's commit so that manifest
prefetching checks out the correct source revision.

Components NOT in the SNAPSHOT are left unchanged (resolved via buildVersionTag).
"""

import argparse
import json
import sys

from ruamel.yaml import YAML

yaml_handler = YAML()
yaml_handler.preserve_quotes = True


def parse_image_repo(image_ref):
    """Extract registry/org/repo from 'registry/org/repo@sha256:...' or 'registry/org/repo:tag'."""
    if "@" in image_ref:
        return image_ref.split("@")[0]
    if ":" in image_ref:
        parts = image_ref.rsplit(":", 1)
        if "/" in parts[1]:
            return image_ref
        return parts[0]
    return image_ref


def build_snapshot_maps(snapshot):
    """Return (repo_to_image, git_url_to_info) dicts from parsed SNAPSHOT JSON."""
    repo_to_image = {}
    git_url_to_info = {}

    for comp_name, comp_data in snapshot.items():
        if not isinstance(comp_data, dict) or "image" not in comp_data:
            continue
        image = comp_data["image"]
        repo = parse_image_repo(image)
        repo_to_image[repo] = image

        git_url = comp_data.get("git.url", "")
        git_commit = comp_data.get("git.commit", "")
        if git_url and git_commit:
            git_url_to_info[git_url] = {
                "commit": git_commit,
                "component": comp_name,
            }

    return repo_to_image, git_url_to_info


def override_operands_map(operands_map_path, repo_to_image):
    """Replace operands-map entries whose image repo matches a SNAPSHOT component."""
    with open(operands_map_path) as f:
        operands_map = yaml_handler.load(f)

    if not operands_map:
        print("operands-map.yaml is empty, nothing to override.")
        return 0

    count = 0
    for entry in operands_map:
        value = str(entry.get("value", ""))
        entry_repo = parse_image_repo(value)
        if entry_repo in repo_to_image:
            print(
                f"SNAPSHOT OVERRIDE {entry.get('name', '?')}: "
                f"{value} -> {repo_to_image[entry_repo]}"
            )
            entry["value"] = repo_to_image[entry_repo]
            count += 1

    with open(operands_map_path, "w") as f:
        yaml_handler.dump(operands_map, f)

    return count


def override_manifests_config(manifests_config_path, git_url_to_info):
    """Update git.commit in manifests-config for components whose git.url matches SNAPSHOT."""
    with open(manifests_config_path) as f:
        mc = yaml_handler.load(f)

    if not mc or "map" not in mc:
        print("manifests-config.yaml has no 'map' section, nothing to override.")
        return 0

    count = 0
    for comp_key in mc["map"]:
        comp_val = mc["map"][comp_key]
        git_url = str(comp_val.get("git.url", ""))
        if git_url in git_url_to_info:
            old_commit = comp_val.get("git.commit", "")
            new_commit = git_url_to_info[git_url]["commit"]
            comp_name = git_url_to_info[git_url]["component"]
            print(
                f"SNAPSHOT OVERRIDE manifests-config[{comp_key}]: "
                f"git.commit {old_commit} -> {new_commit} (from {comp_name})"
            )
            comp_val["git.commit"] = new_commit
            if comp_val.get("ref_type") == "branch":
                comp_val["ref_type"] = "commit"
            count += 1

    with open(manifests_config_path, "w") as f:
        yaml_handler.dump(mc, f)

    return count


def main():
    parser = argparse.ArgumentParser(
        description="Apply SNAPSHOT image overrides to operands-map.yaml"
    )
    parser.add_argument(
        "--snapshot-file",
        required=True,
        help="Path to file containing SNAPSHOT JSON",
    )
    parser.add_argument(
        "--operands-map",
        required=True,
        help="Path to operands-map.yaml",
    )
    parser.add_argument(
        "--manifests-config",
        default="",
        help="Path to manifests-config.yaml (optional)",
    )
    args = parser.parse_args()

    with open(args.snapshot_file) as f:
        snapshot = json.load(f)

    repo_to_image, git_url_to_info = build_snapshot_maps(snapshot)

    if not repo_to_image:
        print("No image entries found in SNAPSHOT, nothing to override.")
        return

    om_count = override_operands_map(args.operands_map, repo_to_image)
    print(f"Applied {om_count} SNAPSHOT override(s) to operands-map.yaml")

    if args.manifests_config and git_url_to_info:
        mc_count = override_manifests_config(args.manifests_config, git_url_to_info)
        print(f"Applied {mc_count} SNAPSHOT override(s) to manifests-config.yaml")


if __name__ == "__main__":
    main()
