"""Provider login verification, reused by ``gpuhedge login-check``.

Each check does a real authenticated round-trip (not just "is a token file
present"): Modal lists apps, RunPod calls ``get_user``, Cerebrium lists
projects. Everything is timeboxed and degrades to a clear "not logged in"
rather than raising, so the CLI can render a status table.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class AuthStatus:
    provider: str
    logged_in: bool
    identity: str | None = None      # profile / user id / project
    detail: str = ""
    config_path: str | None = None

    @property
    def mark(self) -> str:
        return "OK" if self.logged_in else "NOT LOGGED IN"


def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, (proc.stdout + proc.stderr)
    except FileNotFoundError:
        return 127, "command not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def check_modal(timeout: float = 45.0) -> AuthStatus:
    cfg = pathlib.Path.home() / ".modal.toml"
    if shutil.which("modal") is None:
        return AuthStatus("modal", False, detail="modal CLI not installed")
    code, out = _run(["modal", "profile", "current"], timeout)
    profile = out.strip().splitlines()[-1].strip() if code == 0 and out.strip() else None
    if code != 0:
        return AuthStatus("modal", False, detail=out.strip()[:200], config_path=str(cfg))
    # Live auth: listing apps requires a valid token.
    code2, out2 = _run(["modal", "app", "list"], timeout)
    ok = code2 == 0
    return AuthStatus(
        "modal", ok, identity=profile,
        detail="apps listed" if ok else out2.strip()[:200],
        config_path=str(cfg),
    )


def check_runpod(timeout: float = 45.0) -> AuthStatus:
    cfg = pathlib.Path.home() / ".runpod" / "config.toml"
    try:
        from gpuhedge.backends.runpod_backend import load_runpod_api_key

        api_key = load_runpod_api_key()
    except Exception as exc:  # noqa: BLE001
        return AuthStatus("runpod", False, detail=str(exc)[:200], config_path=str(cfg))
    try:
        import runpod

        runpod.api_key = api_key
        user = runpod.get_user()
        uid = user.get("id") if isinstance(user, dict) else None
        return AuthStatus("runpod", True, identity=uid, detail="get_user ok",
                          config_path=str(cfg))
    except Exception as exc:  # noqa: BLE001
        return AuthStatus("runpod", False, detail=str(exc)[:200], config_path=str(cfg))


def check_cerebrium(timeout: float = 45.0) -> AuthStatus:
    cfg = pathlib.Path.home() / ".cerebrium" / "config.yaml"
    if shutil.which("cerebrium") is None:
        return AuthStatus("cerebrium", False, detail="cerebrium CLI not installed",
                          config_path=str(cfg))
    code, out = _run(["cerebrium", "projects", "list", "--no-color"], timeout)
    if code != 0:
        return AuthStatus("cerebrium", False, detail=out.strip()[:200], config_path=str(cfg))
    # Grab the current project context if available.
    code2, out2 = _run(["cerebrium", "projects", "current", "--no-color"], timeout)
    project = None
    if code2 == 0:
        for line in out2.splitlines():
            if "projectId" in line:
                project = line.split(":", 1)[-1].strip()
    return AuthStatus("cerebrium", True, identity=project, detail="projects listed",
                      config_path=str(cfg))


def check_all(timeout: float = 45.0) -> list[AuthStatus]:
    return [check_modal(timeout), check_runpod(timeout), check_cerebrium(timeout)]
