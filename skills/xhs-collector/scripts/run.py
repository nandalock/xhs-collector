"""Run the skill's installed wheel, independent of source repositories."""
import json
import os
from pathlib import Path
import subprocess
import sys

skill_dir = Path(__file__).resolve().parents[1]
runtime = skill_dir / ".runtime"
python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
if not python.exists():
    print(json.dumps({"status": "not_installed", "message": "Install via the standalone project's scripts/install_skill.py"}))
    raise SystemExit(2)
env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"
# Do not import similarly named modules from the current working directory.
command = [str(python), "-I", "-m", "xhs_collector", *sys.argv[1:]]
try:
    raise SystemExit(subprocess.call(command, env=env))
except KeyboardInterrupt:
    raise SystemExit(130)
