"""Offline sanity check for Kubernetes manifests. Needs no cluster."""
import sys
from pathlib import Path

import yaml

K8S_DIR = Path(__file__).resolve().parent.parent / "k8s"
REQUIRED = ("apiVersion", "kind", "metadata")


def check_file(path: Path) -> list[str]:
    try:
        docs = [d for d in yaml.safe_load_all(path.read_text()) if d is not None]
    except yaml.YAMLError as exc:
        return [f"{path.name}: invalid YAML ({type(exc).__name__})"]

    if not docs:
        return [f"{path.name}: file contains no manifests"]

    problems = []
    for doc in docs:
        if not isinstance(doc, dict):
            problems.append(f"{path.name}: a document is not a mapping")
            continue
        for key in REQUIRED:
            if key not in doc:
                problems.append(f"{path.name}: missing '{key}'")
        if "name" not in (doc.get("metadata") or {}):
            problems.append(f"{path.name}: missing metadata.name")
        if doc.get("kind") == "Secret" and (doc.get("data") or doc.get("stringData")):
            problems.append(f"{path.name}: Secret contains values, do not commit credentials")
    return problems


def main() -> int:
    if not K8S_DIR.is_dir():
        print("no k8s/ directory yet, nothing to validate")
        return 0

    files = sorted([*K8S_DIR.rglob("*.yaml"), *K8S_DIR.rglob("*.yml")])
    problems = [p for f in files for p in check_file(f)]
    for problem in problems:
        print("PROBLEM:", problem)
    print(f"checked {len(files)} manifest file(s), {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
    