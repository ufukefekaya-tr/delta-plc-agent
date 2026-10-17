"""Install the selected checkout components using paths relative to this script.

Run with the destination environment's Python: python tools/install.py
Vendor IDEs are host prerequisites, never downloaded or changed here.
"""
from pathlib import Path
import argparse
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('all', 'dvp'), default='all')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    constraints = root / "requirements-lock.txt"
    subprocess.run([sys.executable, "-m", "pip", "install", "-c", str(constraints), str(root)], check=True)
    if args.profile == 'all':
        backend = root / "backends" / "ax"
        if not (backend / "pyproject.toml").is_file():
            raise SystemExit("Bundled AX backend missing; checkout is incomplete")
        subprocess.run([sys.executable, "-m", "pip", "install", "-c", str(constraints), str(backend)], check=True)
    print(f"Universal PLC installed (profile={args.profile}). Configure host-local workspaces and vendor tools.")


if __name__ == "__main__":
    main()
