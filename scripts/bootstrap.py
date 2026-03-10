from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.check_call(cmd)


def repo_to_project_name() -> str:
    repo_dir = Path.cwd().name.lower()
    project = re.sub(r"[^a-z0-9]+", "_", repo_dir).strip("_")
    return project or "config"


def main() -> int:
    project = repo_to_project_name()

    # venv
    if not Path(".venv").exists():
        run([sys.executable, "-m", "venv", ".venv"])

    py = Path(".venv/bin/python")
    if not py.exists():
        raise RuntimeError("Expected Linux venv at .venv/bin/python (Codespaces).")

    run([str(py), "-m", "pip", "install", "-U", "pip"])
    run([str(py), "-m", "pip", "install", "-r", "requirements.txt"])

    # Create Django project if not present
    if not Path("manage.py").exists():
        run([str(py), "-m", "django", "startproject", project, "."])

    settings_py = Path(project) / "settings.py"
    if settings_py.exists():
        txt = settings_py.read_text(encoding="utf-8")

        # Upsert ALLOWED_HOSTS
        if re.search(r"^ALLOWED_HOSTS\s*=", txt, flags=re.M):
            txt = re.sub(r"^ALLOWED_HOSTS\s*=.*$", 'ALLOWED_HOSTS = ["*"]  # dev only', txt, flags=re.M)
        else:
            txt += '\nALLOWED_HOSTS = ["*"]  # dev only\n'

        # Upsert CSRF_TRUSTED_ORIGINS
        csrf_line = 'CSRF_TRUSTED_ORIGINS = ["https://*.githubpreview.dev", "https://*.app.github.dev"]'
        if re.search(r"^CSRF_TRUSTED_ORIGINS\s*=", txt, flags=re.M):
            txt = re.sub(r"^CSRF_TRUSTED_ORIGINS\s*=.*$", csrf_line, txt, flags=re.M)
        else:
            txt += "\n" + csrf_line + "\n"

        settings_py.write_text(txt, encoding="utf-8")

    run([str(py), "manage.py", "migrate"])

    print("")
    print(f"Project module: {project}")
    print("Run server with: make run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
