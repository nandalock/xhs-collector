"""Install a self-contained skill and a non-editable application wheel."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--upgrade", action="store_true", help="Update an existing xhs-collector skill")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    codex = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    destination = (args.destination or codex / "skills" / "xhs-collector").resolve()
    marker = destination / ".installing"
    if destination.exists() and not marker.exists():
        if not args.upgrade:
            parser.error(f"Destination already exists; use --upgrade to update: {destination}")
        skill = destination / "SKILL.md"
        if not skill.is_file() or "name: xhs-collector" not in skill.read_text(encoding="utf-8").splitlines():
            parser.error(f"Destination is not an xhs-collector skill: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    marker.write_text("Installation incomplete; rerun this installer to finish.\n", encoding="utf-8")
    runtime = destination / ".runtime"
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(runtime)
    command = [str(python), "-m", "pip", "install", "--upgrade", str(root)]
    lock = root / "requirements.lock"
    if lock.exists():
        command.extend(["-c", str(lock)])
    subprocess.run(command, check=True)
    source = root / "skills" / "xhs-collector"
    for path in source.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    for name in ("LICENSE", "NOTICE"):
        shutil.copy2(root / name, destination / name)
    subprocess.run([str(python), "-I", "-m", "xhs_collector", "doctor"], check=True)
    marker.unlink()
    print(json.dumps({"status": "installed", "skill": str(destination / "SKILL.md"),
                      "launcher": str(destination / "scripts" / "run.py"), "runtime_python": str(python)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
