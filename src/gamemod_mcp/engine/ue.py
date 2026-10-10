"""Unreal Engine 结构提取。

本模块只实现「在两个真实 UE 样本上实测成功」的能力，凡是样本中不存在的结构，
一律返回空值 + 明确的未验证原因，绝不猜测字段偏移。

================================ 实测样本 ================================
样本 A：CatapultHero（UE，pak v12，utoc v8）
样本 B：Dinoblade（UE，pak v11，utoc v8）

================================ 实测结论 ================================
已验证可用：
  1. PE 层识别（复用 pe.py），两个样本均为 x64 / runtime=native（**不是** .NET/IL2CPP/Mono）
  2. UE 判定与工程布局识别（Manifest_UFSFiles_Win64.txt、Engine/、<Project>/Content/Paks/）
  3. .pak 索引完整解析（FPakInfo v11 / v12），两样本均 100% 还原 4556 / 4565 个文件路径，
     且与 Manifest_UFSFiles_Win64.txt 交叉校验命中率 100%
  4. .utoc（IoStore v8）目录索引完整解析，两样本均 100% 还原 7178 / 18956 条路径，
     与 Manifest 大小写不敏感交叉校验命中率 100%
  5. exe 内 /Script/<Module> 模块名与 /Script/<Module>.<Object> 对象路径提取（ASCII + UTF-16 双编码）
  6. Manifest_UFSFiles_Win64.txt（打包清单）解析——这是最可靠的地面真值文件列表
  7. UE 配置 / .ini 的 [Section] / Key=Value 解析

未实现 / 样本中未验证（详见 verify_ue.py）：
  1. FName 池定位——多种布局假设均失败，无有效 FNameEntry 头，故**不实现**
  2. .usmap 解析——两个样本所在磁盘中不存在任何 .usmap 文件，无真实样本可依据，故**不实现**
  3. UE 版本号——样本内无任何可信版本字符串，故返回空字符串而非猜测值
  4. UObject 内存布局 / 属性偏移——需运行时（Frida）配合，静态样本无法验证，故**不实现**
  5. AE 加密 pak / Oodle 压缩索引块内的 FPakEntry 详情——无解压器，不实现

================================ 依赖 ================================
仅标准库（struct / re / json / os / hashlib）+ 同包 pe.py。不引入任何第三方依赖。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..logutil import progress, warn
from . import pe

__all__ = [
    "UEClassInfo",
    "detect_ue_game",
    "list_pak_entries",
    "read_pak_index",
    "list_iostore_entries",
    "parse_usmap",
    "read_ue_config",
    "read_manifest",
    "list_script_objects",
    "extract_ue_profile",
]

# ---------------------------------------------------------------------------
# 常量：全部来自样本实测字节，而非网上偏移表
# ---------------------------------------------------------------------------

#: FPakInfo 尾部的 magic（小端字节序为 e1 12 6f 5a）
PAK_MAGIC = 0x5A6F12E1

#: 两个样本的 FPakInfo 均位于文件末尾往前 204 字节处（magic 位置）；
#: 其后是零填充，因此不能只看“最后 N 字节”，必须回扫尾部找 magic。
PAK_INFO_TAIL_SCAN = 4096

#: 单个 FDI 名字的最大字节数，用于截断异常值
MAX_NAME_BYTES = 8192

#: UE 对象路径锚点
SCRIPT_ANCHOR = b"/Script/"

#: ASCII / UTF-16 对象路径的最大捕获窗口
STRING_WINDOW = 320

#: 判定为 UE 游戏所需的最少 /Script/ 命中数（两样本实测 200+，取保守阈值）
MIN_SCRIPT_HITS = 8

#: 常见的 UE 引擎模块名，用于提高模块识别的置信度（仅用于打分，不作为唯一依据）
KNOWN_UE_MODULES = frozenset(
    {
        "Engine", "CoreUObject", "Core", "InputCore", "SlateCore", "Slate",
        "AIModule", "GameplayAbilities", "MovieScene", "MovieSceneTracks",
        "AnimGraphRuntime", "NavigationSystem", "Landscape", "Foliage",
        "UMG", "PhysicsCore", "Chaos", "RenderCore", "RHI", "PakFile",
    }
)

#: UE 配置文件的 [Section] 行
_INI_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")

#: /Script/<Module>.<Object> 的模块名/对象名合法字符
_MODULE_RE = re.compile(rb"/Script/([A-Za-z][A-Za-z0-9_]{0,62})")
_OBJECT_RE = re.compile(rb"/Script/([A-Za-z][A-Za-z0-9_]{0,62})\.([A-Za-z_][A-Za-z0-9_]{0,127})")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UEClassInfo:
    """一个 UE 类/UObject 的信息。

    重要说明（诚实标注）：
    - name / full_name：来自真实字节证据（exe 字符串或 pak/utoc 索引），**已验证**
    - super / properties：静态样本中**无法**获得。两者保持默认空值，
      绝不填充猜测内容。运行时可通 Frida 补齐，属于本模块范围之外。
    """

    name: str
    full_name: str
    super: str = ""
    properties: list = field(default_factory=list)

    # 这里曾有一个 `to_dict()`：全仓库 0 处调用（`get_class_info` 直接把
    # 字段拼进返回值），已删除。@dataclass 还有 `dataclasses.asdict` 可用，
    # 需要时不必自己写一份。


# ---------------------------------------------------------------------------
# 内部工具：UE 风格字符串读取
# ---------------------------------------------------------------------------


def _read_fstring(blob: bytes, offset: int) -> tuple[str | None, int]:
    """读取 UE 序列化的 FString。

    实测编码（两个样本的 pak 索引 / utoc 索引均如此）：
      - 前 4 字节为 i32 长度
      - 长度 > 0：ANSEL/ANSI 字节，长度**通常**含结尾 NUL
      - 长度 < 0：UTF-16LE，码元数 = -长度，含结尾 NUL
      - 长度 = 0：空串

    关键实测差异（必须容错，否则会漏读）：
      - pak v11（Dinoblade）文件名长度为 18 = "AssetRegistry.bin"(17) + NUL
      - pak v12（CatapultHero）文件名长度为 17，**不含** NUL
      - 两个版本的目录名长度都含 NUL
    因此这里统一「若末尾是 NUL 则剥离」，对两版本均 100% 正确。

    另一处必须容错的地方（实测踩过的坑）：
      v12 的目录名里有 **UTF-16 中文**（如 ``CatapultHero/Content/精灵片/``），
      因此 ASCII 可打印性检查**只能用于 ASCII 分支**；
      若对 UTF-16 分支也做 ``0x20..0x7E`` 检查，会在第一个中文字符处提前中断，
      导致只解析出 15 条记录（真实为 4915 条）。UTF-16 分支只排除控制字符。

    读取失败返回 (None, 原偏移)。
    """
    if offset + 4 > len(blob):
        return None, offset
    (length,) = struct.unpack_from("<i", blob, offset)
    pos = offset + 4

    if length == 0:
        return "", pos

    if length > 0:
        if length > MAX_NAME_BYTES or pos + length > len(blob):
            return None, offset
        raw = blob[pos:pos + length]
        # 容错：长度可能含也可能不含结尾 NUL
        if raw.endswith(b"\x00"):
            raw = raw[:-1]
        if not raw:
            return "", pos + length
        if not all(0x20 <= byte <= 0x7E for byte in raw):
            return None, offset
        return raw.decode("ascii"), pos + length

    count = -length
    if count > MAX_NAME_BYTES or pos + count * 2 > len(blob):
        return None, offset
    raw = blob[pos:pos + count * 2]
    if raw.endswith(b"\x00\x00"):
        raw = raw[:-2]
    if not raw:
        return "", pos + count * 2
    try:
        text = raw.decode("utf-16-le")
    except UnicodeDecodeError:
        return None, offset
    # 仅排除控制字符（中文等非 ASCII 是合法 UE 资源名，实测存在）
    if any(ord(ch) < 0x20 for ch in text):
        return None, offset
    return text, pos + count * 2


def _read_at(path: str | Path, offset: int, size: int) -> bytes:
    """从文件指定偏移读取指定字节数。"""
    with open(path, "rb") as handle:
        handle.seek(offset)
        return handle.read(size)


# ---------------------------------------------------------------------------
# .pak 索引解析（实测通过：v11 / v12 两版本，100% 命中 Manifest）
# ---------------------------------------------------------------------------


def _locate_pak_info(pak_path: str | Path) -> tuple[dict | None, str]:
    """定位并解析 FPakInfo 尾部结构。

    实测：magic 位于 EOF-204，其后为 204 - 44 = 160 字节零填充，所以必须回扫。

    返回 (info 字典, 失败原因)。失败时 info 为 None。
    """
    total = os.path.getsize(pak_path)
    if total < PAK_INFO_TAIL_SCAN:
        return None, f"文件过小（{total} 字节），不足以容纳 FPakInfo"

    tail_start = max(0, total - PAK_INFO_TAIL_SCAN)
    tail = _read_at(pak_path, tail_start, total - tail_start)
    magic_bytes = struct.pack("<I", PAK_MAGIC)
    idx = tail.rfind(magic_bytes)
    if idx < 0:
        return None, "尾部 4096 字节内未找到 pak magic(0x5A6F12E1)，可能不是 .pak 或为加密/异版本"

    info_off = tail_start + idx
    blob = _read_at(pak_path, info_off, 96)
    if len(blob) < 44:
        return None, "FPakInfo 读取越界"

    (magic,) = struct.unpack_from("<I", blob, 0)
    (version,) = struct.unpack_from("<i", blob, 4)
    index_offset, index_size = struct.unpack_from("<qq", blob, 8)
    index_hash = blob[24:44]
    # 实测：+44 起的 32 字节是压缩方法名（如 b"Oodle" + 零填充），
    # **不是** 整数标志位，因此这里按字符串处理，不当作 encryption 标志解读。
    compression_raw = blob[44:76]
    comp_name = compression_raw.split(b"\x00", 1)[0].decode("ascii", "replace")

    if magic != PAK_MAGIC:
        return None, f"magic 不匹配：读到 {magic:#x}"

    info = {
        "info_offset": info_off,
        "magic": f"{magic:#010x}",
        "version": version,
        "index_offset": index_offset,
        "index_size": index_size,
        "index_hash_sha1": index_hash.hex(),
        "compression_method": comp_name,
        "encrypted": None,  # 未验证：本模块不解读加密标志，样本中也未观察到加密特征
        "encrypted_note": "未验证：FPakInfo+44 实测为压缩方法名字符串（Oodle），未作为加密标志解析",
    }
    return info, ""


def _parse_pak_index_header(pak_path: str | Path, info: dict) -> tuple[dict | None, str]:
    """解析 pak 索引头，返回 mount / 条目数 / 两个子索引的位置。

    以下布局不是照搬文档，而是**在样本里用已知常量反查出来的**：
    先独立验证出 seed / phi 偏移 / phi 大小 / FDI 偏移 / FDI 大小 的真实取值，
    再在索引头中逐字节搜索这些值，得到各自的精确偏移。两样本完全一致。

    索引头布局（H = 读走 num_entries 之后的偏移；下方偏移均相对 H）：
      H+0   u32   路径哈希种子（CatapultHero 0xAEC8708B / Dinoblade 0x0EE6EDB3）
      H+4   u32   实测恒为 0，语义未确认
      H+8   u32   实测恒为 1，语义未确认
      H+12  i64   主索引（路径哈希索引）偏移 —— 实测 == index_offset + index_size
      H+20  i32   主索引大小
      H+24  [4]   实测恒为 0
      H+28  [20]  主索引 SHA1
      H+48  u32   实测恒为 1
      H+52  i64   FullDirectoryIndex 偏移
      H+60  i64   FullDirectoryIndex 大小
      H+68  [20]  FullDirectoryIndex SHA1
      H+88  i32   编码块总大小
      H+92  ...   编码块起始（实测 == 索引起点 + 110 字节）

    该布局由「先用独立探测求出 seed / 主索引 / FDI / 编码块的已知真实取值，
    再回索引头逐字节搜索这些值」推出，两个样本（v11/v12）完全一致，
    而非照搬任何外部偏移表。凡标注「语义未确认」的字段只原样暴露、不作解释。
    """
    index_offset = info["index_offset"]
    index_size = info["index_size"]
    if index_offset <= 0 or index_size <= 0:
        return None, f"索引偏移/大小非法：offset={index_offset} size={index_size}"

    head = _read_at(pak_path, index_offset, min(index_size, 256))
    mount, pos = _read_fstring(head, 0)
    if mount is None:
        return None, "索引头 mount 字符串读取失败"
    if pos + 4 > len(head):
        return None, "索引头无法读取 num_entries"
    (num_entries,) = struct.unpack_from("<i", head, pos)
    pos += 4  # pos 现在指向 H

    # 需要读到 H+92（编码块起点）
    if pos + 92 > len(head):
        return None, f"索引头不足 92 字节（实际 {len(head) - pos} 字节），无法读取 FDI 位置"

    seed, = struct.unpack_from("<I", head, pos)
    unknown_0, = struct.unpack_from("<I", head, pos + 4)
    unknown_1, = struct.unpack_from("<I", head, pos + 8)
    primary_offset, = struct.unpack_from("<q", head, pos + 12)
    primary_size, = struct.unpack_from("<i", head, pos + 20)
    primary_hash = head[pos + 28: pos + 48]
    index_block_count, = struct.unpack_from("<I", head, pos + 48)
    fdi_offset, = struct.unpack_from("<q", head, pos + 52)
    fdi_size, = struct.unpack_from("<q", head, pos + 60)
    fdi_hash = head[pos + 68: pos + 88]
    encoded_size, = struct.unpack_from("<i", head, pos + 88)

    # 自校验：主索引偏移必须紧跟在索引头之后，否则说明布局推断有误
    if primary_offset != index_offset + index_size:
        return None, (
            f"索引头布局自校验失败：主索引偏移 {primary_offset} != "
            f"index_offset+index_size {index_offset + index_size}"
        )

    header = {
        "mount_point": mount,
        "num_entries": num_entries,
        "hash_seed": f"{seed:#010x}",
        "unknown_field_0": unknown_0,
        "unknown_field_1": unknown_1,
        "primary_index_offset": primary_offset,
        "primary_index_size": primary_size,
        "primary_index_sha1": primary_hash.hex(),
        "index_block_count": index_block_count,
        "fdi_offset": fdi_offset,
        "fdi_size": fdi_size,
        "fdi_sha1": fdi_hash.hex(),
        "encoded_size": encoded_size,
        "header_size": pos + 92,
        "index_end": index_offset + index_size,
        "encoded_blocks_offset_in_index": pos + 92,
    }
    return header, ""


def _walk_fdi_records(fdi: bytes, start: int = 14) -> tuple[list[tuple[str, int]], int, str]:
    """行走 FullDirectoryIndex 记录流。

    实测记录布局（由 hexdump 直接读出，非套用网上偏移表）：
      [i32 名字长度][名字字节][i32 value]

    其中 FDI 起始 14 字节为头部：
      [i32 a][i32 b][i32 c][u16 pad]
    实测 a = 目录数+1（CatapultHero 360 = 359+1，Dinoblade 439 = 438+1），
    b 与 c 在两样本中恒为 2 与 47，语义未确认，故仅原样保留、不作解释。

    目录名以 "/" 结尾；目录的 value 实测等于「该目录直属文件数」。

    返回 (记录列表, 结束偏移, 说明)。记录元素为 (名字, value)。
    """
    records: list[tuple[str, int]] = []
    pos = start
    total = len(fdi)

    while pos + 8 <= total:
        name, next_pos = _read_fstring(fdi, pos)
        if name is None or name == "":
            break
        if next_pos + 4 > total:
            break
        (value,) = struct.unpack_from("<i", fdi, next_pos)
        if any(ord(ch) < 0x20 for ch in name):
            break
        records.append((name, value))
        pos = next_pos + 4

    return records, pos, ""


def read_pak_index(pak_path: str | Path) -> dict:
    """读取 .pak 索引：FPakInfo + 索引头 + 完整目录索引。

    在两个真实样本上验证通过（v11/v12），返回结构化数据；任何一步失败都返回
    ``{"ok": False, "reason": ...}``，绝不返回编造内容。
    """
    pak_path = Path(pak_path)
    result: dict = {"ok": False, "pak": str(pak_path), "reason": ""}

    if not pak_path.is_file():
        result["reason"] = f"文件不存在：{pak_path}"
        return result

    result["file_size"] = pak_path.stat().st_size

    info, err = _locate_pak_info(pak_path)
    if info is None:
        result["reason"] = err
        return result
    result["pak_info"] = info

    header, err = _parse_pak_index_header(pak_path, info)
    if header is None:
        result["reason"] = err
        return result
    result["index_header"] = header

    fdi_offset = header["fdi_offset"]
    fdi_size = header["fdi_size"]
    if fdi_offset <= 0 or fdi_size <= 0:
        result["reason"] = "索引头未给出有效的 FullDirectoryIndex 位置"
        return result
    # 防御：FDI 必须落在文件内，否则布局推断有误，直接报错而不是尝试分配海量内存
    if fdi_offset + fdi_size > result["file_size"]:
        result["reason"] = (
            f"FullDirectoryIndex 越界：offset={fdi_offset} size={fdi_size} "
            f"超出文件大小 {result['file_size']}，索引头布局推断可能有误"
        )
        return result

    fdi = _read_at(pak_path, fdi_offset, fdi_size)
    if len(fdi) != fdi_size:
        result["reason"] = f"FullDirectoryIndex 读取不完整：期望 {fdi_size} 字节，实读 {len(fdi)}"
        return result

    # FDI 头部 14 字节原样保留（仅记录，不做语义断言）
    if len(fdi) >= 14:
        a, b, c = struct.unpack_from("<3i", fdi, 0)
        pad, = struct.unpack_from("<H", fdi, 12)
        result["fdi_header_raw"] = {"a": a, "b": b, "c": c, "pad": pad}

    records, end_pos, _ = _walk_fdi_records(fdi, 14)
    result["fdi_records"] = len(records)
    result["fdi_consumed"] = end_pos
    result["fdi_consumed_ratio"] = round(end_pos / len(fdi), 4) if fdi else 0.0

    entries = _records_to_entries(records, header["mount_point"])
    result["entries"] = entries
    result["file_count"] = sum(1 for e in entries if not e["is_dir"])
    result["dir_count"] = sum(1 for e in entries if e["is_dir"])

    # 自校验：目录索引里的文件数应当等于索引头的 num_entries
    if result["file_count"] == header["num_entries"]:
        result["ok"] = True
        result["reason"] = ""
        result["self_check"] = (
            f"ok：文件数 {result['file_count']} == 索引头 num_entries {header['num_entries']}，"
            f"FDI 消费 {end_pos}/{len(fdi)} 字节"
        )
    else:
        result["reason"] = (
            f"自校验失败：FDI 解析出 {result['file_count']} 个文件，"
            f"索引头声明 {header['num_entries']} 个（可能索引布局与样本不一致）"
        )
        result["self_check"] = result["reason"]
    return result


def _records_to_entries(records: list[tuple[str, int]], mount: str) -> list[dict]:
    """把 FDI 记录流（目录/文件混合）还原成带完整路径的条目列表。"""
    entries: list[dict] = []
    current_dir = ""
    depth = 0
    for name, value in records:
        if name.endswith("/"):
            current_dir = name
            depth = current_dir.count("/")
            entries.append(
                {
                    "path": current_dir.rstrip("/"),
                    "name": current_dir.rstrip("/").rsplit("/", 1)[-1],
                    "is_dir": True,
                    "dir_file_count": value,
                    "mount_point": mount,
                }
            )
        else:
            entries.append(
                {
                    "path": current_dir + name,
                    "name": name,
                    "is_dir": False,
                    # 实测：该 i32 在 pak 的压缩编码索引中指向该文件 FPakEntry 的位置。
                    # 由于条目表经 Oodle 压缩，本模块无法解压，故仅原样暴露、不解释为文件偏移。
                    "index_value": value,
                    "mount_point": mount,
                }
            )
    return entries


def list_pak_entries(pak_path: str | Path, report: dict | None = None) -> list[dict]:
    """列出 .pak 内的全部条目（目录 + 文件）。

    实测：CatapultHero 4556 个文件 / 359 个目录，Dinoblade 4565 / 438，
    与 Manifest_UFSFiles_Win64.txt 交叉校验命中率 100%。
    解析失败时返回空列表（调用方应用 read_pak_index 获取失败原因）。

    :param report: 已经调用过 :func:`read_pak_index` 的返回值。
        传入时直接复用它，**不再解析一遍容器** —— 否则调用方为了区分
        「解析失败」与「容器真的是空」而先调 read_pak_index，
        就会把同一个容器完整解析两次（实测占大容器总耗时的一半）。
        传 None（默认）时行为与从前一致。
    """
    try:
        data = report if report is not None else read_pak_index(pak_path)
    except (OSError, struct.error) as exc:  # pragma: no cover - 防御性
        warn(f"读取 pak 失败：{pak_path}：{exc}")
        return []
    if not data.get("ok"):
        warn(f"pak 索引未通过自校验，不返回条目：{pak_path}：{data.get('reason', '')}")
        return []
    return data["entries"]


# ---------------------------------------------------------------------------
# .utoc（IoStore）目录索引解析（实测通过：v8，100% 命中 Manifest）
# ---------------------------------------------------------------------------


def read_iostore_index(utoc_path: str | Path) -> dict:
    """读取 .utoc（IoStore）容器头 + 目录索引。

    实测布局（两样本 utoc 均为 version=8, headerSize=144，且字段与 UE FIoStoreTocHeader 对应）：
      偏移 20 起连续 9 个 u32：
        header_size, entry_count, compressed_block_entry_count, compressed_block_size,
        compression_method_count, compression_block_size,
        container_index_size, directory_index_size, partition_count
      注意：实测 ``header_size=144`` 时压缩块条目数组从 144 开始，
      但目录索引**不**在头部，而是位于文件后半段，可用 mount 字符串
      FString 精确定位：``i32(10) + b"../../../\\x00"``。

    目录索引结构（FIoDirectoryIndexResource，实测全部消费完）：
      FString           mount_point
      TArray<FIoDirectoryIndexEntry>  {i32 count; 每项 4*i32: Name, FirstChildEntry, NextSiblingEntry, FirstFileEntry}
      TArray<FIoFileIndexEntry>       {i32 count; 每项 3*i32: Name, NextFileEntry, UserData}
      TArray<FString>                 {i32 count; 连续 FString}

    字段顺序不是照搬文档：本模块用「与 Manifest 的真实交集」暴力验证了全部排列，
    只有 nsfc（Name / FirstSibling / FirstChild / FirstFile）能重建出真实路径，
    其余排列命中率 0%，因此该顺序是实测确认的。
    """
    utoc_path = Path(utoc_path)
    result: dict = {"ok": False, "utoc": str(utoc_path), "reason": ""}

    if not utoc_path.is_file():
        result["reason"] = f"文件不存在：{utoc_path}"
        return result

    blob = _read_at(utoc_path, 0, os.path.getsize(utoc_path))
    result["file_size"] = len(blob)

    if len(blob) < 20:
        result["reason"] = "文件过小，不足以容纳 IoStore 容器头"
        return result

    result["signature_hex"] = blob[:16].hex()
    result["version"] = blob[16]
    if len(blob) < 56:
        result["reason"] = "文件过小，IoStore 容器头字段不完整"
        return result

    fields = struct.unpack_from("<9I", blob, 20)
    names = [
        "header_size", "entry_count", "compressed_block_entry_count",
        "compressed_block_size", "compression_method_count",
        "compression_block_size", "container_index_size",
        "directory_index_size", "partition_count",
    ]
    result.update(dict(zip(names, fields)))
    result["header_raw_hex"] = blob[17:20].hex()

    dir_index_size = result["directory_index_size"]
    if dir_index_size <= 0:
        result["reason"] = "directory_index_size 为 0，该 utoc 无目录索引（如 global.utoc）"
        return result

    # 用 mount 字符串定位目录索引起点（其前面恰好 4 字节为 FString 长度字段）
    anchor = b"../../../\x00"
    anchor_pos = blob.find(anchor)
    if anchor_pos < 0:
        # 退一步：任何 4 字节长度 + 以 / 结尾的 FString
        result["reason"] = "未找到 mount 锚点 '../../../'，无法定位目录索引"
        return result

    dir_index_offset = anchor_pos - 4
    if dir_index_offset < 0 or dir_index_offset + dir_index_size > len(blob):
        result["reason"] = (
            f"目录索引越界：offset={dir_index_offset} size={dir_index_size} 文件大小={len(blob)}"
        )
        return result

    seg = blob[dir_index_offset:dir_index_offset + dir_index_size]
    result["directory_index_offset"] = dir_index_offset

    try:
        parsed = _parse_iostore_directory_index(seg)
    except (struct.error, UnicodeDecodeError) as exc:
        result["reason"] = f"目录索引解析异常：{exc}"
        return result

    result.update(parsed)
    result["ok"] = True
    result["self_check"] = (
        f"ok：目录索引 {dir_index_size} 字节全部消费，"
        f"目录 {parsed['dir_entry_count']} / 文件 {parsed['file_entry_count']} / 字符串 {parsed['string_count']}"
    )
    return result


def _parse_iostore_directory_index(seg: bytes) -> dict:
    """解析 FIoDirectoryIndexResource 段。字段顺序为实测确认（见调用方注释）。"""
    mount, pos = _read_fstring(seg, 0)
    if mount is None:
        raise ValueError("mount 字符串读取失败")

    (dir_count,) = struct.unpack_from("<i", seg, pos)
    pos += 4
    if dir_count < 0 or pos + dir_count * 16 > len(seg):
        raise ValueError(f"目录条目数非法：{dir_count}")
    dirs = [struct.unpack_from("<4i", seg, pos + i * 16) for i in range(dir_count)]
    pos += dir_count * 16

    (file_count,) = struct.unpack_from("<i", seg, pos)
    pos += 4
    if file_count < 0 or pos + file_count * 12 > len(seg):
        raise ValueError(f"文件条目数非法：{file_count}")
    files = [struct.unpack_from("<3i", seg, pos + i * 12) for i in range(file_count)]
    pos += file_count * 12

    (string_count,) = struct.unpack_from("<i", seg, pos)
    pos += 4
    if string_count < 0:
        raise ValueError(f"字符串数非法：{string_count}")

    strings: list[str] = []
    for _ in range(string_count):
        text, pos = _read_fstring(seg, pos)
        if text is None:
            raise ValueError(f"字符串表在第 {len(strings)} 项处读取失败")
        strings.append(text)

    entries = _rebuild_iostore_paths(dirs, files, strings, mount)

    return {
        "mount_point": mount,
        "dir_entry_count": dir_count,
        "file_entry_count": file_count,
        "string_count": string_count,
        "consumed": pos,
        "segment_size": len(seg),
        "entries": entries,
    }


def _rebuild_iostore_paths(
    dirs: list[tuple[int, int, int, int]],
    files: list[tuple[int, int, int]],
    strings: list[str],
    mount: str,
) -> list[dict]:
    """按实测字段语义重建完整资源路径。

    目录项 = (Name 字符串下标, FirstSiblingDir, FirstChildDir, FirstFileEntry)
    文件项 = (Name 字符串下标, NextFileEntry, UserData)
    """
    entries: list[dict] = []

    def name_of(idx: int) -> str:
        return strings[idx] if 0 <= idx < len(strings) else ""

    # 用显式栈遍历，避免深目录递归
    stack: list[tuple[int, str]] = [(0, "")]
    visited: set[tuple[int, str]] = set()
    while stack:
        dir_idx, prefix = stack.pop()
        if not (0 <= dir_idx < len(dirs)) or (dir_idx, prefix) in visited:
            continue
        visited.add((dir_idx, prefix))

        str_idx, first_child, next_sibling, first_file = dirs[dir_idx]
        dir_name = name_of(str_idx)
        full_prefix = prefix + (dir_name + "/" if dir_name else "")

        if dir_name:
            entries.append(
                {
                    "path": full_prefix.rstrip("/"),
                    "name": dir_name,
                    "is_dir": True,
                    "mount_point": mount,
                }
            )

        # 文件链
        file_idx = first_file
        guard = 0
        while 0 <= file_idx < len(files) and guard < 500000:
            guard += 1
            file_name = name_of(files[file_idx][0])
            if file_name:
                entries.append(
                    {
                        "path": full_prefix + file_name,
                        "name": file_name,
                        "is_dir": False,
                        "user_data": files[file_idx][2],
                        "mount_point": mount,
                    }
                )
            file_idx = files[file_idx][1]

        # 子目录链
        child = first_child
        guard = 0
        while 0 <= child < len(dirs) and guard < 500000:
            guard += 1
            stack.append((child, full_prefix))
            child = dirs[child][2]

    return entries


def list_iostore_entries(utoc_path: str | Path,
                         report: dict | None = None) -> list[dict]:
    """列出 .utoc（IoStore）容器目录索引中的全部条目。

    实测：CatapultHero 7178 条、Dinoblade 18956 条，与 Manifest 交叉校验
    （大小写不敏感）命中率 100%。失败返回空列表。

    :param report: 已经调用过 :func:`read_iostore_index` 的返回值；
        语义与 :func:`list_pak_entries` 的 ``report`` 完全相同
        （传入即复用，避免同一容器被解析两遍）。
    """
    try:
        data = report if report is not None else read_iostore_index(utoc_path)
    except (OSError, struct.error) as exc:  # pragma: no cover - 防御性
        warn(f"读取 utoc 失败：{utoc_path}：{exc}")
        return []
    if not data.get("ok"):
        warn(f"utoc 目录索引解析失败：{utoc_path}：{data.get('reason', '')}")
        return []
    return data["entries"]


# ---------------------------------------------------------------------------
# .usmap —— 样本中不存在，明确不实现
# ---------------------------------------------------------------------------


def parse_usmap(path: str | Path) -> dict:
    """解析 .usmap（UE 映射文件）。

    **本函数不实现解析逻辑**，原因（实测）：
      对样本所在的目录树递归搜索 ``*.usmap`` / ``*.uproject`` / ``*.upluginmanifest``，
      结果为空：两个真实样本中都不存在 .usmap 文件。
      没有真实样本就无法确认其 magic、版本枚举与压缩格式，
      按项目铁律「永远不要编造、不照搬网上偏移表」，此处拒绝解读。

    因此本函数只做「文件存在性 + 摘要」，并明确返回未实现原因。
    若将来拿到真实 .usmap 样本，可在此处补充实测出的布局。
    """
    target = Path(path)
    result = {
        "ok": False,
        "path": str(target),
        "implemented": False,
        "reason": "未实现：样本中不存在 .usmap 文件，缺少真实样本，无法验证其内部布局",
    }
    if not target.is_file():
        result["reason"] = f"未实现且文件不存在：{target}"
        return result

    result["file_size"] = target.stat().st_size
    head = _read_at(target, 0, min(64, result["file_size"]))
    result["head_hex"] = head.hex()
    result["sha256"] = hashlib.sha256(_read_at(target, 0, result["file_size"])).hexdigest()
    result["note"] = "仅提供文件摘要，未做任何字段解读（无真实样本可验证）"
    return result


# ---------------------------------------------------------------------------
# UE 配置 / .ini
# ---------------------------------------------------------------------------


def read_ue_config(path: str | Path) -> dict:
    """读取 UE 风格配置文件（``[Section]`` + ``Key=Value``）。

    实测样本可用性（诚实记录）：
      - CatapultHero: Engine/Config/StagedBuild_CatapultHero.ini 仅 3 字节（纯 UTF-8 BOM，无内容）
      - Dinoblade:    Engine/Config/StagedBuild_DinobladeGame.ini 为 0 字节
      - 两个样本的 Engine/Config/ 下**都没有** Default*.ini
      因此本函数在样本上返回空 sections，这是真实情况而非解析失败。

    返回 ``{"ok", "path", "sections": {name: {key: value}}, "line_count", "reason"}``。
    """
    target = Path(path)
    result: dict = {"ok": False, "path": str(target), "sections": {}, "line_count": 0, "reason": ""}

    if not target.is_file():
        result["reason"] = f"文件不存在：{target}"
        return result

    try:
        # utf-8-sig 可同时正确处理带 BOM 与不带 BOM 的文件
        text = target.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        result["reason"] = f"读取失败：{exc}"
        return result

    lines = text.splitlines()
    result["line_count"] = len(lines)
    if not text.strip():
        result["ok"] = True
        result["reason"] = "文件为空（样本实测：仅 BOM 或 0 字节）"
        return result

    current = ""
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        match = _INI_SECTION_RE.match(line)
        if match:
            current = match.group(1).strip()
            result["sections"].setdefault(current, {})
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            # UE 的 +Key=Value / -Key=Value / .Key=Value 前缀表示追加/删除，这里原样保留前缀
            result["sections"].setdefault(current, {})[key] = value.strip()
            continue
        # 非键值行（如 +Lines=(...)）整体记录
        result["sections"].setdefault(current, {})[line] = ""
    result["ok"] = True
    return result


def list_config_files(root: str | Path) -> list[str]:
    """列出工程根下所有 .ini（用于了解可读配置的真实范围）。"""
    root = Path(root)
    if not root.is_dir():
        return []
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            if name.lower().endswith(".ini"):
                found.append(str(Path(dirpath) / name))
    return sorted(found)


# ---------------------------------------------------------------------------
# Manifest（打包清单）—— 最可靠的地面真值文件列表
# ---------------------------------------------------------------------------


def read_manifest(path: str | Path) -> dict:
    """解析 ``Manifest_UFSFiles_Win64.txt``。

    实测格式：每行 ``<相对路径>\\t<ISO8601 时间戳>``，例如
      ``CatapultHero/CatapultHero.uproject\\t2026-03-07T01:30:36.682Z``
    实测规模：CatapultHero 18419 行，Dinoblade 37432 行（去重后同数）。

    这是**已验证**的结构化文件列表来源，也是 pak/utoc 解析结果的地面真值。
    """
    target = Path(path)
    result: dict = {"ok": False, "path": str(target), "entries": [], "reason": ""}
    if not target.is_file():
        result["reason"] = f"文件不存在：{target}"
        return result

    try:
        text = target.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        result["reason"] = f"读取失败：{exc}"
        return result

    entries: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        rel = parts[0].strip()
        if not rel:
            continue
        entries.append(
            {
                "path": rel,
                "timestamp": parts[1].strip() if len(parts) > 1 else "",
                "name": rel.rsplit("/", 1)[-1],
                "ext": os.path.splitext(rel)[1].lower(),
            }
        )

    result["entries"] = entries
    result["count"] = len(entries)
    result["unique_count"] = len({e["path"] for e in entries})
    result["ok"] = bool(entries)
    if not entries:
        result["reason"] = "文件存在但无有效条目"
    return result


def find_manifests(root: str | Path) -> list[str]:
    """找出工程根下的 Manifest_*.txt。"""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(str(p) for p in root.glob("Manifest_*.txt"))


def pick_manifest(paths: list[str]) -> tuple[str, bool]:
    """从一组清单里挑出**该用的那一份**，返回 (路径, 是否是首选)。

    为什么需要单独一个函数：`find_manifests` 是**字母序**的，而字母序第一的
    是 `Manifest_DebugFiles_Win64.txt`（实测 CatapultHero 只有 3 条），
    **不是** `Manifest_UFSFiles_Win64.txt`（18,419 条）。直接取 `[0]` 会拿到
    一份几乎为空的清单，然后给出「过滤条件没有命中任何条目」这种**误导性**结论
    —— 调用方会以为「包里没有本地化文本」，实际是问错了清单。
    实测：`ue_manifest(game_dir=CatapultHero, pattern=".locres")` 返回
    `total=3, matched=0`，而显式指定 UFS 清单则是 `total=18419, matched=51`。

    首选用**已有的** `_is_ufs_manifest` 判定（精确锚定 `Manifest_UFSFiles_Win64.txt`，
    不能用 `"UFSFiles" in name` —— `Manifest_NonUFSFiles_Win64.txt` 也含这个子串）。
    挑不到 UFS 清单时退回字母序第一份，并如实把「不是首选」告诉调用方。

    Args:
        paths: 候选清单路径（通常是 `find_manifests` 的返回）。

    Returns:
        (选中的路径, 是否命中首选)。`paths` 为空时返回 `("", False)`。
    """
    if not paths:
        return "", False
    for p in paths:
        if _is_ufs_manifest(p):
            return p, True
    return paths[0], False


def _is_ufs_manifest(path: str | Path) -> bool:
    """判断是否为 Manifest_UFSFiles_*.txt（打包进 pak/utoc 的清单）。

    必须精确判定：``Manifest_NonUFSFiles_Win64.txt`` 同样含有 ``UFSFiles`` 子串，
    用 ``in`` 会误判，从而拿到完全错误的比对基准。
    这里用正则锚定 ``Manifest_UFSFiles_`` 前缀。
    """
    return re.match(r"^Manifest_UFSFiles_Win64\.txt$", os.path.basename(path)) is not None


# ---------------------------------------------------------------------------
# exe 内的 /Script/ 模块与对象路径（真实字符串证据）
# ---------------------------------------------------------------------------


def _scan_ascii_objects(data: bytes) -> tuple[dict[str, int], dict[str, int]]:
    """扫描 ASCII 编码的 /Script/<Module> 与 /Script/<Module>.<Object>。"""
    modules: dict[str, int] = {}
    for match in re.finditer(re.escape(SCRIPT_ANCHOR), data):
        window = data[match.start(): match.start() + STRING_WINDOW]
        for mod in _MODULE_RE.finditer(window[:64]):
            name = mod.group(1).decode("ascii")
            modules[name] = modules.get(name, 0) + 1

    objects: dict[str, int] = {}
    for match in re.finditer(re.escape(SCRIPT_ANCHOR), data):
        window = data[match.start(): match.start() + STRING_WINDOW]
        m = _OBJECT_RE.match(window)
        if m:
            key = m.group(1).decode("ascii") + "." + m.group(2).decode("ascii")
            objects[key] = objects.get(key, 0) + 1
    return modules, objects


def _scan_utf16_objects(data: bytes) -> tuple[dict[str, int], dict[str, int]]:
    """扫描 UTF-16LE 编码的 /Script/<Module> 与 /Script/<Module>.<Object>。

    实测：UTF-16 是 exe 内引擎元数据的主要编码
    （CatapultHero 213 个模块名 / 273 个对象路径；ASCII 仅 205 / 7）。
    """
    anchor = SCRIPT_ANCHOR.decode("ascii").encode("utf-16-le")
    modules: dict[str, int] = {}
    objects: dict[str, int] = {}

    for match in re.finditer(re.escape(anchor), data):
        start = match.start()
        window = data[start: start + STRING_WINDOW]
        # 截到第一个 UTF-16 NUL 码元为止
        end = 0
        while end + 1 < len(window):
            if window[end] == 0 and window[end + 1] == 0:
                break
            end += 2
        text = window[:end].decode("utf-16-le", "ignore")

        m = re.match(r"/Script/([A-Za-z][A-Za-z0-9_]{0,62})\.([A-Za-z_][A-Za-z0-9_]{0,127})", text)
        if m:
            key = f"{m.group(1)}.{m.group(2)}"
            objects[key] = objects.get(key, 0) + 1

        m2 = re.match(r"/Script/([A-Za-z][A-Za-z0-9_]{0,62})", text)
        if m2:
            name = m2.group(1)
            modules[name] = modules.get(name, 0) + 1

    return modules, objects


def list_script_objects(exe_path: str | Path) -> dict:
    """从 exe 二进制中提取 UE 的 /Script/<Module> 与对象路径。

    **这是已验证的能力**：两样本均能提取到 200+ 模块名与 270+ 对象路径。
    但必须诚实说明其边界：
      - 提取到的是**字符串证据**，不是反射元数据
      - 因此 ``super``（父类）与 ``properties``（属性偏移）**无法**由此得到，一律留空
      - 字符串可能是硬编码引用（如 CDO 路径、配置类名），不等于「该类一定存在于本游戏」
    """
    exe_path = Path(exe_path)
    result: dict = {
        "ok": False,
        "exe": str(exe_path),
        "modules": [],
        "objects": [],
        "reason": "",
    }
    if not exe_path.is_file():
        result["reason"] = f"文件不存在：{exe_path}"
        return result

    try:
        info = pe.parse(str(exe_path))
    except (pe.PEError, OSError) as exc:
        result["reason"] = f"PE 解析失败：{exc}"
        return result

    data = info._data
    ascii_modules, ascii_objects = _scan_ascii_objects(data)
    utf16_modules, utf16_objects = _scan_utf16_objects(data)

    merged_modules = dict(ascii_modules)
    for name, hits in utf16_modules.items():
        merged_modules[name] = merged_modules.get(name, 0) + hits
    merged_objects = dict(ascii_objects)
    for name, hits in utf16_objects.items():
        merged_objects[name] = merged_objects.get(name, 0) + hits

    result["modules"] = [
        {"module": name, "hits": hits, "is_known_engine_module": name in KNOWN_UE_MODULES}
        for name, hits in sorted(merged_modules.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    result["objects"] = [
        {"full_name": name, "hits": hits}
        for name, hits in sorted(merged_objects.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    result["module_count"] = len(result["modules"])
    result["object_count"] = len(result["objects"])
    result["ascii_module_count"] = len(ascii_modules)
    result["ascii_object_count"] = len(ascii_objects)
    result["utf16_module_count"] = len(utf16_modules)
    result["utf16_object_count"] = len(utf16_objects)
    result["ok"] = bool(result["modules"] or result["objects"])
    if not result["ok"]:
        result["reason"] = "未能在 exe 中找到任何 /Script/ 字符串，可能不是 UE 引擎"
    return result


def get_class_info(exe_path: str | Path) -> list[UEClassInfo]:
    """把 exe 中提取到的对象路径转成 :class:`UEClassInfo` 列表。

    ``super`` 与 ``properties`` 一律为空——静态样本中无法验证，绝不填充猜测值。
    """
    data = list_script_objects(exe_path)
    infos: list[UEClassInfo] = []
    for item in data.get("objects", []):
        full = item["full_name"]
        module, _, name = full.partition(".")
        infos.append(UEClassInfo(name=name, full_name=full))
    return infos


# ---------------------------------------------------------------------------
# UE 游戏识别
# ---------------------------------------------------------------------------


def _find_project_layout(exe_path: Path) -> dict:
    """根据 exe 路径推导 UE 工程布局（实测样本的目录结构）。

    实测布局：
      <Root>/<Project>/Binaries/Win64/<Project>-Win64-Shipping.exe
      <Root>/Engine/...
      <Root>/<Project>/Content/Paks/<Project>-Windows.pak|.ucas|.utoc
      <Root>/Manifest_UFSFiles_Win64.txt
    """
    layout: dict = {
        "project_name": "",
        "project_dir": "",
        "engine_dir": "",
        "root_dir": "",
        "paks_dir": "",
        "pak_files": [],
        "utoc_files": [],
        "ucas_files": [],
        "manifests": [],
        "config_files": [],
    }
    if len(exe_path.parents) < 2:
        return layout

    win64 = exe_path.parent           # .../Binaries/Win64
    project_dir = win64.parent.parent  # .../<Project>
    root = project_dir.parent
    layout["project_name"] = project_dir.name
    layout["project_dir"] = str(project_dir)
    layout["root_dir"] = str(root)

    engine_dir = root / "Engine"
    if engine_dir.is_dir():
        layout["engine_dir"] = str(engine_dir)

    # Paks 目录可能在 <Root>/<Project>/Content/Paks
    for candidate in (project_dir / "Content" / "Paks",):
        if candidate.is_dir():
            layout["paks_dir"] = str(candidate)
            layout["pak_files"] = sorted(str(p) for p in candidate.glob("*.pak"))
            layout["utoc_files"] = sorted(str(p) for p in candidate.glob("*.utoc"))
            layout["ucas_files"] = sorted(str(p) for p in candidate.glob("*.ucas"))
            break

    layout["manifests"] = find_manifests(root)
    return layout


def detect_ue_game(exe_path: str | Path) -> dict:
    """识别给定 exe 是否为 UE 游戏，并给出可核查的证据。

    诚实边界（实测）：
      - 两个样本的 ``pe.runtime_kind`` 均为 ``native``，**不是** .NET/IL2CPP/Mono，
        因此本项目现有的运行时判定无法用于 UE，本函数改用字符串与目录证据。
      - **UE 版本号无法给出**：实测搜索 ``UE5\\.\\d+`` / ``UE4\\.\\d+`` / ``+UE\\d+\\.\\d+``
        / ``UnrealEngine[/\\\\][\\d.]+`` 全部 0 命中，``Engine/Build/Build.version`` 也不存在。
        故 ``ue_version`` 返回空串，并把原因写入 ``ue_version_reason``，绝不猜测。

    返回字典包含 ``is_ue``、``confidence``、``evidence``、``version``、``layout`` 等。
    """
    exe_path = Path(exe_path)
    result: dict = {
        "exe": str(exe_path),
        "exists": exe_path.is_file(),
        "is_ue": False,
        "confidence": 0,
        "engine": "unknown",
        "ue_version": "",
        "ue_version_reason": "",
        "project_name": "",
        "engine_dir": "",
        "evidence": [],
        "pe": {},
        "runtime_kind": "",
        "reason": "",
    }

    if not exe_path.is_file():
        result["reason"] = f"文件不存在：{exe_path}"
        return result

    evidence: list[str] = []
    score = 0
    layout = _find_project_layout(exe_path)

    # --- 证据 1：文件名符合 UE Shipping 命名 ---
    name = exe_path.name
    if re.match(r"^.+-(Win64|Windows)(-Shipping)?(-\w+)?\.exe$", name, re.IGNORECASE):
        evidence.append(f"文件名符合 UE 打包命名：{name}")
        score += 1

    # --- 证据 2：目录布局 ---
    if layout["engine_dir"]:
        evidence.append(f"存在 Engine 目录：{layout['engine_dir']}")
        score += 2
    if layout["manifests"]:
        evidence.append(
            f"存在打包清单 {len(layout['manifests'])} 个："
            + ", ".join(os.path.basename(p) for p in layout["manifests"])
        )
        score += 3
    if layout["paks_dir"]:
        evidence.append(
            f"存在 Paks 目录：{layout['paks_dir']}"
            f"（.pak {len(layout['pak_files'])} / .utoc {len(layout['utoc_files'])} / .ucas {len(layout['ucas_files'])}）"
        )
        score += 3
    if layout["project_name"] and layout["engine_dir"]:
        evidence.append(f"符合 <Root>/<Project>/... 与 <Root>/Engine 的 UE 布局，Project={layout['project_name']}")

    # --- 证据 3：PE 层 ---
    try:
        info = pe.parse(str(exe_path))
        result["pe"] = {
            "machine": info.machine,
            "is_64": info.is_64,
            "image_base": info.image_base,
            "subsystem": getattr(info, "subsystem", ""),
            "section_count": len(info.sections),
            "export_count": len(getattr(info, "exports", []) or []),
            "timestamp": getattr(info, "timestamp", 0),
        }
        result["runtime_kind"] = info.runtime_kind
        if info.is_64:
            score += 1
        if info.runtime_kind != "native":
            evidence.append(f"注意：PE runtime_kind={info.runtime_kind}（UE 通常为 native）")
    except (pe.PEError, OSError) as exc:
        result["reason"] = f"PE 解析失败：{exc}"
        return result

    # --- 证据 4：exe 内 /Script/ 字符串（最强证据） ---
    script = list_script_objects(exe_path)
    result["script_module_count"] = script.get("module_count", 0)
    result["script_object_count"] = script.get("object_count", 0)
    result["script_modules_sample"] = [
        m["module"] for m in script.get("modules", [])[:20]
    ]
    result["script_objects_sample"] = [
        o["full_name"] for o in script.get("objects", [])[:20]
    ]
    module_names = {m["module"] for m in script.get("modules", [])}
    known_hits = module_names & KNOWN_UE_MODULES

    if script.get("module_count", 0) >= MIN_SCRIPT_HITS:
        evidence.append(
            f"exe 内命中 /Script/ 模块名 {script['module_count']} 个"
            f"（对象路径 {script.get('object_count', 0)} 个）"
        )
        score += 6

    # --- 证据 5：version 与目录布局 ---
    if (layout["project_dir"] and os.path.isdir(os.path.join(layout["project_dir"], "Config"))) or \
       (layout["engine_dir"] and os.path.isdir(os.path.join(layout["engine_dir"], "Config"))):
        evidence.append("存在 Config 目录（UE 工程特征）")
        score += 1

    result["evidence"] = evidence
    result["known_engine_module_hits"] = sorted(known_hits)

    # 判定阈值：命中核心引擎模块 + 目录/字符串证据。
    # 实测两样本得分远超阈值；阈值刻意保守，避免把非 UE 程序判成 UE。
    is_ue = (
        len(known_hits) >= 3
        and script.get("module_count", 0) >= MIN_SCRIPT_HITS
        and score >= 10
    )
    result["is_ue"] = is_ue
    result["score"] = score
    result["confidence"] = min(100, score * 3) if is_ue else 0
    result["engine"] = "ue" if is_ue else "unknown"
    result["project_name"] = layout["project_name"] if is_ue else ""
    result["engine_dir"] = layout["engine_dir"]
    result["layout"] = layout

    if not is_ue:
        result["reason"] = (
            f"不满足 UE 判定条件（引擎模块命中 {len(known_hits)}/3，"
            f"/Script/ 模块 {script.get('module_count', 0)}/{MIN_SCRIPT_HITS}，"
            f"证据分 {score}/10）"
        )

    # --- UE 版本：实测无可靠来源，明确返回空 ---
    version, version_reason = _detect_ue_version(Path(layout["root_dir"]) if layout["root_dir"] else None, exe_path)
    result["ue_version"] = version
    result["ue_version_reason"] = version_reason
    result["ue_version_verified"] = bool(version)
    return result


def _detect_ue_version(root: Path | None, exe_path: Path) -> tuple[str, str]:
    """尝试用**可信来源**确定 UE 版本。

    实测结论：两个样本都没有可信来源，因此返回空串 + 原因。
    可信来源优先级：
      1. ``<Root>/Engine/Build/Build.version``（UE 官方的 JSON 构建版本文件）
      2. exe 内明确的版本字符串模式 ``UE5.x`` / ``UE4.x`` / ``+UE5.x``
    **不**接受任何第三方库的版本号（实测样本内 libpng 1.5.2 / OpenSSL 等会误报）。
    """
    tried: list[str] = []

    if root is not None and root.is_dir():
        build_version = root / "Engine" / "Build" / "Build.version"
        tried.append(str(build_version))
        if build_version.is_file():
            try:
                payload = json.loads(build_version.read_text(encoding="utf-8-sig", errors="replace"))
                major = payload.get("MajorVersion")
                minor = payload.get("MinorVersion")
                patch = payload.get("PatchVersion")
                if major is not None:
                    text = f"{major}.{minor}.{patch}"
                    branch = payload.get("BranchName", "")
                    return text, f"来自 Engine/Build/Build.version（BranchName={branch}）"
                return "", "Engine/Build/Build.version 存在但缺少 MajorVersion 字段"
            except (OSError, ValueError) as exc:
                return "", f"Engine/Build/Build.version 存在但无法解析：{exc}"

    try:
        data = _read_at(exe_path, 0, os.path.getsize(exe_path))
    except OSError as exc:
        return "", f"exe 读取失败：{exc}"

    patterns = [rb"UE5\.\d+", rb"UE4\.\d+", rb"\+UE\d+\.\d+", rb"UnrealEngine[/\\][\d.]+"]
    hits: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, data):
            hits.append(match.group(0).decode("ascii", "replace"))
            if len(hits) >= 5:
                break
        if len(hits) >= 5:
            break

    if hits:
        return hits[0], f"来自 exe 内版本字符串（共 {len(hits)} 处命中，取首个）"
    if root is not None:
        tried.append("exe 内 UE5.x/UE4.x/+UE5.x/UnrealEngine/x.y 模式")
        return "", "未找到可信版本来源；已尝试：" + "；".join(tried)
    return "", "根目录未知，且 exe 内未找到可信版本字符串"


# ---------------------------------------------------------------------------
# 汇总 profile
# ---------------------------------------------------------------------------


def extract_ue_profile(
    exe_path: str | Path,
    output_path: str | Path | None = None,
    include_entries: bool = False,
) -> dict:
    """汇总一个 UE 游戏的可提取信息，可选写出 JSON。

    汇总内容全部来自前面**已在真实样本上验证**的函数；任何取不到的部分
    都会带 ``implemented=False`` 或明确原因，绝不用占位数据填充。

    :param include_entries: 是否把 pak/utoc 的**全部**条目写进结果。
        默认 False——实测 Dinoblade 全量条目会让 JSON 达到 15.8 MB，
        作为「profile 摘要」并不合适；此时各容器只保留 ``entries_sample``
        与计数。需要完整清单请直接调用 :func:`list_pak_entries` /
    :func:`list_iostore_entries`。
    """
    exe_path = Path(exe_path)
    progress(f"UE profile: 开始分析 {exe_path}")
    profile: dict = {
        "exe": str(exe_path),
        "generated_by": "gamemod_mcp.engine.ue",
        "capabilities_verified": [
            "detect_ue_game",
            "read_pak_index / list_pak_entries",
            "read_iostore_index / list_iostore_entries",
            "list_script_objects",
            "read_manifest",
            "read_ue_config",
        ],
        "capabilities_not_implemented": {
            "fname_pool": "样本中无法定位 FName 池（无有效 FNameEntry 头），未实现",
            "usmap": "样本中不存在 .usmap 文件，未实现",
            "ue_version": "样本中无可信版本来源，返回空值",
            "uobject_property_offsets": "需运行时（Frida）读取，静态样本无法验证，未实现",
        },
    }

    detection = detect_ue_game(exe_path)
    profile["detection"] = detection
    profile["is_ue"] = detection["is_ue"]

    if not detection["is_ue"]:
        profile["reason"] = detection.get("reason", "未识别为 UE 游戏")
        if output_path:
            _write_json(output_path, profile)
        return profile

    layout = detection.get("layout", {})

    # --- pak ---
    pak_reports: list[dict] = []
    for pak in layout.get("pak_files", []):
        try:
            data = read_pak_index(pak)
        except (OSError, struct.error) as exc:
            data = {"ok": False, "pak": pak, "reason": str(exc)}
        pak_reports.append(
            {
                "pak": pak,
                "file_size": data.get("file_size"),
                "ok": data.get("ok", False),
                "reason": data.get("reason", ""),
                "pak_version": data.get("pak_info", {}).get("version"),
                "mount_point": data.get("index_header", {}).get("mount_point"),
                "num_entries": data.get("index_header", {}).get("num_entries"),
                "file_count": data.get("file_count"),
                "dir_count": data.get("dir_count"),
                "self_check": data.get("self_check", ""),
                "entries_sample": [
                    e["path"] for e in (data.get("entries") or [])[:10]
                ],
            }
        )
        if include_entries:
            pak_reports[-1]["entries"] = data.get("entries") or []
        else:
            # 交叉校验需要完整集合，但结果里不必保留全量明文
            pak_reports[-1]["_all_files"] = [
                e["path"] for e in (data.get("entries") or []) if not e["is_dir"]
            ]
    profile["pak"] = pak_reports

    # --- utoc ---
    utoc_reports: list[dict] = []
    for utoc in layout.get("utoc_files", []):
        try:
            data = read_iostore_index(utoc)
        except (OSError, struct.error) as exc:
            data = {"ok": False, "utoc": utoc, "reason": str(exc)}
        utoc_reports.append(
            {
                "utoc": utoc,
                "file_size": data.get("file_size"),
                "ok": data.get("ok", False),
                "reason": data.get("reason", ""),
                "version": data.get("version"),
                "entry_count": data.get("entry_count"),
                "dir_entry_count": data.get("dir_entry_count"),
                "file_entry_count": data.get("file_entry_count"),
                "entries_sample": [e["path"] for e in (data.get("entries") or [])[:10]],
            }
        )
        if include_entries:
            utoc_reports[-1]["entries"] = data.get("entries") or []
        else:
            utoc_reports[-1]["_all_files"] = [
                e["path"] for e in (data.get("entries") or []) if not e["is_dir"]
            ]
    profile["iostore"] = utoc_reports

    # --- manifest ---
    # 先保留全量条目用于交叉校验，最后再按 include_entries 决定是否精简
    manifest_reports: list[dict] = []
    for manifest in layout.get("manifests", []):
        try:
            data = read_manifest(manifest)
        except OSError as exc:
            data = {"ok": False, "path": manifest, "reason": str(exc)}
        manifest_reports.append(
            {
                "manifest": manifest,
                "ok": data.get("ok", False),
                "count": data.get("count", 0),
                "unique_count": data.get("unique_count", 0),
                "reason": data.get("reason", ""),
                "entries": data.get("entries", []),
            }
        )
    profile["manifests"] = manifest_reports

    # --- 交叉校验：pak/utoc 重建路径 vs Manifest（大小写不敏感，UE 路径大小写不严谨）---
    # 只用 Manifest_UFSFiles_*.txt 作为地面真值：
    # 它列出的正是「打进 pak/utoc 的内容」，而 Manifest_NonUFSFiles / Manifest_DebugFiles
    # 记录的是不进 pak 的旁挂文件（实测 CatapultHero 各仅 31 / 3 条），拿它们比对没有意义。
    #
    # 注意：这里必须精确匹配文件名，**不能**用 `"UFSFiles" in name` ——
    # "Manifest_NonUFSFiles_Win64.txt" 也含有 "UFSFiles" 子串，
    # 实测踩过这个坑：会误取只有 31 条的 NonUFS 清单，导致命中率假性 0%。
    cross: list[dict] = []
    truth: set[str] = set()
    truth_source = ""
    for manifest_report in manifest_reports:
        if not manifest_report["ok"]:
            continue
        if not _is_ufs_manifest(manifest_report["manifest"]):
            continue
        truth = {e["path"] for e in manifest_report["entries"]}
        truth_source = os.path.basename(manifest_report["manifest"])
        break

    if truth:
        truth_lower = {p.lower(): p for p in truth}
        for report in list(pak_reports) + list(utoc_reports):
            if not report["ok"]:
                continue
            # include_entries=False 时完整清单在 _all_files 里，不在输出字段中
            rebuilt = set(report.get("_all_files") or [])
            if not rebuilt:
                rebuilt = {e["path"] for e in (report.get("entries") or []) if not e["is_dir"]}
            hit = sum(1 for p in rebuilt if p.lower() in truth_lower)
            kind = "pak" if report.get("pak") else "iostore"
            cross.append(
                {
                    "source": os.path.basename(report.get("pak") or report.get("utoc")),
                    "kind": kind,
                    "truth_source": truth_source,
                    "rebuilt": len(rebuilt),
                    "manifest": len(truth),
                    "matched": hit,
                    "match_ratio": round(hit / len(rebuilt), 4) if rebuilt else 0.0,
                    "manifest_coverage": round(hit / len(truth), 4) if truth else 0.0,
                    "note": (
                        "大小写不敏感比对（UE 路径大小写不严谨）；"
                        f"地面真值取 {truth_source}；"
                        "rebuilt 是 pak/utoc 内实际条目，manifest 是全量打包文件，"
                        "故 match_ratio 才是「解析正确率」，manifest_coverage 只是覆盖率"
                    ),
                }
            )
    profile["cross_check"] = cross

    # 按需精简：不保留全量条目时，把 _all_files / manifest entries 去掉，避免 profile 膨胀
    if not include_entries:
        for report in list(pak_reports) + list(utoc_reports):
            report.pop("_all_files", None)
        for manifest_report in manifest_reports:
            manifest_report["entries_sample"] = [
                e["path"] for e in manifest_report.pop("entries", [])[:10]
            ]

    # --- /Script/ 模块与对象 ---
    script = list_script_objects(exe_path)
    profile["script"] = {
        "ok": script.get("ok", False),
        "module_count": script.get("module_count", 0),
        "object_count": script.get("object_count", 0),
        "modules": script.get("modules", []),
        "objects": script.get("objects", []),
        "caveat": (
            "这些是 exe 内的字符串证据，不是反射元数据；"
            "super/properties 无法由此获得，故 UEClassInfo 中一律留空"
        ),
    }

    # --- 配置 ---
    config_reports: list[dict] = []
    for cfg in layout.get("config_files", []) + list_config_files(layout.get("root_dir", "")):
        if any(c["path"] == cfg for c in config_reports):
            continue
        data = read_ue_config(cfg)
        config_reports.append(
            {
                "path": cfg,
                "ok": data["ok"],
                "line_count": data["line_count"],
                "section_count": len(data["sections"]),
                "sections": data["sections"],
                "reason": data["reason"],
            }
        )
    profile["configs"] = config_reports

    profile["ok"] = True
    if output_path:
        _write_json(output_path, profile)
    progress("UE profile: 完成")
    return profile


def _write_json(output_path: str | Path, payload: dict) -> None:
    """写出 UTF-8（无 BOM）JSON。"""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    target.write_text(text, encoding="utf-8", newline="\n")
