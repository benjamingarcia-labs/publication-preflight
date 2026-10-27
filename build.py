"""Build a one-click application on the host operating system.

Run with Python and PyInstaller installed in a dedicated build environment.
PyInstaller does not cross-compile Windows and macOS binaries.
"""
from __future__ import annotations

import os
from pathlib import Path
import platform
import subprocess
import sys


ROOT = Path(__file__).resolve().parent


def command(system: str | None = None) -> list[str]:
    system = system or platform.system()
    if system not in {"Windows", "Darwin"}:
        raise RuntimeError("Supported release targets are Windows and macOS; build on the target OS")
    static = ROOT / "preflight" / "static"
    if not static.is_dir():
        raise RuntimeError("Application interface assets are missing")
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--windowed", "--name", "PublicationPreflight",
        "--add-data", str(static) + os.pathsep + "preflight/static",
    ]
    if system == "Windows":
        args.append("--onefile")
    else:
        args.append("--onedir")  # Generates PublicationPreflight.app on macOS.
    return args + [str(ROOT / "launcher.py")]


def main() -> None:
    print("Building Publication Preflight for", platform.system())
    subprocess.run(command(), cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
