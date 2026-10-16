#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Regenerate the runtime lock with hashes and a public, stable header."""

import os
from pathlib import Path
import subprocess
import sys


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["CUSTOM_COMPILE_COMMAND"] = "python scripts/compile_requirements.py"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "piptools",
            "compile",
            "--generate-hashes",
            "--no-reuse-hashes",
            "--no-emit-options",
            "--no-emit-index-url",
            "--no-emit-trusted-host",
            "--no-strip-extras",
            "--output-file=deploy/requirements.txt",
            "deploy/requirements.in",
        ],
        cwd=root,
        env=env,
        check=True,
    )
    output = root / "deploy/requirements.txt"
    license_header = "# SPDX-License-Identifier: Apache-2.0\n"
    generated = output.read_text()
    if not generated.startswith(license_header):
        output.write_text(license_header + generated)


if __name__ == "__main__":
    main()
