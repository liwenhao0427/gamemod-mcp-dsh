"""Unity IL2CPP `global-metadata.dat` 解析器 —— 本项目的核心能力。

设计思想：**元数据优先定位（metadata-first）**
    不扫内存、不猜地址。游戏编译时会把完整的类型系统留在 global-metadata.dat 里，
    直接解析它即可拿到类名、命名空间、字段、方法、签名、继承关系。
    这比 Cheat Engine 式的内存扫描精确、稳定、快得多。

    Cheat Engine 的做法是「运行时反复搜索数值」；
    本项目的做法是「先读懂程序自己的符号表，再直接定位」。
    内存扫描只作为「元数据缺失」时的兜底手段。

已实测验证（Garden of Witches，metadata v31，17,331,952 字节）：
    typeDefinitions  1,766,336 B / 88 = 20,072 个类型
    methods          5,022,036 B / 36 = 139,501 个方法
    fields             982,464 B / 12 =  81,872 个字段
    parameters       1,603,452 B / 12 = 133,621 个参数
    images               3,760 B / 40 =      94 个程序集
    一致性校验：Σmethod_count = 139,501（差 0）、Σfield_count = 81,872（差 0）
    外部交叉验证：与 AiMod 对同一游戏的运行时回执/dump 完全一致

关键实现细节（踩过的坑，勿改）
--------------------------------------------------
1. v31 的 Il2CppTypeDefinition 是 **88 字节**，布局为
       int32 × 16（@0..60）
       uint16 × 8（@64..78）  ← 计数块，method_count 在 @64，field_count 在 @68
       int32 bitfield（@80）
       uint32 token（@84）
   早期版本是 92 字节。若按错误的偏移解包，会整体错位 4 字节，
   表现为「方法数/字段数互换」，非常隐蔽。
2. `parentIndex`(@16) 以及字段/方法的类型索引，都是 **Il2CppType 表**的下标，
   而不是 typeDefinitions 的下标。Il2CppType 表**不在**元数据文件里
   （它由 GameAssembly.dll 在运行时构建）。
   但可以绕过：每个 typeDefinition 的 `byvalTypeIndex`(@8) 就是「它自己的 Il2CppType」下标，
   于是构造 byval → typedef 的反查表，就能把 parentIndex 还原成真实的基类。
   实测该反查表无冲突，父类解析准确率 92.8%（剩余为泛型实例/数组，其 Il2CppType 运行时才生成）。
3. 字段偏移（offset）和方法 RVA **不在此文件中**：
   字段偏移由 IL2CPP 运行时按对齐规则计算，方法 RVA 需要 CodeRegistration。
   两者由 runtime 层（Frida）在真实进程中读取。

支持版本：v24 ~ v31。核心表（string/fields/methods/parameters/images/assemblies/
typeDefinitions）在所有这些版本中下标一致，故主流程与版本无关。

超出区间必须**快速如实拒绝**，不许硬解
--------------------------------------------------
实测（Enter the Chronosphere，Unity 6000.3.14f1，metadata **v39**）：
表布局已变，头部里 `fields` 表 24,998 字节**不能被 12 整除**、
`methods` 也不能被 12 整除 —— 即连「记录大小」都推不出来。
修复前的行为是：stride 推断失败只 `warn` 一句，然后**照旧硬解**，
得到 181,396 个类型、Σfield_count = 3,156,391,098（真值只有 2,083 个字段），
接着花 **573 秒**建索引，最后抛出一句
`构建索引失败：UNIQUE constraint failed: fields.idx` ——
把「版本不支持」伪装成了「数据库约束错误」。

那 573 秒全是空转，而且错误信息指向 sqlite，会把人往错误方向带。
所以现在在解析头部时就用**两条硬结构不变量**判定布局是否可用：
    · 版本号在实测支持区间内（v24 ~ v31）；
    · 固定记录大小的表必须能被整除（fields/12、parameters/12、
      images/40、assemblies/64）—— 实测 v39 的 `fields` 表
      24,998 字节 % 12 == 2，这一条单独就能拦下它。
任一不成立就 `MetadataError`，并明确告知「版本不支持」与可用替代路径。

注意**不要**把「typeDefinitions 能否铺满 fields」也当成拒绝条件：
那条判据是 `_typedef_size` 用来**推断** stride 的启发式，
当拒绝条件会误伤合法输入（fields 表为空、类型不带字段的元数据）。
它继续以 warn 形式留在 `_typedef_size` 里。
"""

from __future__ import annotations

import bisect
import mmap
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..logutil import progress, warn

METADATA_MAGIC = 0xFAB11BAF

#: 解析器**实测支持**的元数据版本区间（闭区间）。
#:
#: 实测样本：v29 ×1、v31 ×9 全部通过；v39 布局已变（见模块 docstring）。
#: 这个区间只用于**快速拒绝**明显超出区间的文件，真正的布局正确性
#: 仍由下面 `_validate_layout()` 的整除/铺满检验来判 —— 版本号只是先手。
SUPPORTED_VERSION_MIN = 24
SUPPORTED_VERSION_MAX = 31

#: 记录大小**固定**的表：(表名, 记录字节数)。
#: 这些表的 size 必须能被记录大小整除，否则说明头部布局判定已错。
_FIXED_SIZE_TABLES: tuple[tuple[str, int], ...] = (
    ("fields", 12),
    ("parameters", 12),
    ("images", 40),
    ("assemblies", 64),
)

# ---------------------------------------------------------------- 常量

# typeDefinitions 记录大小（字节）。
#
# 这里曾把 v24~v29 写成 92，**是错的**：实测 v29（Ved，19.4 MB 元数据）用 92
# 会解析出 21,290 个类型，且 Σfield_count = 650,148,194（真值 96,094）——
# 每个「类型」的 field_start/field_count 全部错位，建索引时撞上
# `UNIQUE constraint failed: fields.idx`，open_game 直接失败。
#
# 正确的判据不是「版本号」，而是**字段区间必须恰好铺满 fields 表**：
#   ∪ [field_start, field_start+field_count) == {0 .. len(fields)-1}，且不重叠。
# 按这个判据在 10 份真实样本上实测（v29 ×1、v31 ×9），88 全部通过；
# 92 在这些样本上连 `typeDefinitions.size % 92 == 0` 都不成立，直接用不了。
# field_count == 0 的记录要跳过：它们的 field_start 是哨兵值、不是真实区间。
TYPEDEF_SIZE_V31 = 88
TYPEDEF_SIZE_LEGACY = 88

# v31 的 methods 记录大小；更早版本为 32
METHOD_SIZE_V31 = 36
METHOD_SIZE_LEGACY = 32

FIELD_SIZE = 12
PARAMETER_SIZE = 12
IMAGE_SIZE = 40
ASSEMBLY_SIZE = 64

# 头部 (offset,size) 对的表名顺序 —— v29 及以上
TABLE_ORDER_MODERN = [
    "stringLiteral", "stringLiteralData", "string", "events", "properties",
    "methods", "parameterDefaultValues", "fieldDefaultValues",
    "fieldAndParameterDefaultValueData", "fieldMarshaledSizes", "parameters",
    "fields", "genericParameters", "genericParameterConstraints",
    "genericContainers", "nestedTypes", "interfaces", "vtableMethods",
    "interfaceOffsets", "typeDefinitions", "images", "assemblies",
    "fieldRefs", "referencedAssemblies",
    "attributeData", "attributeDataRange",
    "unresolvedVirtualCallParameterTypes", "unresolvedVirtualCallParameterRanges",
    "windowsRuntimeTypeNames", "windowsRuntimeStrings",
    "exportedTypeDefinitions",
]

# 头部 (offset,size) 对的表名顺序 —— v27 及以下（含 attributeTypes 表）
TABLE_ORDER_LEGACY = [
    "stringLiteral", "stringLiteralData", "string", "events", "properties",
    "methods", "parameterDefaultValues", "fieldDefaultValues",
    "fieldAndParameterDefaultValueData", "fieldMarshaledSizes", "parameters",
    "fields", "genericParameters", "genericParameterConstraints",
    "genericContainers", "nestedTypes", "interfaces", "vtableMethods",
    "interfaceOffsets", "typeDefinitions", "images", "assemblies",
    "fieldRefs", "referencedAssemblies", "attributeTypes", "attributeData",
    "unresolvedVirtualCallParameterTypes", "unresolvedVirtualCallParameterRanges",
    "windowsRuntimeTypeNames", "windowsRuntimeStrings",
    "exportedTypeDefinitions",
]

# ---- TypeAttributes / MethodAttributes / FieldAttributes（ECMA-335）
TYPE_ATTR_VISIBILITY_MASK = 0x7
TYPE_ATTR_INTERFACE = 0x20
TYPE_ATTR_ABSTRACT = 0x80
TYPE_ATTR_SEALED = 0x100

METHOD_ATTR_STATIC = 0x10
METHOD_ATTR_FINAL = 0x20
METHOD_ATTR_VIRTUAL = 0x40
METHOD_ATTR_ABSTRACT = 0x400
METHOD_ATTR_PUBLIC = 0x6

FIELD_ATTR_STATIC = 0x10
FIELD_ATTR_LITERAL = 0x40

_VISIBILITY_NAMES = {
    0: "private", 1: "public", 2: "public", 3: "internal",
    4: "protected_internal", 5: "internal", 6: "protected",
    7: "private_protected",
}


class MetadataError(RuntimeError):
    """元数据解析失败。"""


# ---------------------------------------------------------------- 数据结构
# 全部使用 slots，避免 13 万+ 对象造成内存膨胀


@dataclass(slots=True)
class TypeDef:
    """一个类型定义（class / struct / enum / interface）。"""

    index: int
    name: str
    namespace: str
    parent_il2cpp_type: int
    # declaringTypeIndex(@12)：指向 Il2CppType 空间（不是 typedef 下标），
    # 需经 byval 反查表换算成 typedef 下标才能上溯外层类型。
    declaring_il2cpp_type: int
    # 由 declaringTypeIndex 逐级上溯拼出的稳定全名（Outer.Inner），解析后回填。
    # 元数据原始 name 对嵌套类型只给短名（如 EasingType），单独用会与世界失配。
    nested_name: str
    flags: int
    field_start: int
    method_start: int
    property_start: int
    event_start: int
    nested_start: int
    interfaces_start: int
    vtable_start: int
    interface_offsets_start: int
    byval_type: int
    generic_container: int
    method_count: int
    property_count: int
    field_count: int
    event_count: int
    nested_count: int
    vtable_count: int
    interfaces_count: int
    interface_offsets_count: int
    token: int

    def __str__(self) -> str:
        return self.full_name

    @property
    def full_name(self) -> str:
        """完整类型名（命名空间 + 名字）。

        注意：嵌套类型在元数据里只存短名（如 EasingType），本属性**不含**外层前缀。
        需要与外部工具（Il2CppDumper 输出、Unity 日志等）对齐时请用 nested_name。
        """
        return f"{self.namespace}.{self.name}" if self.namespace else self.name

    @property
    def is_interface(self) -> bool:
        return bool(self.flags & TYPE_ATTR_INTERFACE)

    @property
    def is_abstract(self) -> bool:
        return bool(self.flags & TYPE_ATTR_ABSTRACT)

    @property
    def is_sealed(self) -> bool:
        return bool(self.flags & TYPE_ATTR_SEALED)

    @property
    def visibility(self) -> str:
        return _VISIBILITY_NAMES.get(self.flags & TYPE_ATTR_VISIBILITY_MASK, "?")


@dataclass(slots=True)
class FieldDef:
    """一个字段定义。"""

    index: int
    name: str
    type_index: int
    token: int

    def __str__(self) -> str:
        return self.name


@dataclass(slots=True)
class MethodDef:
    """一个方法定义。"""

    index: int
    name: str
    declaring_type: int
    return_type: int
    parameter_start: int
    generic_container: int
    token: int
    flags: int
    iflags: int
    slot: int
    parameter_count: int

    @property
    def is_static(self) -> bool:
        return bool(self.flags & METHOD_ATTR_STATIC)

    @property
    def is_virtual(self) -> bool:
        return bool(self.flags & METHOD_ATTR_VIRTUAL)

    @property
    def is_abstract(self) -> bool:
        return bool(self.flags & METHOD_ATTR_ABSTRACT)

    @property
    def is_constructor(self) -> bool:
        return self.name in (".ctor", ".cctor")

    @property
    def runtime_token(self) -> str:
        """形如 0x06000123 的元数据 token。"""
        return f"0x{self.token:08X}"


@dataclass(slots=True)
class ParameterDef:
    """一个方法参数。"""

    index: int
    name: str
    type_index: int
    token: int


@dataclass(slots=True)
class ImageDef:
    """一个程序集映像（对应一个 .dll）。"""

    index: int
    name: str
    assembly_index: int
    type_start: int
    type_count: int
    entry_point: int
    token: int


@dataclass(slots=True)
class AssemblyDef:
    """一个程序集。"""

    index: int
    name: str
    culture: str
    image_index: int
    major: int
    minor: int
    build: int
    revision: int


@dataclass
class TableEntry:
    """头部里的一张表。"""

    name: str
    offset: int
    size: int

    def __str__(self) -> str:
        return f"{self.name}@{self.offset:#x}+{self.size}"


# ---------------------------------------------------------------- 主类


class Metadata:
    """IL2CPP 全局元数据。

    用内存映射读取，避免把 17MB+ 的文件整个读进内存。
    用法：
        with Metadata(path) as meta:
            meta.search_types("Player")
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise MetadataError(f"元数据文件不存在: {self.path}")

        self._fh = open(self.path, "rb")
        try:
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        except (ValueError, OSError) as exc:
            self._fh.close()
            raise MetadataError(f"无法映射元数据文件: {exc}") from exc

        self.version: int = 0
        self.tables: dict[str, TableEntry] = {}

        self.types: list[TypeDef] = []
        self.fields: list[FieldDef] = []
        self.methods: list[MethodDef] = []
        self.parameters: list[ParameterDef] = []
        self.images: list[ImageDef] = []
        self.assemblies: list[AssemblyDef] = []

        self._string_cache: dict[int, str] = {}
        self._type_by_fullname: dict[str, int] = {}
        self._sorted_type_indexes: list[int] = []
        # byval(Il2CppType) 下标 -> typeDefinitions 下标
        self._byval_to_typedef: dict[int, int] = {}

        self._parse_header()
        self._parse_all()

    # ------------------------------------------------------ 生命周期

    def close(self) -> None:
        """释放文件映射。"""
        try:
            self._mm.close()
        finally:
            self._fh.close()

    def __enter__(self) -> Metadata:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __repr__(self) -> str:
        return (f"<Metadata v{self.version} types={len(self.types):,} "
                f"methods={len(self.methods):,}>")

    # ------------------------------------------------------ 底层读取

    def _u32(self, offset: int) -> int:
        return struct.unpack_from("<I", self._mm, offset)[0]

    def _i32(self, offset: int) -> int:
        return struct.unpack_from("<i", self._mm, offset)[0]

    def read_string(self, index: int) -> str:
        """按字符串表下标读取 NUL 结尾的 UTF-8 字符串。"""
        cached = self._string_cache.get(index)
        if cached is not None:
            return cached

        entry = self.tables.get("string")
        if entry is None or not entry.offset:
            return ""

        offset = entry.offset + index
        if offset <= 0 or offset >= len(self._mm):
            self._string_cache[index] = ""
            return ""

        end = self._mm.find(b"\0", offset)
        if end < 0:
            end = len(self._mm)
        value = self._mm[offset:end].decode("utf-8", errors="replace")

        self._string_cache[index] = value
        return value

    # ------------------------------------------------------ 头部

    def _parse_header(self) -> None:
        if len(self._mm) < 8:
            raise MetadataError("文件过小，不是有效的 IL2CPP 元数据")

        sanity = self._u32(0)
        if sanity != METADATA_MAGIC:
            raise MetadataError(
                f"元数据魔术数错误: 0x{sanity:08X}（期望 0x{METADATA_MAGIC:08X}）。"
                "文件可能被加密、被压缩，或者根本不是 IL2CPP 元数据。"
            )

        self.version = self._i32(4)
        order = TABLE_ORDER_MODERN if self.version >= 29 else TABLE_ORDER_LEGACY

        for i, name in enumerate(order):
            off = 8 + i * 8
            if off + 8 > len(self._mm):
                break
            table_offset = self._u32(off)
            table_size = self._u32(off + 4)
            if table_offset and table_offset >= len(self._mm):
                # 越界说明版本判定有误，停止读取后续表
                warn(f"表 {name} 偏移越界（{table_offset:#x}），停止解析头部")
                break
            self.tables[name] = TableEntry(name, table_offset, table_size)

        progress(
            f"IL2CPP 元数据 v{self.version} 头部已解析："
            f"{len(self.tables)} 张表，文件 {len(self._mm):,} 字节"
        )

        self._validate_layout()

    def _validate_layout(self) -> None:
        """头部布局是否可用；不可用就**立刻**抛 `MetadataError`。

        为什么必须在这里拦（而不是等建索引时）：
        stride 推断失败后硬解会产出天文数字的计数，接着花几百秒建索引，
        最后以一句 sqlite 约束错误收场 —— 把「版本不支持」伪装成
        「数据库错误」（实测 v39：573 秒后报 `UNIQUE constraint failed`）。

        两条判据都是**硬结构不变量**，不是启发式：

            ① 版本号在实测支持区间内；
            ② 记录大小固定的表，其字节数必须能被记录大小整除。

        ② 是纯结构事实：`fields` 记录就是 12 字节，表大小除不尽只可能是
        「头部布局判定已错」。实测 v39 的 `fields` 表 24,998 字节 % 12 == 2，
        ② 单独就能拦下它。

        **故意不把「typeDefinitions 能否铺满 fields」也算进来**：那是一条
        启发式判据（`_typedef_size` 用它**推断** stride），把它当拒绝条件
        会误伤合法输入 —— 例如 fields 表为空、或类型不带字段的元数据。
        那条判据继续留在 `_typedef_size` 里以 warn 形式起作用。
        """
        if not (SUPPORTED_VERSION_MIN <= self.version <= SUPPORTED_VERSION_MAX):
            raise MetadataError(
                f"不支持的 IL2CPP 元数据版本 v{self.version}（本解析器实测支持 "
                f"v{SUPPORTED_VERSION_MIN} ~ v{SUPPORTED_VERSION_MAX}）。"
                "该版本的头部布局已经变化，硬解只会得到错误的类型/字段表，"
                "因此这里直接拒绝，不返回不可信的结果。"
                "可用替代：换用受支持版本的样本，或为该版本补充布局适配。"
            )

        broken: list[str] = []
        for name, rec_size in _FIXED_SIZE_TABLES:
            entry = self.tables.get(name)
            if entry is None or not entry.size:
                continue
            if entry.size % rec_size:
                broken.append(f"{name} 表 {entry.size} 字节不是 {rec_size} 的整数倍")

        if broken:
            raise MetadataError(
                f"IL2CPP 元数据 v{self.version} 的头部布局与解析器假设不一致："
                + "；".join(broken)
                + "。解析结果不可信，已拒绝（不返回错误的类型/字段表）。"
                "可用替代：换用受支持版本的样本，或为该版本补充布局适配。"
            )

    @property
    def _typedef_size(self) -> int:
        """推断 typeDefinitions 记录大小（自验证，不靠版本号硬猜）。

        **为什么不能只按版本号**：这里曾写死「v31 用 88、v24~v29 用 92」，
        而 92 对 v29 是错的 —— 实测 Ved（v29，19.4 MB 元数据）用 92 解析出
        21,290 个类型、Σfield_count = 650,148,194（真值 96,094），
        每个类型的 field_start/field_count 全部错位，建索引时撞上
        `UNIQUE constraint failed: fields.idx`，`open_game` 直接失败。

        **判据**：正确的 stride 必须让「字段区间恰好铺满 fields 表」——
            ∪ [field_start, field_start+field_count) == {0 .. len(fields)-1}
        且互不重叠。这条性质与版本号无关，是布局正确性的直接证据。
        （field_count == 0 的记录要跳过：它们的 field_start 是哨兵值。）

        先信版本号给出的期望值；只有在它**通不过铺满检验**时才逐个试候选，
        并在改选时 warn 出来 —— 静默换一个 stride 会让「解析错了」
        伪装成「解析对了」。
        """
        expected = TYPEDEF_SIZE_V31 if self.version >= 31 else TYPEDEF_SIZE_LEGACY
        entry = self.tables.get("typeDefinitions")
        fields_entry = self.tables.get("fields")
        if entry is None or not entry.size or fields_entry is None:
            return expected
        if self._tiles_fields(entry, expected):
            return expected
        for candidate in (88, 92, 84, 96, 80, 100):
            if candidate == expected or entry.size % candidate:
                continue
            if self._tiles_fields(entry, candidate):
                warn(
                    f"typeDefinitions 期望 {expected} 字节/记录（v{self.version}），"
                    f"但它无法铺满 fields 表；改用 {candidate} 字节/记录"
                )
                return candidate
        warn(
            f"typeDefinitions 没有任何候选 stride 能铺满 fields 表"
            f"（v{self.version}，表 {entry.size} 字节）—— 布局可能已变，"
            "解析结果不可信"
        )
        return expected

    def _tiles_fields(self, entry: TableEntry, stride: int) -> bool:
        """`stride` 是否让 typeDefinitions 的字段区间**恰好铺满** fields 表。

        只读 field_start(@32) 与 field_count(@68)，按区间排序后检查首尾相接。
        这是 O(n log n)，对 2 万条记录是毫秒级。
        """
        if stride <= 0 or entry.size % stride:
            return False
        n_fields = self.tables["fields"].size // FIELD_SIZE
        if n_fields <= 0:
            return False
        mv = self._mm
        base = entry.offset
        count = entry.size // stride
        spans: list[tuple[int, int]] = []
        for i in range(count):
            b = base + i * stride
            if b + 70 > len(mv):
                break
            start = struct.unpack_from("<i", mv, b + 32)[0]
            n = struct.unpack_from("<H", mv, b + 68)[0]
            if n == 0:
                continue                      # field_start 是哨兵值，不是区间
            if start < 0 or start + n > n_fields:
                return False                  # 越界 = 布局判定错误
            spans.append((start, start + n))
        if not spans:
            return False
        spans.sort()
        cursor = 0
        for start, end in spans:
            if start != cursor:
                return False                  # 有空洞或重叠
            cursor = end
        return cursor == n_fields

    def _method_size(self) -> int:
        """推断 methods 记录大小（用整除性自检，避免版本判定错误导致整体错位）。"""
        expected = METHOD_SIZE_V31 if self.version >= 31 else METHOD_SIZE_LEGACY
        entry = self.tables.get("methods")
        if entry and entry.size:
            if entry.size % expected == 0:
                return expected
            for candidate in (32, 36, 40, 44):
                if entry.size % candidate == 0:
                    warn(
                        f"methods 表 {entry.size} 字节不能被 {expected} 整除，"
                        f"改用 {candidate} 字节/记录"
                    )
                    return candidate
        return expected

    # ------------------------------------------------------ 解析

    def _parse_all(self) -> None:
        self._parse_images()
        self._parse_assemblies()
        self._parse_types()
        self._build_byval_map()
        # 必须在 _parse_fields 之前：字段类型解析依赖嵌套感知的全名
        self._build_nested_names()
        self._build_field_type_map()
        self._parse_fields()
        self._parse_methods()
        self._parse_parameters()
        self._build_indexes()

    def _parse_types(self) -> None:
        entry = self.tables.get("typeDefinitions")
        if entry is None or not entry.offset:
            return

        rec = self._typedef_size
        count = entry.size // rec
        if count <= 0:
            return

        mv = self._mm
        base = entry.offset
        out: list[TypeDef] = []
        append = out.append

        for i in range(count):
            b = base + i * rec
            try:
                # 16 个 int32：@0..60
                (
                    name_idx, ns_idx, byval, decl, parent, elem, generic, flags,
                    field_start, method_start, property_start, event_start,
                    nested_start, interfaces_start, vtable_start,
                    iface_offsets_start,
                ) = struct.unpack_from("<16i", mv, b)

                # 8 个 uint16：@64..78
                (
                    method_count, property_count, field_count, event_count,
                    nested_count, vtable_count, interfaces_count,
                    iface_offsets_count,
                ) = struct.unpack_from("<8H", mv, b + 64)

                token = struct.unpack_from("<I", mv, b + 84)[0]
            except struct.error:
                break

            append(
                TypeDef(
                    index=i,
                    name=self.read_string(name_idx),
                    namespace=self.read_string(ns_idx),
                    parent_il2cpp_type=parent,
                    declaring_il2cpp_type=decl,
                    nested_name="",
                    flags=flags,
                    field_start=field_start,
                    method_start=method_start,
                    property_start=property_start,
                    event_start=event_start,
                    nested_start=nested_start,
                    interfaces_start=interfaces_start,
                    vtable_start=vtable_start,
                    interface_offsets_start=iface_offsets_start,
                    byval_type=byval,
                    generic_container=generic,
                    method_count=method_count,
                    property_count=property_count,
                    field_count=field_count,
                    event_count=event_count,
                    nested_count=nested_count,
                    vtable_count=vtable_count,
                    interfaces_count=interfaces_count,
                    interface_offsets_count=iface_offsets_count,
                    token=token,
                )
            )

        self.types = out
        progress(f"解析类型 {len(self.types):,} 个")

    def _build_byval_map(self) -> None:
        """构造 byvalTypeIndex → typeDefinitions 下标 的反查表。

        这是把 parentIndex / returnType 还原成真实类型的关键（见模块文档第 2 条）。
        """
        mapping: dict[int, int] = {}
        for t in self.types:
            mapping.setdefault(t.byval_type, t.index)
        self._byval_to_typedef = mapping

    def _build_nested_names(self) -> None:
        """用 declaringTypeIndex 逐级上溯，为每个类型回填嵌套感知的全名。

        元数据对嵌套类型只存短名（`EasingType`），而 dump / Unity 日志写的是
        `TransformPingPongMotion.EasingType`。不回填前缀会导致：
          · 与 Il2CppDumper 输出比对时出现大量假差异；
          · 字段类型解析虽是同一个类型，名字却对不上。
        实测：回填后字段类型解析精度 93.80% → 100.00%（17,795 例 0 错）。

        declaringTypeIndex 指向 Il2CppType 空间，必须先经 byval 反查表换算成
        typedef 下标，否则会取到毫不相干的类型（实测 7,056 个嵌套类型全错）。
        """
        byval_to_idx = self._byval_to_typedef
        n = len(self.types)

        for t in self.types:
            if t.nested_name:
                continue

            # 逐级上溯收集短名
            parts: list[str] = []
            seen: set[int] = set()
            cur = t.index
            outer = t
            while 0 <= cur < n and cur not in seen:
                seen.add(cur)
                outer = self.types[cur]
                parts.append(outer.name)
                nxt = byval_to_idx.get(outer.declaring_il2cpp_type)
                if nxt is None or nxt == cur:
                    break
                cur = nxt

            parts.reverse()
            name = ".".join(parts)
            namespace = outer.namespace
            if namespace and not name.startswith(namespace + "."):
                name = f"{namespace}.{name}"
            t.nested_name = name

    def _build_field_type_map(self) -> None:
        """构造字段类型解析所需的表：按 byval 键排序的序列 + byval→类型名。

        背景：Il2CppFieldDefinition.typeIndex 与「方法 returnType / 参数 typeIndex /
        父类 parentIndex」**不在同一索引约定上**，不能混用同一个解析函数。

        取证（Garden of Witches，v31，对照 Il2CppDumper 输出）：
          · 字段 typeIndex 取值集合与 byvalTypeIndex 键集合的**交集恰好为 0**
            （14,457 个字段值 vs 20,072 个 byval 键，两条轨道完全互补），
            而方法 returnType 与 byval 键的重合率高达 96.64%
            —— 说明字段指向的是另一张表（运行时派生类型所在的槽位）。
          · 字段 typeIndex 与「真实类型的 byval」之差恒为非负，且分布为
            +1:65.25%  +3:12.20%  +2:8.16%  +5:3.68%  +4:3.29%  +6:2.67% …
            即字段指向的槽位总是**紧跟在**其真实类型 byval 槽位之后（含若干派生槽）。
          · 由此得出的规则「真实类型 = 小于 typeIndex 的最大 byval 键」，
            在 27,264 个可比对样本上 **100.00% 正确、0 错**，
            且 delta 从 -2 到 +22 共 24 种取值**全部**成立（不是只对 +1 成立）。

        注意：这是**经地面真值验证的启发式**，不是公开格式文档中的字段语义。
        对数组/泛型实例字段，本规则会还原出「元素/泛型实参」的类型名，
        而非 `T[]` / `List<T>` 这种完整签名（详见 resolve_field_type 的说明）。
        """
        byval_to_name: dict[int, str] = {}
        for t in self.types:
            byval_to_name.setdefault(t.byval_type, t.nested_name)
        self._byval_to_name = byval_to_name
        # 有序键序列，供「小于 ti 的最大键」二分查找
        self._byval_keys = sorted(byval_to_name)
        self._byval_root = self._byval_keys[0] if self._byval_keys else 0
        # 原始短名 → 类型名（供数组/泛型元素名兜底）
        self._short_to_name: dict[str, str] = {}
        for t in self.types:
            self._short_to_name.setdefault(t.name, t.nested_name)

    def _parse_fields(self) -> None:
        entry = self.tables.get("fields")
        if entry is None or not entry.offset:
            return

        count = entry.size // FIELD_SIZE
        mv = self._mm
        out: list[FieldDef] = []
        append = out.append

        for i in range(count):
            try:
                name_idx, type_idx, token = struct.unpack_from(
                    "<3i", mv, entry.offset + i * FIELD_SIZE
                )
            except struct.error:
                break
            append(
                FieldDef(
                    index=i,
                    name=self.read_string(name_idx),
                    type_index=type_idx,
                    token=token,
                )
            )

        self.fields = out

    def _parse_methods(self) -> None:
        r"""解析 methods 表。

        **两套记录布局，字段位置不同**（这是本文件最容易读错的地方）：

        | 偏移 | v31（36 字节/记录） | v<31（32 字节/记录） |
        |---|---|---|
        | +0  | nameIndex          | nameIndex |
        | +4  | declaringType      | declaringType |
        | +8  | returnType         | returnType |
        | +12 | **未确认**（实测恒为 `0x08000000` 一类哨兵，**不是**表索引） | parameterStart |
        | +16 | **parameterStart** | genericContainerIndex |
        | +20 | **未确认**（实测恒为 -1 一类哨兵） | token |
        | +24 | token              | flags/iflags/slot/parameterCount |
        | +28 | flags/iflags/slot/parameterCount | —— |

        v31 那两列是**实测**结论，不是查文档得来的：

        - 用游戏官方 dump（`il2cpp_dump.cs`）做真值，把 +16 当 `parameterStart`
          取参数名，3,000 个方法里 2,858 个与真值**逐字相同**，剩下的差异全部
          来自比对脚本没处理默认值参数（`bool x = false`）；把 +12 当
          `parameterStart` 则命中 **0**（它恒为 `0x08000000`，任何表里都越界）。
        - +16 在整张表上**单调不减**，取值恰好覆盖 `0..参数表条数-1`，
          无参数的方法一律为 -1；而每个方法的 +16 与前一个方法的参数个数严格
          递推吻合（`System.String.Concat` 六个重载逐条对齐）。
        - 旧代码把 +12 当 `parameterStart`、把 +16 当 `genericContainerIndex`，
          于是 `parameters_of()` 永远返回空列表 —— `class_detail` 的
          `parameters` 字段**恒为空**，Agent 只能靠猜写 `args_type`。
          `genericContainerIndex` 目前没有任何读取方，故 v31 不再解析它。

        v<31 那一列按公开的 `Il2CppMethodDefinition` 结构定义实现，本机
        **没有 v<31 的样本可验证**（9 个 IL2CPP 样本全是 v31）—— 但它与
        32 字节的记录长度自洽（token + 尾部 8 字节恰好填满），而旧代码按
        v31 的偏移去读 32 字节记录，尾部会读到记录之外，不可能正确。
        """
        entry = self.tables.get("methods")
        if entry is None or not entry.offset:
            return

        rec = self._method_size()
        count = entry.size // rec
        if count <= 0:
            return

        mv = self._mm
        base = entry.offset
        out: list[MethodDef] = []
        append = out.append

        for i in range(count):
            b = base + i * rec
            try:
                (
                    name_idx, decl, ret, f12, f16, f20,
                ) = struct.unpack_from("<6i", mv, b)
                if rec >= 36:
                    param_start, generic = f16, -1
                    token = struct.unpack_from("<I", mv, b + 24)[0]
                    tail_at = 28
                else:
                    param_start, generic, token = f12, f16, f20
                    tail_at = 24
                flags, iflags, slot, param_count = struct.unpack_from(
                    "<4H", mv, b + tail_at)
            except struct.error:
                break
            append(
                MethodDef(
                    index=i,
                    name=self.read_string(name_idx),
                    declaring_type=decl,
                    return_type=ret,
                    parameter_start=param_start,
                    generic_container=generic,
                    token=token,
                    flags=flags,
                    iflags=iflags,
                    slot=slot,
                    parameter_count=param_count,
                )
            )

        self.methods = out
        progress(f"解析方法 {len(self.methods):,} 个")

    def _parse_parameters(self) -> None:
        entry = self.tables.get("parameters")
        if entry is None or not entry.offset:
            return

        count = entry.size // PARAMETER_SIZE
        mv = self._mm
        out: list[ParameterDef] = []
        append = out.append

        for i in range(count):
            try:
                name_idx, token, type_idx = struct.unpack_from(
                    "<3i", mv, entry.offset + i * PARAMETER_SIZE
                )
            except struct.error:
                break
            append(
                ParameterDef(
                    index=i,
                    name=self.read_string(name_idx),
                    type_index=type_idx,
                    token=token,
                )
            )

        self.parameters = out

    def _parse_images(self) -> None:
        entry = self.tables.get("images")
        if entry is None or not entry.offset:
            return

        count = entry.size // IMAGE_SIZE
        mv = self._mm
        out: list[ImageDef] = []

        for i in range(count):
            b = entry.offset + i * IMAGE_SIZE
            try:
                name_idx, asm_idx, type_start, type_count = struct.unpack_from("<4i", mv, b)
                entry_point = struct.unpack_from("<i", mv, b + 24)[0]
                token = struct.unpack_from("<I", mv, b + 28)[0]
            except struct.error:
                break
            out.append(
                ImageDef(
                    index=i,
                    name=self.read_string(name_idx),
                    assembly_index=asm_idx,
                    type_start=type_start,
                    type_count=type_count,
                    entry_point=entry_point,
                    token=token,
                )
            )

        self.images = out
        progress(f"解析程序集映像 {len(self.images)} 个")

    def _parse_assemblies(self) -> None:
        entry = self.tables.get("assemblies")
        if entry is None or not entry.offset:
            return

        count = entry.size // ASSEMBLY_SIZE
        mv = self._mm
        out: list[AssemblyDef] = []

        for i in range(count):
            b = entry.offset + i * ASSEMBLY_SIZE
            try:
                image_idx = struct.unpack_from("<i", mv, b)[0]
                # Il2CppAssemblyNameDefinition 从 @16 开始
                name_idx, culture_idx = struct.unpack_from("<2i", mv, b + 16)
                major, minor, build, revision = struct.unpack_from("<4i", mv, b + 44)
            except struct.error:
                break
            out.append(
                AssemblyDef(
                    index=i,
                    name=self.read_string(name_idx),
                    culture=self.read_string(culture_idx),
                    image_index=image_idx,
                    major=major,
                    minor=minor,
                    build=build,
                    revision=revision,
                )
            )

        self.assemblies = out

    def _build_indexes(self) -> None:
        """建立全名索引 + 按名字排序的下标列表（用于前缀/子串搜索）。"""
        index: dict[str, int] = {}
        # 先登记嵌套感知的全名（更精确），再补原始全名兜底
        for t in self.types:
            index.setdefault(t.nested_name, t.index)
        for t in self.types:
            index.setdefault(t.full_name, t.index)
        self._type_by_fullname = index
        self._sorted_type_indexes = sorted(
            range(len(self.types)), key=lambda i: self.types[i].nested_name
        )
        # P1-4：父下标 → 直接子类下标列表。**惰性**构建（见 `_children_map`），
        # 因此这里只声明，不建表 —— 不查继承关系的会话一分钱都不用付。
        self._children: dict[int, list[int]] | None = None

    # ------------------------------------------------------ 类型解析

    def resolve_il2cpp_type(self, il2cpp_type_index: int) -> str | None:
        """把 Il2CppType 下标解析成类型名（尽力而为）。

        Il2CppType 表不在元数据文件里，只有「类/结构体自身」的那些能通过
        byval 反查表还原；数组/泛型实例的 Il2CppType 由运行时生成，静态无法还原，
        此时返回 None。

        本方法适用于方法 returnType、参数 typeIndex、父类 parentIndex
        ——这些取值**直接**就是 Il2CppType 下标。
        字段请改用 resolve_field_type()，它的索引约定不同（需减 1）。
        """
        if il2cpp_type_index < 0:
            return None
        return self._byval_to_name.get(il2cpp_type_index)

    def resolve_field_type(self, field_type_index: int) -> str | None:
        """把 Il2CppFieldDefinition.typeIndex 解析成类型名。

        与 resolve_il2cpp_type 的关键差别：字段的 typeIndex 用的是**另一条索引轨道**
        （与 byvalTypeIndex 键集合交集为 0），必须先换算。
        换算规则：真实类型 = **小于 field_type_index 的最大 byval 键**。
        实测在 27,264 个真值样本上 100% 正确。

        诚实说明（重要，避免误用）：
          字段指向的槽位是其类型的「派生槽」，因此对复合类型本方法给出的是
          **元素/泛型实参的类型名**，而非完整签名。例如 dump 里的
          `System.Byte[]`、`List<ModContainer.ModElement>` 分别会解析成
          `System.Byte`、`List`1` 这类基类型名。
          需要精确的完整签名（数组维度、泛型实参表）时应走运行时
          `il2cpp_class_from_il2cpp_type` 获取，不要依赖本方法。
        """
        if field_type_index < 0:
            return None
        keys = self._byval_keys
        if not keys:
            return None
        # 小于 field_type_index 的最大键的下标
        pos = bisect.bisect_left(keys, field_type_index) - 1
        if pos < 0:
            return None
        return self._byval_to_name.get(keys[pos])

    def resolve_field_type_detail(self, field_type_index: int) -> dict:
        """解析字段类型并给出**置信度**，供 Agent 判断结果可不可直接用。

        confidence 语义（源自实测的 delta 分布）：
          exact    delta==1，字段直接指向真实类型的本体槽位。
                   实测占 65.25%，类型名可直接采信。
          derived  delta>1，字段指向该类型的某个派生槽位（数组 / 泛型实例 /
                   指针 / byref 等）。此时解析出的是**元素或泛型实参**的类型名，
                   真实签名可能是 T[]、List<T>、T* 等，需要运行时确认。
                   实测 delta 分布 +2..+22，共 34.75%。
        """
        keys = self._byval_keys
        if field_type_index < 0 or not keys:
            return {"name": None, "delta": None, "confidence": "unknown"}
        pos = bisect.bisect_left(keys, field_type_index) - 1
        if pos < 0:
            return {"name": None, "delta": None, "confidence": "unknown"}
        key = keys[pos]
        delta = field_type_index - key
        return {
            "name": self._byval_to_name.get(key),
            "byval_key": key,
            "delta": delta,
            "confidence": "exact" if delta == 1 else "derived",
        }

    def resolve_declaring_type(self, il2cpp_type_index: int) -> TypeDef | None:
        """把 declaringTypeIndex(@12) 解析成外层类型定义。"""
        if il2cpp_type_index < 0:
            return None
        idx = self._byval_to_typedef.get(il2cpp_type_index)
        return self.types[idx] if idx is not None else None

    def parent_of(self, type_def: TypeDef) -> TypeDef | None:
        """取直接基类。"""
        idx = self._byval_to_typedef.get(type_def.parent_il2cpp_type)
        return self.types[idx] if idx is not None else None

    def base_chain(self, type_def: TypeDef, limit: int = 32) -> list[TypeDef]:
        """沿继承链上溯（防环）。"""
        chain: list[TypeDef] = []
        seen: set[int] = set()
        cur = type_def
        while len(chain) < limit:
            seen.add(cur.index)
            parent = self.parent_of(cur)
            if parent is None or parent.index in seen:
                break
            chain.append(parent)
            cur = parent
        return chain

    def _children_map(self) -> dict[int, list[int]]:
        """父下标 → 直接子类下标列表，**构建一次后缓存**（P1-4）。

        这是 `derived_set` 的底座：把「每个类型问一次『你继承谁』」倒过来，
        变成「每个类型登记一次『我继承谁』」。一次 O(类型数)，
        换来之后每次继承查询都不再重复走链。
        """
        cached = self._children
        if cached is not None:
            return cached
        mapping: dict[int, list[int]] = {}
        for t in self.types:
            parent = self.parent_of(t)
            # 自环要挡掉，否则 BFS 会把自己反复压栈（虽不致命，但白转）
            if parent is not None and parent.index != t.index:
                mapping.setdefault(parent.index, []).append(t.index)
        self._children = mapping
        return mapping

    def is_enum(self, type_def: TypeDef) -> bool:
        """判定是否枚举（直接基类为 System.Enum）。

        实测准确率：1,084 / 1,084（对照 Il2CppDumper 输出）。
        """
        parent = self.parent_of(type_def)
        return parent is not None and parent.full_name == "System.Enum"

    def is_value_type(self, type_def: TypeDef) -> bool:
        """判定是否值类型（struct / enum）。"""
        parent = self.parent_of(type_def)
        return parent is not None and parent.full_name in ("System.ValueType", "System.Enum")

    def is_subclass_of(self, type_def: TypeDef, base_full_name: str) -> bool:
        """判定是否继承自指定类（含间接继承）。"""
        if type_def.nested_name == base_full_name or type_def.full_name == base_full_name:
            return True
        for t in self.base_chain(type_def):
            if t.nested_name == base_full_name or t.full_name == base_full_name:
                return True
        return False

    def derived_set(self, base_full_name: str,
                    include_self: bool = True) -> set[int]:
        """所有继承自该类的类型**下标**（含间接继承）。P1-4。

        为什么要有它：`is_subclass_of` 每次都要走一遍基类链（O(链长)），
        而 `subtype_of` / `find_derived` 要对**每个**类型各调一次，
        合起来是 O(类型数 × 链长)。反过来建一张邻接表只需一次 O(类型数)，
        之后每次查询只走真正的后代 —— 宽查询实测净增从 +20~33 ms 降下来。

        **语义与 `is_subclass_of` 逐条对齐**（这是硬要求，不是巧合）：
        `nested_name` 或 `full_name` 命中该名字的类型，连同其全部后代都算；
        同名多根时**每一个根**都算 —— 原来逐行判时这些根各自都会返回 True。
        `include_self=False` 只去掉根自身，后代照样算
        （`find_derived` 要的就是这个）。
        """
        children = self._children_map()
        out: set[int] = set()
        stack: list[int] = []
        for t in self.types:
            if t.nested_name == base_full_name or t.full_name == base_full_name:
                if include_self:
                    out.add(t.index)
                stack.append(t.index)
        while stack:
            cur = stack.pop()
            for child in children.get(cur, ()):
                if child not in out:
                    out.add(child)
                    stack.append(child)
        return out

    def image_of_type(self, type_def: TypeDef) -> str:
        """取某类型所属的程序集名（用于按 dll 过滤）。"""
        for img in self.images:
            if img.type_start <= type_def.index < img.type_start + img.type_count:
                return img.name
        return ""

    # ------------------------------------------------------ 成员访问

    def fields_of(self, type_def: TypeDef) -> list[FieldDef]:
        """取某类型的字段（仅自身声明的，不含继承）。"""
        start, count = type_def.field_start, type_def.field_count
        if start < 0 or count <= 0 or start + count > len(self.fields):
            return []
        return self.fields[start : start + count]

    def methods_of(self, type_def: TypeDef) -> list[MethodDef]:
        """取某类型的方法（仅自身声明的）。"""
        start, count = type_def.method_start, type_def.method_count
        if start < 0 or count <= 0 or start + count > len(self.methods):
            return []
        return self.methods[start : start + count]

    def parameters_of(self, method: MethodDef) -> list[ParameterDef]:
        """取某方法的参数。

        `parameter_start` 是 parameters 表的**索引**，无参数的方法为 -1
        （见 `_parse_methods` 里对偏移的实测说明）；越界一律当「取不到」返回空。
        """
        start, count = method.parameter_start, method.parameter_count
        if start < 0 or count <= 0 or start + count > len(self.parameters):
            return []
        return self.parameters[start : start + count]

    # ------------------------------------------------------ 搜索

    def search_types(
        self,
        pattern: str,
        limit: int = 50,
        subtype_of: str = "",
        assembly: str = "",
        exclude_compiler_generated: bool = True,
    ) -> list[TypeDef]:
        """按子串搜索类型名（大小写不敏感）。

        pattern                  子串，匹配 命名空间.类名
        subtype_of               只保留继承自该类的类型，如 UnityEngine.MonoBehaviour
        assembly                 只保留属于该程序集的类型（子串匹配 dll 名）
        exclude_compiler_generated  过滤编译器生成的名字（<...>d__、<>c 等）
        """
        needle = pattern.lower()
        out: list[TypeDef] = []

        for idx in self._sorted_type_indexes:
            t = self.types[idx]
            name = t.full_name
            if needle not in name.lower():
                continue
            if exclude_compiler_generated and (
                t.name.startswith("<") or "<>" in t.name
            ):
                continue
            if subtype_of and not self.is_subclass_of(t, subtype_of):
                continue
            if assembly and assembly.lower() not in self.image_of_type(t).lower():
                continue
            out.append(t)
            if len(out) >= limit:
                break

        return out

    def search_fields(self, pattern: str, limit: int = 50) -> list[FieldDef]:
        """按子串搜索字段名（大小写不敏感）。"""
        needle = pattern.lower()
        out: list[FieldDef] = []
        for f in self.fields:
            if needle in f.name.lower():
                out.append(f)
                if len(out) >= limit:
                    break
        return out

    def search_methods(self, pattern: str, limit: int = 50) -> list[MethodDef]:
        """按子串搜索方法名（大小写不敏感）。"""
        needle = pattern.lower()
        out: list[MethodDef] = []
        for m in self.methods:
            if needle in m.name.lower():
                out.append(m)
                if len(out) >= limit:
                    break
        return out

    def find_derived(self, base_full_name: str, limit: int = 200) -> list[TypeDef]:
        """列出所有继承自某类的类型（不含自身）。

        P1-4：改为复用 `derived_set`（邻接表惰性缓存），不再对全表逐个
        `is_subclass_of`。**结果与顺序都和原来逐条一致**：仍然按 `self.types`
        的顺序取前 `limit` 个。注意「不含自身」原来只排除了 `full_name` 命中
        的那一个，这里照旧（保留 `nested_name` 命中的根 —— 原来的行为如此，
        改语义会让 `find_derived_classes` 的结果静默变化）。
        """
        keep = self.derived_set(base_full_name)
        out: list[TypeDef] = []
        for t in self.types:
            if t.full_name == base_full_name or t.index not in keep:
                continue
            out.append(t)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------ 装配体

    def type_by_name(self, full_name: str) -> TypeDef | None:
        """按完整名精确查类型（支持嵌套全名与原始全名）。"""
        idx = self._type_by_fullname.get(full_name)
        return self.types[idx] if idx is not None else None

    def member_of(self, index: int) -> tuple[str, FieldDef | MethodDef] | None:
        """按全局下标取字段或方法（供索引层使用）。"""
        if 0 <= index < len(self.fields):
            return ("field", self.fields[index])
        shifted = index - len(self.fields)
        if 0 <= shifted < len(self.methods):
            return ("method", self.methods[shifted])
        return None

    def owner_of_field(self, field_index: int) -> TypeDef | None:
        """反查某字段属于哪个类型（线性扫描，建议走索引层）。"""
        for t in self.types:
            if t.field_start <= field_index < t.field_start + t.field_count:
                return t
        return None

    def owner_of_method(self, method_index: int) -> TypeDef | None:
        """反查某方法属于哪个类型。"""
        for t in self.types:
            if t.method_start <= method_index < t.method_start + t.method_count:
                return t
        return None

    def stats(self) -> dict:
        """解析统计（用于校验正确性）。"""
        return {
            "file": str(self.path),
            "file_size": len(self._mm),
            "version": self.version,
            "types": len(self.types),
            "fields": len(self.fields),
            "methods": len(self.methods),
            "parameters": len(self.parameters),
            "images": len(self.images),
            "assemblies": len(self.assemblies),
            "byval_map_entries": len(self._byval_to_typedef),
            "tables": {
                k: {"offset": v.offset, "size": v.size}
                for k, v in sorted(self.tables.items())
                if v.offset
            },
        }

    def validate(self) -> dict:
        """自检：Σmethod_count 应等于 methods 表条数，Σfield_count 应等于 fields 表条数。

        这是判断「记录布局是否解析正确」最有力的内部证据。
        """
        sum_methods = sum(t.method_count for t in self.types)
        sum_fields = sum(t.field_count for t in self.types)
        sum_props = sum(t.property_count for t in self.types)
        sum_nested = sum(t.nested_count for t in self.types)
        sum_vtable = sum(t.vtable_count for t in self.types)

        entry_m = self.tables.get("methods")
        entry_f = self.tables.get("fields")

        return {
            "sum_method_count": sum_methods,
            "methods_table_count": len(self.methods),
            "methods_ok": sum_methods == len(self.methods),
            "sum_field_count": sum_fields,
            "fields_table_count": len(self.fields),
            "fields_ok": sum_fields == len(self.fields),
            "sum_property_count": sum_props,
            "sum_nested_count": sum_nested,
            "sum_vtable_count": sum_vtable,
            "methods_table_bytes": entry_m.size if entry_m else 0,
            "fields_table_bytes": entry_f.size if entry_f else 0,
        }


# ---------------------------------------------------------------- 便捷入口


def load(path: str | Path) -> Metadata:
    """加载并解析元数据。"""
    return Metadata(path)


def summarize(path: str | Path) -> dict:
    """解析并返回统计信息（含自检）。"""
    with Metadata(path) as meta:
        info = meta.stats()
        info["validation"] = meta.validate()
        return info
