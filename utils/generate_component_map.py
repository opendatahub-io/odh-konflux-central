#!/usr/bin/env python3

import json
import os
import sys
from urllib.parse import urlparse

import yaml

PIPELINERUNS_DIR = os.path.join(os.getcwd(), "pipelineruns")
EXCLUDED_DIRS = {"template"}
PUSH_SUFFIX = "-push.yaml"
SUFFIX_OVERRIDES = {"notebooks": "-ci-push.yaml"}
OUTPUT_FILE = os.path.join(os.getcwd(), "config", "component_repo_map.json")


def extract_repo_name(annotation_value):
    url_part = annotation_value.split("?")[0]
    return urlparse(url_part).path.rstrip("/").split("/")[-1]


def extract_quay_repo(output_image_value):
    without_scheme = output_image_value.split("quay.io/", 1)[-1]
    return without_scheme.rsplit(":", 1)[0]


def find_output_image(params):
    for param in params:
        if param.get("name") == "output-image":
            return param["value"]
    return None


def process_yaml(filepath):
    with open(filepath) as f:
        doc = yaml.safe_load(f)

    repo_url = doc["metadata"]["annotations"]["build.appstudio.openshift.io/repo"]
    repo_name = extract_repo_name(repo_url)

    component_name = doc["metadata"]["labels"]["appstudio.openshift.io/component"]

    output_image = find_output_image(doc["spec"]["params"])
    if output_image is None:
        print(f"WARNING: No output-image param found in {filepath}", file=sys.stderr)
        return repo_name, component_name, None

    quay_repo = extract_quay_repo(output_image)
    return repo_name, component_name, quay_repo


def collect_subdirs():
    subdirs = []
    for entry in sorted(os.listdir(PIPELINERUNS_DIR)):
        full_path = os.path.join(PIPELINERUNS_DIR, entry)
        if not os.path.isdir(full_path):
            continue
        if entry in EXCLUDED_DIRS:
            continue
        subdirs.append((entry, full_path))
        for nested in sorted(os.listdir(full_path)):
            nested_path = os.path.join(full_path, nested)
            if os.path.isdir(nested_path) and "release" not in nested:
                subdirs.append((f"{entry}/{nested}", nested_path))
    return subdirs


def main():
    entries = []

    for dir_key, dir_path in collect_subdirs():
        top_level_dir = dir_key.split("/")[0]
        suffix = SUFFIX_OVERRIDES.get(top_level_dir, PUSH_SUFFIX)
        push_files = sorted(
            f for f in os.listdir(dir_path)
            if f.endswith(suffix) and "release" not in f and os.path.isfile(os.path.join(dir_path, f))
        )
        if not push_files:
            continue

        repo_names_seen = set()
        components = {}

        for filename in push_files:
            filepath = os.path.join(dir_path, filename)
            repo_name, component_name, quay_repo = process_yaml(filepath)
            repo_names_seen.add(repo_name)
            if quay_repo is not None:
                components[component_name] = quay_repo

        if len(repo_names_seen) > 1:
            print(
                f"WARNING: Mismatched REPO_NAME in {dir_key}: {repo_names_seen}",
                file=sys.stderr,
            )

        repo_name = repo_names_seen.pop() if len(repo_names_seen) == 1 else sorted(repo_names_seen)[0]
        entries.append((dir_key, repo_name, components))

    repo_name_count = {}
    for _, repo_name, _ in entries:
        repo_name_count[repo_name] = repo_name_count.get(repo_name, 0) + 1

    result = {}
    for dir_key, repo_name, components in entries:
        key = dir_key if repo_name_count[repo_name] > 1 else repo_name
        result[key] = components

    with open(OUTPUT_FILE, "w") as f:
        json.dump(result, f, indent=2, sort_keys=True)
        f.write("\n")

    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
