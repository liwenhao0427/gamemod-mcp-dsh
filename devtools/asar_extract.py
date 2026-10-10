"""从 Electron app.asar 中提取单个文件（只读，用于研究 DSH 插件 API）。

asar 布局：
    0..3     pickle 大小（恒为 4）
    4..7     header 大小
    8..11    header JSON 长度（含 padding）
    12..15   header JSON 字符串长度
    16..     header JSON
    之后 4 字节对齐处即文件数据区，条目里的 offset 相对数据区起点。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load_header(asar: Path) -> tuple[dict, int]:
    with open(asar, "rb") as fh:
        first = fh.read(16)
        json_len = int.from_bytes(first[12:16], "little")
        raw = fh.read(json_len)
    # 数据区起点 = 16 + json_len，按 4 字节对齐
    data_start = 16 + json_len
    if data_start % 4:
        data_start += 4 - (data_start % 4)
    return json.loads(raw.decode("utf-8")), data_start


def find_entry(header: dict, target: str) -> dict | None:
    node = header
    parts = [p for p in target.split("/") if p]
    for part in parts:
        node = node.get("files", {}).get(part)
        if node is None:
            return None
    return node


def extract(asar: Path, target: str) -> bytes:
    header, data_start = load_header(asar)
    node = find_entry(header, target)
    if node is None:
        raise SystemExit(f"asar 中找不到：{target}")
    if "files" in node:
        raise SystemExit(f"{target} 是目录，不是文件")
    offset = int(node["offset"])
    size = int(node["size"])
    with open(asar, "rb") as fh:
        fh.seek(data_start + offset)
        return fh.read(size)


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(
            "用法: python asar_extract.py <app.asar> <内部路径> [输出文件]\n"
            "      python asar_extract.py --list <app.asar> <子串>"
        )
    asar = Path(sys.argv[1])
    if sys.argv[2] == "--list":
        header, _ = load_header(asar)
        needle = sys.argv[3].lower() if len(sys.argv) > 3 else ""
        hits: list[str] = []

        def walk(node: dict, prefix: str) -> None:
            for name, child in node.get("files", {}).items():
                path = f"{prefix}/{name}"
                if "files" in child:
                    walk(child, path)
                elif needle in path.lower():
                    hits.append(path)

        walk(header, "")
        print(f"共 {len(hits)} 个匹配：")
        for h in hits:
            print(" ", h)
        return

    data = extract(asar, sys.argv[2])
    if len(sys.argv) > 3:
        Path(sys.argv[3]).write_bytes(data)
        print(f"已写出 {sys.argv[3]}（{len(data)} 字节）")
    else:
        sys.stdout.write(data.decode("utf-8", errors="replace"))


if __name__ == "__main__":
    main()
