"""Select existing service build contexts for push, PR, and manual workflows."""

import json
import os
from pathlib import Path
import re
import subprocess


def git(*args):
    return subprocess.check_output(["git", *args]).decode()


def select(event, requested, before, base):
    services = sorted(path.parent.name for path in Path("services").glob("*/Containerfile"))
    for service in services:
        if not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", service):
            raise ValueError(f"Invalid service image name: {service}")
    if event == "workflow_dispatch":
        if requested and requested not in services:
            raise ValueError(f"No services/{requested}/Containerfile; available: {services}")
        return [requested] if requested else services
    if event == "pull_request":
        changed = git("diff", "--name-only", "-z", f"{base}...HEAD").split("\0")
    elif not before or set(before) == {"0"}:
        return services
    else:
        # A force-pushed-away base may no longer be fetchable: build everything.
        if subprocess.run(["git", "cat-file", "-e", f"{before}^{{commit}}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            return services
        changed = git("diff", "--name-only", "-z", before, "HEAD").split("\0")
    if any(path == ".github/workflows/container-build.yml" or path.startswith(".github/scripts/")
           for path in changed):
        return services
    return [service for service in services if any(path.startswith(f"services/{service}/") for path in changed)]


def main():
    tag = os.environ.get("CUSTOM_TAG", "")
    if tag:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
            raise ValueError("Custom tag must be a valid image tag of at most 128 characters")
        if tag == "buildcache":
            raise ValueError("buildcache is reserved for the registry build cache")
        if tag == "latest" and os.environ["REF_NAME"] != os.environ["DEFAULT_BRANCH"]:
            raise ValueError("latest is reserved for the default branch")
    services = select(os.environ["EVENT_NAME"], os.environ.get("REQUESTED_CONTAINER", ""),
                      os.environ.get("BEFORE_SHA", ""), os.environ.get("BASE_SHA", ""))
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"containers={json.dumps(services)}\n")
        output.write(f"has_containers={str(bool(services)).lower()}\n")
    print(f"Selected containers: {json.dumps(services)}")


if __name__ == "__main__":
    main()
