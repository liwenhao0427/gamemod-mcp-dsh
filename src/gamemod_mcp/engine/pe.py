"""纯 Python PE/COFF 解析器（无第三方依赖）。

用途：
- 判定目标是否为 .NET / IL2CPP / Mono / UE
- RVA ↔ 文件偏移换算
- 读取导出表（用于定位 Mono 的 mono_* 导出、IL2CPP 的 il2cpp_* 导出）
- 在镜像中按模式搜索（用于启发式定位 IL2CPP 的 registration 结构）

之所以不依赖 pefile：MCP Server 可能被丢到任意环境里跑，
只带标准库能显著降低部署摩擦。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 常量

DOS_MAGIC = b"MZ"
PE_MAGIC = b"PE\0\0"
CLR_DIR_INDEX = 14  # DataDirectory[14] = CLR Runtime Header

IMAGE_FILE_MACHINE = {
    0x014C: "x86",
    0x8664: "x64",
    0x01C0: "ARM",
    0x01C4: "ARMv7",
    0xAA64: "ARM64",
}

IMAGE_SUBSYSTEM = {
    1: "native",
    2: "windows-gui",
    3: "windows-cui",
    9: "windows-ce",
    10: "efi",
}


class PEError(RuntimeError):
    """PE 解析失败。"""


# ---------------------------------------------------------------- 数据结构


@dataclass
class Section:
    """一个 PE 节区。"""

    name: str
    virtual_address: int
    virtual_size: int
    raw_pointer: int
    raw_size: int
    characteristics: int

    @property
    def readable_name(self) -> str:
        return self.name.rstrip("\0")

    @property
    def is_executable(self) -> bool:
        return bool(self.characteristics & 0x20000000)

    @property
    def is_writable(self) -> bool:
        return bool(self.characteristics & 0x80000000)

    def contains_rva(self, rva: int) -> bool:
        size = max(self.virtual_size, self.raw_size)
        return self.virtual_address <= rva < self.virtual_address + size


@dataclass
class Export:
    """一个导出符号。"""

    name: str
    ordinal: int
    rva: int


@dataclass
class ImportedDll:
    """一个导入表条目。"""

    dll: str
    functions: list[str] = field(default_factory=list)
    is_delay: bool = False


@dataclass
class PEInfo:
    """PE 文件的解析结果。"""

    path: Path
    size: int
    machine: str
    is_64: bool
    image_base: int
    entry_point_rva: int
    timestamp: int
    subsystem: str
    sections: list[Section]
    exports: list[Export]
    imports: list[ImportedDll]
    clr_header_rva: int

    # 由 parse 填充
    _data: bytes = field(default=b"", repr=False)
    _export_map: dict[str, int] = field(default_factory=dict, repr=False)

    # ---------------------------------------------------------- 派生属性

    @property
    def is_dotnet(self) -> bool:
        """是否为托管程序集（有 CLR Runtime Header）。"""
        return self.clr_header_rva != 0

    @property
    def is_nativeaot(self) -> bool:
        """是否为 .NET NativeAOT 编译产物。

        NativeAOT 会生成 `.managed` / `hydrated` 节区，且不是传统 .NET。
        """
        names = {s.readable_name.lower() for s in self.sections}
        return "hydrated" in names or ".managed" in names

    @property
    def is_il2cpp(self) -> bool:
        """是否含 IL2CPP 运行时导出。"""
        return any(e.name.startswith("il2cpp_") for e in self.exports)

    @property
    def is_mono(self) -> bool:
        """是否含 Mono 运行时导出。"""
        return any(e.name.startswith("mono_") for e in self.exports)

    @property
    def runtime_kind(self) -> str:
        """推断运行时类型。"""
        if self.is_dotnet:
            return "dotnet"
        if self.is_nativeaot:
            return "nativeaot"
        if self.is_il2cpp:
            return "il2cpp"
        if self.is_mono:
            return "mono"
        return "native"

    # ---------------------------------------------------------- 换算 / 读取

    def rva_to_offset(self, rva: int) -> int | None:
        """RVA → 文件偏移。失败返回 None。"""
        for sec in self.sections:
            if sec.contains_rva(rva):
                delta = rva - sec.virtual_address
                if delta < sec.raw_size:
                    return sec.raw_pointer + delta
                # 位于节的虚拟填充区，无对应文件数据
                return None
        return None

    def offset_to_rva(self, offset: int) -> int | None:
        """文件偏移 → RVA。"""
        for sec in self.sections:
            if sec.raw_pointer <= offset < sec.raw_pointer + sec.raw_size:
                return sec.virtual_address + (offset - sec.raw_pointer)
        return None

    def read_rva(self, rva: int, size: int) -> bytes | None:
        """按 RVA 读取原始字节。"""
        off = self.rva_to_offset(rva)
        if off is None or off + size > len(self._data):
            return None
        return self._data[off : off + size]

    def read_offset(self, offset: int, size: int) -> bytes:
        """按文件偏移读取原始字节。"""
        return self._data[offset : offset + size]

    def export_rva(self, name: str) -> int | None:
        """按名字取导出符号的 RVA。"""
        return self._export_map.get(name)

    def find_export_by_prefix(self, prefix: str) -> list[Export]:
        """按前缀查找导出符号。"""
        return [e for e in self.exports if e.name.startswith(prefix)]

    # ---------------------------------------------------------- 搜索

    def search_bytes(self, pattern: bytes, limit: int = 0) -> list[int]:
        """在文件中搜索字节串，返回文件偏移列表。"""
        out: list[int] = []
        start = 0
        while True:
            idx = self._data.find(pattern, start)
            if idx < 0:
                break
            out.append(idx)
            start = idx + 1
            if limit and len(out) >= limit:
                break
        return out

    def iter_data_sections(self):
        """迭代已初始化数据节区（.rdata/.data 等），用于扫描 registration 结构。"""
        data = self._data
        for sec in self.sections:
            if sec.raw_size == 0 or sec.virtual_address == 0:
                continue
            # 跳过纯代码节
            if sec.is_executable and not sec.is_writable:
                continue
            yield sec, data[sec.raw_pointer : sec.raw_pointer + sec.raw_size]


# ---------------------------------------------------------------- 解析


def _read_cstring(data: bytes, offset: int, limit: int = 512) -> str:
    """读取以 NUL 结尾的 ASCII 字符串。"""
    end = data.find(b"\0", offset)
    if end < 0 or end - offset > limit:
        end = min(offset + limit, len(data))
    return data[offset:end].decode("ascii", errors="replace")


def parse(path: str | Path) -> PEInfo:
    """解析 PE 文件。失败抛 PEError。"""
    path = Path(path)
    data = path.read_bytes()
    size = len(data)

    if size < 0x40 or data[:2] != DOS_MAGIC:
        raise PEError(f"不是有效的 PE 文件（缺少 MZ 头）: {path}")

    # ---- DOS 头 → e_lfanew
    (e_lfanew,) = struct.unpack_from("<I", data, 0x3C)
    if e_lfanew + 24 > size or data[e_lfanew : e_lfanew + 4] != PE_MAGIC:
        raise PEError(f"NT 头签名无效: {path}")

    coff = e_lfanew + 4
    machine_raw, num_sections, timestamp = struct.unpack_from("<HHI", data, coff)
    opt_size, characteristics = struct.unpack_from("<HH", data, coff + 16)

    machine = IMAGE_FILE_MACHINE.get(machine_raw, f"unknown(0x{machine_raw:04X})")
    is_64 = machine_raw in (0x8664, 0xAA64)

    opt = coff + 20
    magic = struct.unpack_from("<H", data, opt)[0]
    pe32_plus = magic == 0x20B

    if pe32_plus != is_64:
        # 以 Optional Header magics 为准修正
        is_64 = pe32_plus

    entry_point_rva = struct.unpack_from("<I", data, opt + 16)[0]
    image_base = (
        struct.unpack_from("<Q", data, opt + 24)[0]
        if pe32_plus
        else struct.unpack_from("<I", data, opt + 28)[0]
    )
    subsystem = struct.unpack_from("<H", data, opt + 68)[0]

    # ---- 数据目录
    dd_offset = opt + (112 if pe32_plus else 96)
    num_dirs = struct.unpack_from("<I", data, opt + (108 if pe32_plus else 92))[0]

    directories: list[tuple[int, int]] = []
    for i in range(min(num_dirs, 16)):
        rva, sz = struct.unpack_from("<II", data, dd_offset + i * 8)
        directories.append((rva, sz))

    clr_header_rva = directories[CLR_DIR_INDEX][0] if len(directories) > CLR_DIR_INDEX else 0

    # ---- 节区表
    sec_start = opt + opt_size
    sections: list[Section] = []
    for i in range(num_sections):
        base = sec_start + i * 40
        if base + 40 > size:
            break
        raw_name = data[base : base + 8]
        vsize, vaddr, rsize, rptr = struct.unpack_from("<IIII", data, base + 8)
        chars = struct.unpack_from("<I", data, base + 36)[0]
        sections.append(
            Section(
                name=raw_name.decode("ascii", errors="replace"),
                virtual_address=vaddr,
                virtual_size=vsize,
                raw_pointer=rptr,
                raw_size=rsize,
                characteristics=chars,
            )
        )

    info = PEInfo(
        path=path,
        size=size,
        machine=machine,
        is_64=is_64,
        image_base=image_base,
        entry_point_rva=entry_point_rva,
        timestamp=timestamp,
        subsystem=IMAGE_SUBSYSTEM.get(subsystem, f"unknown({subsystem})"),
        sections=sections,
        exports=[],
        imports=[],
        clr_header_rva=clr_header_rva,
        _data=data,
    )

    # ---- 导出表
    if directories and directories[0][0]:
        info.exports = _parse_exports(info, directories[0][0])

    # ---- 导入表
    if len(directories) > 1 and directories[1][0]:
        info.imports = _parse_imports(info, directories[1][0])

    # ---- 延迟导入表
    if len(directories) > 13 and directories[13][0]:
        info.imports.extend(_parse_delay_imports(info, directories[13][0]))

    info._export_map = {e.name: e.rva for e in info.exports}
    return info


def _parse_exports(info: PEInfo, export_rva: int) -> list[Export]:
    """解析导出表。"""
    data = info._data
    base = info.rva_to_offset(export_rva)
    if base is None or base + 40 > len(data):
        return []

    (_, _, _, _, name_rva, ordinal_base, func_count, name_count,
     funcs_rva, names_rva, ords_rva) = struct.unpack_from("<IIHHIIIIIII", data, base)

    out: list[Export] = []
    for i in range(name_count):
        n_rva_off = info.rva_to_offset(names_rva + i * 4)
        if n_rva_off is None:
            continue
        (sym_name_rva,) = struct.unpack_from("<I", data, n_rva_off)

        sym_name_off = info.rva_to_offset(sym_name_rva)
        if sym_name_off is None:
            continue
        sym_name = _read_cstring(data, sym_name_off)

        ord_off = info.rva_to_offset(ords_rva + i * 2)
        if ord_off is None:
            continue
        (ordinal,) = struct.unpack_from("<H", data, ord_off)

        fn_off = info.rva_to_offset(funcs_rva + ordinal * 4)
        if fn_off is None:
            continue
        (fn_rva,) = struct.unpack_from("<I", data, fn_off)

        if fn_rva == 0:
            continue
        out.append(Export(name=sym_name, ordinal=ordinal + ordinal_base, rva=fn_rva))

    return out


def _parse_imports(info: PEInfo, import_rva: int) -> list[ImportedDll]:
    """解析标准导入表。"""
    data = info._data
    out: list[ImportedDll] = []
    ptr_size = 8 if info.is_64 else 4
    fmt = "<Q" if info.is_64 else "<I"
    ordinal_flag = 1 << (63 if info.is_64 else 31)

    cursor = import_rva
    seen: set[int] = set()
    while cursor and cursor not in seen:
        seen.add(cursor)
        base = info.rva_to_offset(cursor)
        if base is None or base + 20 > len(data):
            break

        orig_first, _, _, name_rva, first_thunk = struct.unpack_from("<IIIII", data, base)
        if name_rva == 0:
            break

        name_off = info.rva_to_offset(name_rva)
        if name_off is None:
            break
        dll = _read_cstring(data, name_off)

        entry = ImportedDll(dll=dll)
        thunk_rva = orig_first or first_thunk
        while thunk_rva:
            t_off = info.rva_to_offset(thunk_rva)
            if t_off is None or t_off + ptr_size > len(data):
                break
            (value,) = struct.unpack_from(fmt, data, t_off)
            if value == 0:
                break

            if value & ordinal_flag:
                entry.functions.append(f"#{value & 0xFFFF}")
            else:
                hint_off = info.rva_to_offset(value)
                if hint_off is not None:
                    entry.functions.append(_read_cstring(data, hint_off + 2))

            thunk_rva += ptr_size

        if entry.functions:
            out.append(entry)

        cursor += 20

    return out


def _parse_delay_imports(info: PEInfo, delay_rva: int) -> list[ImportedDll]:
    """解析延迟导入表。"""
    data = info._data
    out: list[ImportedDll] = []
    ptr_size = 8 if info.is_64 else 4
    fmt = "<Q" if info.is_64 else "<I"
    ordinal_flag = 1 << (63 if info.is_64 else 31)

    cursor = delay_rva
    seen: set[int] = set()
    while cursor and cursor not in seen:
        seen.add(cursor)
        base = info.rva_to_offset(cursor)
        if base is None or base + 32 > len(data):
            break

        attrs, name_rva, _, _, _, _, _, _ = struct.unpack_from("<IIIIIIII", data, base)
        if name_rva == 0:
            break

        name_off = info.rva_to_offset(name_rva)
        if name_off is None:
            break
        dll = _read_cstring(data, name_off)

        entry = ImportedDll(dll=dll, is_delay=True)
        int_rva = info.rva_to_offset(base)  # 见下：延迟导入的字段布局
        # 重新按字段读取 IAT / INT
        iat, int_table = struct.unpack_from("<II", data, base + 16)
        thunk_rva = int_table or iat

        while thunk_rva:
            t_off = info.rva_to_offset(thunk_rva)
            if t_off is None or t_off + ptr_size > len(data):
                break
            (value,) = struct.unpack_from(fmt, data, t_off)
            if value == 0:
                break
            if value & ordinal_flag:
                entry.functions.append(f"#{value & 0xFFFF}")
            else:
                hint_off = info.rva_to_offset(value)
                if hint_off is not None:
                    entry.functions.append(_read_cstring(data, hint_off + 2))
            thunk_rva += ptr_size

        if entry.functions:
            out.append(entry)

        cursor += 32

    return out


# ---------------------------------------------------------------- 便捷函数


def summarize(path: str | Path) -> dict:
    """生成一个便于展示的 PE 摘要（用于 MCP 工具的返回值）。"""
    info = parse(path)
    runtime = info.runtime_kind

    # 从导出/导入里挑出有价值的运行时符号
    highlights: list[str] = []
    if runtime == "il2cpp":
        highlights = sorted({e.name for e in info.exports if e.name.startswith("il2cpp_")})[:12]
    elif runtime == "mono":
        highlights = sorted({e.name for e in info.exports if e.name.startswith("mono_")})[:12]

    return {
        "path": str(info.path),
        "size": info.size,
        "machine": info.machine,
        "is_64": info.is_64,
        "image_base": hex(info.image_base),
        "entry_point_rva": hex(info.entry_point_rva),
        "timestamp": info.timestamp,
        "subsystem": info.subsystem,
        "runtime": runtime,
        "is_dotnet": info.is_dotnet,
        "is_nativeaot": info.is_nativeaot,
        "section_count": len(info.sections),
        "sections": [
            {
                "name": s.readable_name,
                "rva": hex(s.virtual_address),
                "vsize": s.virtual_size,
                "raw_size": s.raw_size,
                "flags": ("X" if s.is_executable else "-") + ("W" if s.is_writable else "-"),
            }
            for s in info.sections
        ],
        "export_count": len(info.exports),
        "import_dll_count": len(info.imports),
        "runtime_symbols": highlights,
    }
