"""Cursor afterFileEdit：在 Agent 保存 .env.example 后自动合并到 .env。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    fp = data.get("file_path") or ""
    path = Path(fp)
    if path.name != ".env.example":
        return 0

    roots = data.get("workspace_roots") or []
    if roots:
        root = Path(roots[0])
        if not path.is_absolute():
            path = root / path
    else:
        root = path.resolve().parent

    if path.name != ".env.example":
        return 0

    script = root / "scripts" / "sync_env_from_example.py"
    if not script.is_file():
        print(f"[sync-env-hook] 未找到 {script}", file=sys.stderr)
        return 0

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    r = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "sync_env_from_example failed").encode(
            "utf-8", errors="replace"
        )
        sys.stderr.buffer.write(msg)
        if not msg.endswith(b"\n"):
            sys.stderr.buffer.write(b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
