"""Cursor afterFileEdit：在 Agent 保存 .env.example 后自动合并到 .env。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _resolve_project_root(data: dict, path: Path) -> Path | None:
    """从 workspace_roots 或从已保存文件路径向上查找含 sync 脚本的项目根。"""
    roots = data.get("workspace_roots") or []
    if roots:
        return Path(roots[0])
    for p in path.parents:
        if (p / "scripts" / "sync_env_from_example.py").is_file():
            return p
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    fp = data.get("file_path") or ""
    path = Path(fp)
    roots = data.get("workspace_roots") or []
    if roots and not path.is_absolute():
        path = Path(roots[0]) / path
    try:
        path = path.resolve()
    except OSError:
        return 0

    if path.name != ".env.example":
        return 0

    root = _resolve_project_root(data, path)
    if root is None:
        print("[sync-env-hook] 无法解析项目根目录（缺少 workspace_roots）", file=sys.stderr)
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
