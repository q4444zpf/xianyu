#!/usr/bin/env python3
"""将 .env.example 同步到 .env：保留 .env 已有键值，补全 example 中新增键。

用法：
  python scripts/sync_env_from_example.py
  python scripts/sync_env_from_example.py --dry-run   # 只打印将要写入的内容
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
DOTENV = ROOT / ".env"

# KEY=VALUE，KEY 为常见环境变量命名
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _parse_key(line: str) -> str | None:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    m = _ASSIGN_RE.match(s)
    return m.group(1) if m else None


def _read_env_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def merge_env(example_lines: list[str], env_lines: list[str]) -> list[str]:
    """按 example 行顺序生成新 .env；已存在的键沿用 .env 中整行；仅存在于 .env 的键附在末尾。"""
    # key -> 在 .env 中最后一次出现的整行
    env_by_key: dict[str, str] = {}
    env_order_keys: list[str] = []
    for line in env_lines:
        k = _parse_key(line)
        if k:
            if k not in env_by_key:
                env_order_keys.append(k)
            env_by_key[k] = line.rstrip("\r")
    example_keys: set[str] = set()
    out: list[str] = []
    for line in example_lines:
        k = _parse_key(line)
        if k:
            example_keys.add(k)
            if k in env_by_key:
                out.append(env_by_key[k])
            else:
                out.append(line.rstrip("\r"))
        else:
            out.append(line.rstrip("\r"))

    extra_lines: list[str] = []
    seen_extra: set[str] = set()
    for line in env_lines:
        k = _parse_key(line)
        if k and k not in example_keys and k not in seen_extra:
            extra_lines.append(line.rstrip("\r"))
            seen_extra.add(k)

    if extra_lines:
        if out and out[-1].strip():
            out.append("")
        out.append("# --- 仅存在于 .env（不在 .env.example 中）---")
        out.extend(extra_lines)

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge .env.example -> .env")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="不写入文件，只打印结果",
    )
    args = parser.parse_args()

    if not EXAMPLE.exists():
        print(f"缺少 {EXAMPLE}", file=sys.stderr)
        return 1

    example_lines = _read_env_lines(EXAMPLE)
    env_lines = _read_env_lines(DOTENV)
    merged = merge_env(example_lines, env_lines)
    text = "\n".join(merged) + "\n"

    if args.dry_run:
        print(text, end="")
        return 0

    DOTENV.write_text(text, encoding="utf-8", newline="\n")
    print(f"已写入 {DOTENV}（共 {len(merged)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
