"""Verify published Git blobs against the source manifest; never scan local state."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root)
    manifest = json.loads(git("show", ":PUBLIC_SOURCE_MANIFEST.json"))
    paths = set(git("ls-files", "-z").decode().split("\0")) - {"", "PUBLIC_SOURCE_MANIFEST.json"}
    errors = []
    if set(manifest["files"]) != paths:
        errors.append("manifest file set differs from the index")
    for path in sorted(paths):
        data = git("show", ":" + path)
        if hashlib.sha256(data).hexdigest() != manifest["files"].get(path):
            errors.append("digest mismatch: " + path)
        if path.startswith((".agent-state/", "PLC/", "HMI/")) or path.endswith((".project", ".projectarchive")):
            errors.append("private/native path: " + path)
        if path.endswith((".py", ".json", ".md", ".toml")):
            for marker in (b"C:/Users/casper", b"C:\\Users\\casper", b"GVL_TZ_", b"codex://threads/", b"BEGIN PRIVATE KEY"):
                # This checker contains its own negative signatures, not private data.
                if marker in data and path != "tools/check_public_manifest.py":
                    errors.append("private marker: " + path)
    for error in errors:
        print(error)
    print("PUBLIC_MANIFEST_" + ("FAIL" if errors else "PASS") + " files=" + str(len(paths)))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
