"""Mono (ECMA-335) 程序集元数据解析器 —— `il2cpp_meta.py` 的平行实现。

设计思想：**官方规范优先**
    Unity 的 Mono 后端直接产出标准 ECMA-335（ISO/IEC 23271）托管程序集，
    格式是公开的、列顺序是规范钉死的、没有需要猜的启发式。
    因此本模块与 `il2cpp_meta.py` 不同：那里有大量「实测推断」的规则，
    这里**不做任何猜测**——每一张表的每一列都按 ECMA-335 II.22 的定义解，
    读不出来就返回 None 或抛 MetadataError，绝不返回假数据。

对上层（`index.py` 及 MCP 工具）的价值
--------------------------------------------------
    IL2CPP 被翻译成 C++ 后丢失了托管 IL，很多信息要靠运行时补；
    Mono 程序集里则**完整保留**签名 blob、常量、泛型参数、属性、事件、P/Invoke 等信息。
    也就是说：Mono 游戏的静态分析能力实际上**强于** IL2CPP，
    前提是这里把表解对。

已验证（真实样本交叉比对，脚本 devtools/verify_mono.py）
--------------------------------------------------
    <游戏目录>\\Archon Soul_Data\\Managed\\Assembly-CSharp.dll
        （1,141,760 字节，PE32 x86，元数据 v4.0.30319，#~ 2.0，heapSizes=0x01）
    <游戏目录>\\Kinny and the Cosmic Cauldron_Data\\Managed\\Assembly-CSharp.dll
    真值来源：微软官方 System.Reflection.Metadata（devtools/mono_groundtruth/），
    完全不复用本模块的代码，逐表比对行数与成员归属。

定位元数据的路径（已实测）
--------------------------------------------------
    PE 头 → DataDirectory[14]（CLR Runtime Header）→ RVA 转文件偏移
      → CLI 头 +8 处是 MetaData 的 RVA/Size → 再次 RVA 转文件偏移
      → 该处应是 "BSJB"（0x424A5342）小端字节序 42 53 4A 42。
    注意：DataDirectory[14][1] 是 CLI 头的**大小**（实测 0x48），
    元数据大小在 CLI 头 +12 处（实测 0x8199C），不要混用。

已实现的表（ECMA-335 II.22 全 45 张，含列顺序）
--------------------------------------------------
    0x00 Module              1 ModuleGeneration u2 | 2 Name str | 3 Mvid guid | 4 EncId guid | 5 EncBaseId guid
    0x01 TypeRef             1 ResolutionScope cod | 2 Name str | 3 Namespace str
    0x02 TypeDef             1 Flags u4 | 2 Name str | 3 Namespace str | 4 Extends cod
                             5 FieldList idx(Field) | 6 MethodList idx(MethodDef)
    0x03 FieldPtr            1 Field idx(Field)                      ← 未见过，按规范实现
    0x04 Field               1 Flags u2 | 2 Name str | 3 Signature blob
    0x05 MethodPtr           1 Method idx(MethodDef)                 ← 未见过，按规范实现
    0x06 MethodDef           1 RVA u4 | 2 ImplFlags u2 | 3 Flags u2 | 4 Name str
                             5 Signature blob | 6 ParamList idx(Param)
    0x07 ParamPtr            1 Param idx(Param)                      ← 未见过，按规范实现
    0x08 Param               1 Flags u2 | 2 Sequence u2 | 3 Name str
    0x09 InterfaceImpl       1 Class idx(TypeDef) | 2 Interface cod
    0x0A MemberRef           1 Class cod | 2 Name str | 3 Signature blob
    0x0B Constant            1 Type u1 | 2 Padding u1 | 3 Parent cod | 4 Value blob
    0x0C CustomAttribute     1 Parent cod | 2 Type cod | 3 Value blob
    0x0D FieldMarshal        1 Parent cod | 2 NativeType blob
    0x0E DeclSecurity        1 Action u2 | 2 Parent cod | 3 PermissionSet blob
    0x0F ClassLayout         1 PackingSize u2 | 2 ClassSize u4 | 3 Parent idx(TypeDef)
    0x10 FieldLayout         1 Offset u4 | 2 Field idx(Field)
    0x11 StandAloneSig       1 Signature blob
    0x12 EventMap            1 Parent idx(TypeDef) | 2 EventList idx(Event)
    0x13 EventPtr            1 Event idx(Event)                      ← 未见过，按规范实现
    0x14 Event               1 EventFlags u2 | 2 Name str | 3 EventType cod
    0x15 PropertyMap         1 Parent idx(TypeDef) | 2 PropertyList idx(Property)
    0x16 PropertyPtr         1 Property idx(Property)                ← 未见过，按规范实现
    0x17 Property            1 Flags u2 | 2 Name str | 3 Type blob
    0x18 MethodSemantics     1 Semantics u2 | 2 Method idx(MethodDef) | 3 Association cod
    0x19 MethodImpl          1 Class idx(TypeDef) | 2 MethodBody cod | 3 MethodDeclaration cod
    0x1A ModuleRef           1 Name str
    0x1B TypeSpec            1 Signature blob
    0x1C ImplMap             1 MappingFlags u2 | 2 MemberForwarded cod | 3 ImportName str
                             4 ImportScope idx(ModuleRef)
    0x1D FieldRVA            1 RVA u4 | 2 Field idx(Field)
    0x1E ENCLog              1 Token u4 | 2 FuncCode u4               ← Edit&Continue，按规范实现
    0x1F ENCMap              1 Token u4                              ← Edit&Continue，按规范实现
    0x20 Assembly            1 HashAlgId u4 | 2 MajorVersion u2 | 3 MinorVersion u2
                             4 BuildNumber u2 | 5 RevisionNumber u2 | 6 Flags u4
                             7 PublicKey blob | 8 Name str | 9 Culture str
    0x21 AssemblyProcessor   1 Processor u4
    0x22 AssemblyOS          1 OSPlatformId u4 | 2 OSMajorVersion u4 | 3 OSMinorVersion u4
    0x23 AssemblyRef         1 Major..4 Revision u2×4 | 5 Flags u4 | 6 PublicKeyOrToken blob
                             7 Name str | 8 Culture str | 9 HashValue blob
    0x24 AssemblyRefProcessor 1 Processor u4 | 2 AssemblyRef idx(AssemblyRef)
    0x25 AssemblyRefOS       1 OSPlatformId u4 | 2 OSMajorVersion u4
                             3 OSMinorVersion u4 | 4 AssemblyRef idx(AssemblyRef)
    0x26 File                1 Flags u4 | 2 Name str | 3 HashValue blob
    0x27 ExportedType        1 Flags u4 | 2 TypeDefId u4 | 3 TypeName str | 4 TypeNamespace str
                             5 Implementation cod
    0x28 ManifestResource    1 Offset u4 | 2 Flags u4 | 3 Name str | 4 Implementation cod
    0x29 NestedClass         1 NestedClass idx(TypeDef) | 2 EnclosingClass idx(TypeDef)
    0x2A GenericParam        1 Number u2 | 2 Flags u2 | 3 Owner cod(TypeOrMethodDef) | 4 Name str
    0x2B MethodSpec          1 Method cod(MethodDefOrRef) | 2 Instantiation blob
    0x2C GenericParamConstraint 1 Owner idx(GenericParam) | 2 Constraint cod(TypeDefOrRef)

    说明：`#Pdb` 流（便携 PDB）与 `#-` 未压缩流：`#-` 与 `#~` 共用同一套表头布局，
    本模块两者都支持（命名 `tables_stream`）；`#Pdb` 不解析（那是调试信息，不属于元数据表）。
    若哪天遇到 FieldPtr/MethodPtr/ParamPtr/EventPtr/PropertyPtr 非空的程序集
    （增量编译产物），这几张表会被正确解析，但 `FieldList` 等间接寻址**尚未接线**，
    此时本模块会记一条警告而不是给出错误结果。

**未实现/不解析的部分（诚实清单）**
--------------------------------------------------
    · `#Pdb` 流、`#US` 用户字符串堆（只定位，不解码）
    · CustomAttribute 的 blob **内容**（不解码构造参数；只给原始 blob）
    · Constant 的完整值解码仅覆盖 ELEMENT_TYPE 基本类型（enum/字符串/数值）；
      其余返回原始字节
    · 泛型实例化签名（GENERICINST）会被解析成 ``List`1<!0>`` 这样的文本，
      但不会与 TypeDef 建索引关系
    · FieldPtr / MethodPtr / ParamPtr / EventPtr / PropertyPtr 的间接寻址未接线

关键实现细节（踩过的坑，勿改）
--------------------------------------------------
1. **版本字符串的 4 字节对齐**：根头里 versionLength 声明 12，实际字符串
   "v4.0.30319" 只有 10 字节 + NUL = 11，必须向上对齐到 4 才到 stream 数量字段。
   实测按「找到 NUL 再对齐」与「声明长度再对齐」两种算法在此样本上结果一致，
   但**按声明长度对齐才是规范**，本模块采用规范法。
2. **coded index 的宽度是「两张表最大行数」决定的**，不是「当前表行数」。
   例如 AssemblyRef 行数只有 22，但 HasCustomAttribute 里含 TypeDef(1387)，
   只要有一张引用的表超过 2^(16-tag_bits) 边界，整个 coded index 就是 4 字节。
   把它当成「该表自己的行数」是最常见的错法，会让后续所有列整体错位。
3. **TypeDef 的字段/方法归属是「下一个 TypeDef 的起点」，不是本表里的计数**：
   第 i 个 TypeDef 的字段范围是 [FieldList[i], FieldList[i+1])，
   最后一个到 Field 表末尾。**映射到 0 基下标时，行号 1 要减 1，但行号 0 要钳到 0**
   （TypeDef 第 0 行是 `<Module>` 哨兵，它的 FieldList=1 表示「第一个类型的字段从 1 开始」，
   而全局字段下标 0 属于它）。这个钳位做错会导致所有类型都少解析出第一个字段。
4. **TypeDef 第 0 行是 `<Module>`**，不是真实类型。本模块保留它（token 0x02000001），
   因为它确实是元数据的一部分，但搜索时会被 `<` 前缀规则过滤掉。
5. **Param.Sequence==0 是返回值**，不是第 0 个参数；参数按 Sequence 排序（规范要求按序存储）。
   因此 `methods` 的 `parameters_of` 已按 Sequence 过滤排序。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from ..logutil import progress, warn

# ---------------------------------------------------------------- 常量

BSJB_MAGIC = 0x424A5342

#: 元数据表名（下标即 ECMA-335 的表编号，0x00 ~ 0x2C）
TABLE_NAMES: list[str] = [
    "Module",                   # 0x00
    "TypeRef",                  # 0x01
    "TypeDef",                  # 0x02
    "FieldPtr",                 # 0x03
    "Field",                    # 0x04
    "MethodPtr",                # 0x05
    "MethodDef",                # 0x06
    "ParamPtr",                 # 0x07
    "Param",                    # 0x08
    "InterfaceImpl",            # 0x09
    "MemberRef",                # 0x0A
    "Constant",                 # 0x0B
    "CustomAttribute",          # 0x0C
    "FieldMarshal",             # 0x0D
    "DeclSecurity",             # 0x0E
    "ClassLayout",              # 0x0F
    "FieldLayout",              # 0x10
    "StandAloneSig",            # 0x11
    "EventMap",                 # 0x12
    "EventPtr",                 # 0x13
    "Event",                    # 0x14
    "PropertyMap",              # 0x15
    "PropertyPtr",              # 0x16
    "Property",                 # 0x17
    "MethodSemantics",          # 0x18
    "MethodImpl",               # 0x19
    "ModuleRef",                # 0x1A
    "TypeSpec",                 # 0x1B
    "ImplMap",                  # 0x1C
    "FieldRVA",                 # 0x1D
    "ENCLog",                   # 0x1E
    "ENCMap",                   # 0x1F
    "Assembly",                 # 0x20
    "AssemblyProcessor",        # 0x21
    "AssemblyOS",               # 0x22
    "AssemblyRef",              # 0x23
    "AssemblyRefProcessor",     # 0x24
    "AssemblyRefOS",            # 0x25
    "File",                     # 0x26
    "ExportedType",             # 0x27
    "ManifestResource",         # 0x28
    "NestedClass",              # 0x29
    "GenericParam",             # 0x2A
    "MethodSpec",               # 0x2B
    "GenericParamConstraint",   # 0x2C
]

#: 13 个 coded index 的表集合（顺序即 tag 值，None 表示该 tag 保留）
CODED_INDEX_TABLES: dict[str, tuple[str | None, ...]] = {
    # 2 位 tag
    "TypeDefOrRef": ("TypeDef", "TypeRef", "TypeSpec"),
    "HasConstant": ("Field", "Param", "Property"),
    "HasFieldMarshal": ("Field", "Param"),
    "HasDeclSecurity": ("TypeDef", "MethodDef", "Assembly"),
    "HasSemantics": ("Event", "Property"),
    "MethodDefOrRef": ("MethodDef", "MemberRef"),
    "MemberForwarded": ("Field", "MethodDef"),
    "Implementation": ("File", "AssemblyRef", "ExportedType"),
    # 3 位 tag
    "CustomAttributeType": (None, None, "MethodDef", "MemberRef", None),
    "ResolutionScope": ("Module", "ModuleRef", "AssemblyRef", "TypeRef"),
    "MemberRefParent": ("TypeDef", "TypeRef", "ModuleRef", "MethodDef", "TypeSpec"),
    # 5 位 tag
    "HasCustomAttribute": (
        "MethodDef", "Field", "TypeRef", "TypeDef", "Param", "InterfaceImpl",
        "MemberRef", "Module", "DeclSecurity", "Property", "Event",
        "StandAloneSig", "ModuleRef", "TypeSpec", "Assembly", "AssemblyRef",
        "File", "ExportedType", "ManifestResource", "GenericParam",
        "GenericParamConstraint", "MethodSpec",
    ),
    # 1 位 tag
    "TypeOrMethodDef": ("TypeDef", "MethodDef"),
    # 2 位 tag（GenericParam 的 Owner 也用 TypeOrMethodDef，1 位）
}

#: TypeDef / MethodDef / Field 的属性位（ECMA-335 II.23.1）
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

#: ELEMENT_TYPE 基本类型码（ECMA-335 II.23.1.16）
ELEMENT_TYPE_NAMES = {
    0x01: "System.Void",
    0x02: "System.Boolean",
    0x03: "System.Char",
    0x04: "System.SByte",
    0x05: "System.Byte",
    0x06: "System.Int16",
    0x07: "System.UInt16",
    0x08: "System.Int32",
    0x09: "System.UInt32",
    0x0A: "System.Int64",
    0x0B: "System.UInt64",
    0x0C: "System.Single",
    0x0D: "System.Double",
    0x0E: "System.String",
    0x16: "System.TypedReference",
    0x18: "System.IntPtr",
    0x19: "System.UIntPtr",
    0x1C: "System.Object",
}

ELEMENT_TYPE_END = 0x00
ELEMENT_TYPE_VOID = 0x01
ELEMENT_TYPE_BOOLEAN = 0x02
ELEMENT_TYPE_CHAR = 0x03
ELEMENT_TYPE_I1 = 0x04
ELEMENT_TYPE_U1 = 0x05
ELEMENT_TYPE_I2 = 0x06
ELEMENT_TYPE_U2 = 0x07
ELEMENT_TYPE_I4 = 0x08
ELEMENT_TYPE_U4 = 0x09
ELEMENT_TYPE_I8 = 0x0A
ELEMENT_TYPE_U8 = 0x0B
ELEMENT_TYPE_R4 = 0x0C
ELEMENT_TYPE_R8 = 0x0D
ELEMENT_TYPE_STRING = 0x0E
ELEMENT_TYPE_PTR = 0x0F
ELEMENT_TYPE_BYREF = 0x10
ELEMENT_TYPE_VALUETYPE = 0x11
ELEMENT_TYPE_CLASS = 0x12
ELEMENT_TYPE_VAR = 0x13
ELEMENT_TYPE_ARRAY = 0x14
ELEMENT_TYPE_GENERICINST = 0x15
ELEMENT_TYPE_TYPEDBYREF = 0x16
ELEMENT_TYPE_I = 0x18
ELEMENT_TYPE_U = 0x19
ELEMENT_TYPE_FNPTR = 0x1B
ELEMENT_TYPE_OBJECT = 0x1C
ELEMENT_TYPE_SZARRAY = 0x1D
ELEMENT_TYPE_MVAR = 0x1E
ELEMENT_TYPE_CMOD_REQD = 0x1F
ELEMENT_TYPE_CMOD_OPT = 0x20
ELEMENT_TYPE_INTERNAL = 0x21
ELEMENT_TYPE_MODIFIER = 0x40
ELEMENT_TYPE_SENTINEL = 0x41
ELEMENT_TYPE_PINNED = 0x45

#: Constant.Type 的原始值就是 ELEMENT_TYPE（ECMA-335 II.22.9）


class MetadataError(RuntimeError):
    """元数据解析失败。"""


# ---------------------------------------------------------------- 数据结构
# 全部使用 slots，避免 6000+ 字段/方法对象造成内存膨胀


@dataclass(slots=True)
class TypeDef:
    """一个类型定义（class / struct / enum / interface）。

    与 `il2cpp_meta.TypeDef` 的同名字段语义一致，另有 Mono 特有的精确信息：
    `extends_ref` 是未消解的 TypeDefOrRef 编码值，`field_list_start` 是
    ECMA-335 的 1 基行号（field_start 是换算后的 0 基下标）。
    """

    index: int
    name: str
    namespace: str
    flags: int
    # TypeDefOrRef 编码值（未消解）；0 表示无基类（<Module> / 接口）
    extends_ref: int
    # ECMA-335 原始行号（1 基）：TypeDef.FieldList / MethodList
    field_list_start: int
    method_list_start: int
    # 换算出的 0 基全局下标 + 归属数量（见模块文档「关键实现细节 3」）
    field_start: int
    field_count: int
    method_start: int
    method_count: int
    # 嵌套感知全名（Outer.Inner），由 NestedClass 表回填
    nested_name: str
    # 直接外层类型的下标；-1 表示非嵌套
    parent_index: int
    token: int

    def __str__(self) -> str:
        return self.full_name

    @property
    def full_name(self) -> str:
        """完整类型名（命名空间 + 名字）。

        与 `il2cpp_meta.TypeDef.full_name` 一致：**不含**外层前缀。
        需要与外层拼好的名字请用 `nested_name`。
        """
        return f"{self.namespace}.{self.name}" if self.namespace else self.name

    @property
    def is_nested(self) -> bool:
        return self.parent_index >= 0

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

    @property
    def runtime_token(self) -> str:
        return f"0x{self.token:08X}"


@dataclass(slots=True)
class FieldDef:
    """一个字段定义。"""

    index: int
    name: str
    flags: int
    # #Blob 堆里的字段签名下标（不是类型下标；用 resolve_field_type 消解）
    signature: int
    token: int

    def __str__(self) -> str:
        return self.name

    @property
    def is_static(self) -> bool:
        return bool(self.flags & FIELD_ATTR_STATIC)

    @property
    def is_literal(self) -> bool:
        return bool(self.flags & FIELD_ATTR_LITERAL)

    @property
    def runtime_token(self) -> str:
        return f"0x{self.token:08X}"


@dataclass(slots=True)
class MethodDef:
    """一个方法定义。"""

    index: int
    name: str
    # 方法体 RVA；0 表示无实现（abstract / P/Invoke / runtime）
    rva: int
    impl_flags: int
    flags: int
    # #Blob 堆里的方法签名下标
    signature: int
    # ECMA-335 原始行号（1 基）：MethodDef.ParamList
    param_list_start: int
    # 换算出的 0 基下标 + 归属数量
    parameter_start: int
    parameter_count: int
    # 所属类型的下标（由 TypeDef.MethodList 反推）；-1 表示归属不明
    declaring_type: int
    # 由签名 blob 解出的参数个数与泛型参数个数（解析失败为 -1）
    signature_parameter_count: int
    generic_parameter_count: int
    token: int

    def __str__(self) -> str:
        return self.name

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
        return f"0x{self.token:08X}"


@dataclass(slots=True)
class ParameterDef:
    """一个方法参数（ECMA-335 Param 表的一行）。

    注意：`sequence == 0` 的行表示**返回值**，不是参数。
    `parameters_of()` 会把它过滤掉。
    """

    index: int
    name: str
    flags: int
    sequence: int
    token: int

    def __str__(self) -> str:
        return self.name


@dataclass(slots=True)
class ImageDef:
    """一个映像 / 程序集（Mono 里一个模块就是一个 .dll）。

    `name` 是 Module 表里的模块名（如 `Assembly-CSharp.dll`），
    与 `il2cpp_meta.ImageDef.name` 的取值风格一致；
    `assembly_name` 是 Assembly 表里的程序集名（如 `Assembly-CSharp`）。
    """

    index: int
    name: str
    assembly_name: str
    module_mvid: str
    generation: int
    token: int


@dataclass(slots=True)
class AssemblyDef:
    """一个程序集定义（Assembly 表，主模块唯一）。"""

    index: int
    name: str
    culture: str
    major: int
    minor: int
    build: int
    revision: int
    flags: int
    hash_alg: int
    public_key: bytes


@dataclass(slots=True)
class AssemblyRefDef:
    """一个引用程序集（AssemblyRef 表）。"""

    index: int
    name: str
    culture: str
    major: int
    minor: int
    build: int
    revision: int
    flags: int
    public_key_or_token: bytes

    @property
    def version_text(self) -> str:
        return f"{self.major}.{self.minor}.{self.build}.{self.revision}"


@dataclass(slots=True)
class MemberRefDef:
    """一个成员引用（MemberRef 表）。"""

    index: int
    name: str
    class_ref: int
    signature: int
    token: int


@dataclass(slots=True)
class ConstantDef:
    """一个常量（Constant 表的一行）。"""

    index: int
    element_type: int
    parent_ref: int
    value: bytes
    token: int


@dataclass(slots=True)
class NestedClassDef:
    """一条嵌套关系（NestedClass 表的一行）。"""

    index: int
    nested_index: int
    enclosing_index: int


# ---------------------------------------------------------------- 主类


class Metadata:
    """Mono（ECMA-335）程序集元数据。

    用法与 `il2cpp_meta.Metadata` 一致：

        with Metadata(r"...\\Managed\\Assembly-CSharp.dll") as meta:
            for t in meta.search_types("Player", limit=10):
                print(t.nested_name, [f.name for f in meta.fields_of(t)])

    与 IL2CPP 版的关键差异：
        · 需要先过 PE + CLI 头定位元数据（IL2CPP 是独立的 .dat 文件）
        · `fields_of()` / `methods_of()` 的归属由 TypeDef.FieldList/MethodList
          的**相邻区间**算出，不是表里直接给的计数
        · 多了 `assembly_refs` / `member_refs` / `constants` / `properties` /
          `events` / `generic_params` 等 IL2CPP 元数据里没有或不便取的视图
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise MetadataError(f"程序集文件不存在: {self.path}")

        # ---- 定位元数据（PE → CLI 头 → BSJB）
        self.image = None
        self.pe_error: str | None = None
        self.clr_header_rva = 0
        self.metadata_rva = 0
        self.metadata_size = 0
        self.metadata_offset = 0
        try:
            from .pe import PEError, parse as pe_parse

            try:
                self.image = pe_parse(self.path)
            except PEError as exc:
                raise MetadataError(f"PE 解析失败: {exc}") from exc
        except ImportError:  # pragma: no cover - 仅在脱离包环境时发生
            raise MetadataError("无法导入 .pe 模块，无法定位元数据")

        if self.image is None or not self.image.is_dotnet:
            raise MetadataError(
                f"不是托管程序集（无 CLR Runtime Header）: {self.path}。"
                "IL2CPP 游戏请用 il2cpp_meta.Metadata 解析 global-metadata.dat。"
            )
        self.clr_header_rva = self.image.clr_header_rva

        self._data: bytes = self.image._data  # 复用 PE 已读入的字节，避免二次 IO

        # ---- 元数据根
        self.version_string: str = ""
        self.version: str = ""
        self.major_version: int = 0
        self.minor_version: int = 0
        self.streams: dict[str, tuple[int, int]] = {}
        self.tables_stream_name: str = ""
        self.table_version: tuple[int, int] = (0, 0)
        self.heap_sizes: int = 0
        self.valid_mask: int = 0
        self.sorted_mask: int = 0
        self.table_rows: dict[str, int] = {}
        self.table_names_present: list[int] = []

        # ---- 解析结果
        self.types: list[TypeDef] = []
        self.fields: list[FieldDef] = []
        self.methods: list[MethodDef] = []
        self.parameters: list[ParameterDef] = []
        self.images: list[ImageDef] = []
        self.assemblies: list[AssemblyDef] = []
        self.assembly_refs: list[AssemblyRefDef] = []
        self.member_refs: list[MemberRefDef] = []
        self.constants: list[ConstantDef] = []
        self.nested_classes: list[NestedClassDef] = []
        self.properties: list[dict] = []
        self.events: list[dict] = []
        self.generic_params: list[dict] = []
        self.interface_impls: list[dict] = []
        self.custom_attributes: list[dict] = []
        self.class_layouts: list[dict] = []
        self.field_layouts: list[dict] = []
        self.impl_maps: list[dict] = []
        self.field_rvas: list[dict] = []
        self.module_name: str = ""

        # ---- 缓存 / 索引
        self._string_cache: dict[int, str] = {}
        self._type_by_fullname: dict[str, int] = {}
        self._sorted_type_indexes: list[int] = []
        self._type_parent_index: list[int] = []
        self._owner_field: list[int] = []
        self._owner_method: list[int] = []
        self._table_rows: dict[str, list[dict]] = {}
        self._table_span: dict[str, tuple[int, int]] = {}
        self._assemblies_by_index: dict[int, AssemblyRefDef] = {}
        self.warnings: list[str] = []

        self._parse_root()
        self._parse_tables_header()
        self._layout_tables()
        self._parse_all()
        self._build_indexes()

    # ------------------------------------------------------ 底层读取
    #
    # 所有读取都做**越界检查**并抛 MetadataError。
    # 原因（实测踩过）：文件被截断时，元数据流的**声明大小**可能仍然完整，
    # 于是「声明范围内」的读取会直接越过真实文件末尾，抛出的
    # `struct.error: unpack_from requires a buffer of at least N bytes`
    # 是一个对调用方毫无意义的低级异常。
    # 调用方需要的是「文件被截断了」这个诊断，所以这里统一收口。

    def _ensure(self, offset: int, size: int, what: str = "数据") -> None:
        if offset < 0 or offset + size > len(self._data):
            raise MetadataError(
                f"读取{what}越界：偏移 0x{offset:X} + {size} 字节，"
                f"但文件只有 {len(self._data):,} 字节（文件可能被截断）"
            )

    def _u8(self, offset: int) -> int:
        self._ensure(offset, 1)
        return self._data[offset]

    def _u16(self, offset: int) -> int:
        self._ensure(offset, 2)
        return struct.unpack_from("<H", self._data, offset)[0]

    def _u32(self, offset: int) -> int:
        self._ensure(offset, 4)
        return struct.unpack_from("<I", self._data, offset)[0]

    def _u64(self, offset: int) -> int:
        self._ensure(offset, 8)
        return struct.unpack_from("<Q", self._data, offset)[0]

    def _rva_to_offset(self, rva: int) -> int | None:
        return self.image.rva_to_offset(rva)

    # ------------------------------------------------------ 堆访问

    def _stream_span(self, name: str) -> tuple[int, int]:
        """取某流的 (绝对文件偏移, 大小)。

        注意：流表里的 size 是**声明大小**，文件被截断时它会大于真实剩余字节。
        因此这里把大小**按真实文件长度夹紧**，并在夹紧时抛错——
        宁可明确报「文件被截断」，也不要拿着假的大小越界读取。
        调用方若想拿原始声明大小，用 `self.streams[name]`。
        """
        entry = self.streams.get(name)
        if entry is None:
            return (0, 0)
        offset, size = entry
        base = self.metadata_offset + offset
        if base < 0 or base > len(self._data):
            raise MetadataError(
                f"流 {name} 的偏移 0x{base:X} 超出文件长度 {len(self._data):,}"
            )
        if base + size > len(self._data):
            raise MetadataError(
                f"流 {name} 声明大小 {size:,} 字节，但文件在偏移 0x{base:X} 处"
                f"只剩 {len(self._data) - base:,} 字节（文件被截断）"
            )
        return (base, size)

    def read_string(self, index: int) -> str:
        """按 `#Strings` 堆下标读取 NUL 结尾的 UTF-8 字符串。

        下标 0 按规范表示空串。越界返回空串（并只警告一次，不抛异常，
        因为个别工具会写出越界但无害的索引）。
        """
        if index <= 0:
            return ""
        cached = self._string_cache.get(index)
        if cached is not None:
            return cached

        base, size = self._stream_span("#Strings")
        if base == 0 or index >= size:
            self._note_once(f"#Strings 下标越界: {index}（堆大小 {size}）")
            return ""
        end = self._data.find(b"\0", base + index, base + size)
        if end < 0:
            end = base + size
        value = self._data[base + index : end].decode("utf-8", errors="replace")
        self._string_cache[index] = value
        return value

    def read_blob(self, index: int) -> bytes | None:
        """按 `#Blob` 堆下标读取一段 blob（自动跳过压缩长度前缀）。

        下标 0 表示「无 blob」，返回 None。
        """
        if index <= 0:
            return None
        base, size = self._stream_span("#Blob")
        if base == 0 or index >= size:
            self._note_once(f"#Blob 下标越界: {index}（堆大小 {size}）")
            return None
        length, next_off = self._read_compressed(self._data, base + index)
        if length is None:
            self._note_once(f"#Blob 长度前缀损坏: 下标 {index}")
            return None
        if next_off + length > base + size:
            self._note_once(f"#Blob 内容越过堆末尾: 下标 {index}")
            return None
        return self._data[next_off : next_off + length]

    def read_guid(self, index: int) -> str:
        """按 `#GUID` 堆下标读取 GUID（1 基，下标 0 表示无）。"""
        if index <= 0:
            return ""
        base, size = self._stream_span("#GUID")
        off = base + (index - 1) * 16
        if base == 0 or (index - 1) * 16 + 16 > size:
            self._note_once(f"#GUID 下标越界: {index}")
            return ""
        raw = self._data[off : off + 16]
        # .NET 的 GUID 字节序：前 3 段小端
        a = struct.unpack_from("<I", raw, 0)[0]
        b = struct.unpack_from("<H", raw, 4)[0]
        c = struct.unpack_from("<H", raw, 6)[0]
        return (
            f"{a:08x}-{b:04x}-{c:04x}-"
            f"{raw[8]:02x}{raw[9]:02x}-{raw[10]:02x}{raw[11]:02x}"
            f"{raw[12]:02x}{raw[13]:02x}{raw[14]:02x}{raw[15]:02x}"
        )

    def _note_once(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
            warn(message)

    # ------------------------------------------------------ 压缩整数

    @staticmethod
    def _read_compressed(data: bytes, offset: int) -> tuple[int | None, int]:
        """解码 ECMA-335 II.23.2 的压缩无符号整数。

        规则（首字节高位决定宽度）：
            bit7 = 0 → 1 字节，(b & 0x7F)
            bit7 = 1, bit6 = 0 → 2 字节，((b & 0x3F) << 8) | b2
            bit7 = 1, bit6 = 1, bit5 = 0 → 4 字节，((b & 0x1F) << 24) | b2<<16 | b3<<8 | b4
            bit7..bit5 = 110 → 保留（返回 None）
        返回 (值, 下一个字节偏移)；解码失败返回 (None, offset)。
        """
        if offset >= len(data):
            return (None, offset)
        b0 = data[offset]
        if b0 & 0x80 == 0:
            return (b0 & 0x7F, offset + 1)
        if b0 & 0x40 == 0:
            if offset + 2 > len(data):
                return (None, offset)
            return (((b0 & 0x3F) << 8) | data[offset + 1], offset + 2)
        if b0 & 0x20 == 0:
            if offset + 4 > len(data):
                return (None, offset)
            value = (
                ((b0 & 0x1F) << 24)
                | (data[offset + 1] << 16)
                | (data[offset + 2] << 8)
                | data[offset + 3]
            )
            return (value, offset + 4)
        # 0xE0 形式（ECMA-335 II.23.2 保留 / 未定义），诚实返回失败
        return (None, offset)

    # ------------------------------------------------------ 根头

    def _parse_root(self) -> None:
        """解析 CLI 头 + 元数据根 + 流表。"""
        # CLI 头
        cli_off = self._rva_to_offset(self.clr_header_rva)
        if cli_off is None:
            raise MetadataError(f"CLR 头 RVA 无法换算为文件偏移: 0x{self.clr_header_rva:X}")
        if cli_off + 0x48 > len(self._data):
            raise MetadataError("CLR 头越界，文件可能被截断")

        cb = self._u32(cli_off)
        if cb < 0x48:
            raise MetadataError(f"CLI 头大小异常: 0x{cb:X}（应 >= 0x48）")

        self.metadata_rva = self._u32(cli_off + 8)
        self.metadata_size = self._u32(cli_off + 12)
        if not self.metadata_rva:
            raise MetadataError("CLI 头的 MetaData RVA 为 0，没有元数据")

        md_off = self._rva_to_offset(self.metadata_rva)
        if md_off is None:
            raise MetadataError(
                f"元数据 RVA 无法换算为文件偏移: 0x{self.metadata_rva:X}"
            )
        self.metadata_offset = md_off

        if md_off + 16 > len(self._data):
            raise MetadataError("元数据根越界，文件可能被截断")
        magic = self._u32(md_off)
        if magic != BSJB_MAGIC:
            raise MetadataError(
                f"元数据签名错误: 0x{magic:08X}（期望 0x{BSJB_MAGIC:08X}）"
            )

        self.major_version = self._u16(md_off + 4)
        self.minor_version = self._u16(md_off + 6)
        self.version = f"v{self.major_version}.{self.minor_version}"

        version_length = self._u32(md_off + 12)
        if version_length > 255 or md_off + 16 + version_length > len(self._data):
            raise MetadataError(f"版本字符串长度异常: {version_length}")
        raw_ver = self._data[md_off + 16 : md_off + 16 + version_length]
        self.version_string = raw_ver.split(b"\0", 1)[0].decode("utf-8", errors="replace")

        # 版本字符串按 4 字节对齐（见模块文档「关键实现细节 1」）
        cursor = md_off + 16 + ((version_length + 3) & ~3)
        if cursor + 4 > len(self._data):
            raise MetadataError("流表头越界")

        self._u16(cursor)  # flags：保留位，规范要求为 0，这里不校验
        stream_count = self._u16(cursor + 2)
        cursor += 4

        if stream_count > 64:
            raise MetadataError(f"流数量异常: {stream_count}")

        for _ in range(stream_count):
            if cursor + 8 > len(self._data):
                raise MetadataError("流表项越界")
            offset = self._u32(cursor)
            size = self._u32(cursor + 4)
            name_start = cursor + 8
            name_end = self._data.find(b"\0", name_start, name_start + 64)
            if name_end < 0:
                raise MetadataError("流名未以 NUL 结尾")
            name = self._data[name_start:name_end].decode("ascii", errors="replace")
            self.streams[name] = (offset, size)
            # 名字（含 NUL）按 4 字节对齐
            cursor = name_start + (((name_end - name_start) + 1 + 3) & ~3)

        if "#~" in self.streams:
            self.tables_stream_name = "#~"
        elif "#-" in self.streams:
            self.tables_stream_name = "#-"
        else:
            raise MetadataError(
                f"元数据中没有表流（#~ / #-）。已有流: {sorted(self.streams)}"
            )

        progress(
            f"Mono 元数据 {self.version_string} 流已解析："
            f"{sorted(self.streams)}，文件 {len(self._data):,} 字节"
        )

    # ------------------------------------------------------ 表头

    def _parse_tables_header(self) -> None:
        """解析表流头：Reserved/Major/Minor/HeapSizes/Reserved2/Valid/Sorted/行数。"""
        base, size = self._stream_span(self.tables_stream_name)
        if base == 0 or size < 24:
            raise MetadataError("表流缺失或过小")

        self.table_version = (self._u8(base + 4), self._u8(base + 5))
        self.heap_sizes = self._u8(base + 6)
        self.valid_mask = self._u64(base + 8)
        self.sorted_mask = self._u64(base + 16)

        cursor = base + 24
        for i, name in enumerate(TABLE_NAMES):
            if not (self.valid_mask >> i) & 1:
                continue
            if cursor + 4 > base + size:
                raise MetadataError(f"表 {name} 行数越界")
            self.table_rows[name] = self._u32(cursor)
            self.table_names_present.append(i)
            cursor += 4

        self._rows_end = cursor - base

        # 堆索引宽度（bit0=#Strings, bit1=#GUID, bit2=#Blob 用 4 字节）
        self._str_width = 4 if self.heap_sizes & 0x01 else 2
        self._guid_width = 4 if self.heap_sizes & 0x02 else 2
        self._blob_width = 4 if self.heap_sizes & 0x04 else 2

    # ------------------------------------------------------ 列布局

    def _index_size(self, table: str) -> int:
        """普通表索引宽度：行数 < 65536 用 2 字节，否则 4 字节。"""
        return 4 if self.table_rows.get(table, 0) >= 0x10000 else 2

    def _coded_size(self, coded_name: str) -> int:
        """coded index 宽度。

        规则：取所有被引用表的最大行数，若 max_rows < (1 << (16 - tag_bits)) 则 2 字节。
        注意 tag_bits 由**表集合长度**决定：2 张表 → 1 位，3~4 张 → 2 位，
        5~8 张 → 3 位，... 这是 ECMA-335 II.24.2.6 的编码方式，
        不能写成「按 coded 名字硬编码」。
        """
        tables = CODED_INDEX_TABLES[coded_name]
        # tag 位数 = 能容纳「表集合下标」的最少位数。
        # 注意：保留槽位（None）也占一位，所以按**集合长度**算而不是按有效表数算。
        # 例：CustomAttributeType 是 (—,—,MethodDef,MemberRef,—)，len=5 → 3 位。
        tag_bits = (len(tables) - 1).bit_length() if len(tables) > 1 else 1
        max_rows = max((self.table_rows.get(t, 0) for t in tables if t), default=0)
        return 4 if max_rows >= (1 << (16 - tag_bits)) else 2

    def _spec_width(self, spec) -> int:
        """求一个列类型的字节宽度。"""
        if isinstance(spec, tuple):
            kind, arg = spec
            if kind == "idx":
                return self._index_size(arg)
            if kind == "cod":
                return self._coded_size(arg)
            raise MetadataError(f"未知列类型: {spec}")
        if spec == "u1":
            return 1
        if spec == "u2":
            return 2
        if spec == "u4":
            return 4
        if spec == "str":
            return self._str_width
        if spec == "guid":
            return self._guid_width
        if spec == "blob":
            return self._blob_width
        raise MetadataError(f"未知列类型: {spec}")

    @staticmethod
    def _column_plan(table: str) -> tuple[tuple[str, object], ...]:
        """返回一张表的列计划：((列名, 列类型), ...)，顺序严格按 ECMA-335 II.22。"""
        idx = lambda t: ("idx", t)          # noqa: E731  普通表索引
        cod = lambda c: ("cod", c)          # noqa: E731  coded index
        plans: dict[str, tuple[tuple[str, object], ...]] = {
            "Module": (
                ("Generation", "u2"), ("Name", "str"), ("Mvid", "guid"),
                ("EncId", "guid"), ("EncBaseId", "guid"),
            ),
            "TypeRef": (
                ("ResolutionScope", cod("ResolutionScope")), ("Name", "str"),
                ("Namespace", "str"),
            ),
            "TypeDef": (
                ("Flags", "u4"), ("Name", "str"), ("Namespace", "str"),
                ("Extends", cod("TypeDefOrRef")), ("FieldList", idx("Field")),
                ("MethodList", idx("MethodDef")),
            ),
            "FieldPtr": (("Field", idx("Field")),),
            "Field": (("Flags", "u2"), ("Name", "str"), ("Signature", "blob")),
            "MethodPtr": (("Method", idx("MethodDef")),),
            "MethodDef": (
                ("RVA", "u4"), ("ImplFlags", "u2"), ("Flags", "u2"),
                ("Name", "str"), ("Signature", "blob"), ("ParamList", idx("Param")),
            ),
            "ParamPtr": (("Param", idx("Param")),),
            "Param": (("Flags", "u2"), ("Sequence", "u2"), ("Name", "str")),
            "InterfaceImpl": (
                ("Class", idx("TypeDef")), ("Interface", cod("TypeDefOrRef")),
            ),
            "MemberRef": (
                ("Class", cod("MemberRefParent")), ("Name", "str"),
                ("Signature", "blob"),
            ),
            "Constant": (
                ("Type", "u1"), ("Padding", "u1"),
                ("Parent", cod("HasConstant")), ("Value", "blob"),
            ),
            "CustomAttribute": (
                ("Parent", cod("HasCustomAttribute")),
                ("Type", cod("CustomAttributeType")), ("Value", "blob"),
            ),
            "FieldMarshal": (
                ("Parent", cod("HasFieldMarshal")), ("NativeType", "blob"),
            ),
            "DeclSecurity": (
                ("Action", "u2"), ("Parent", cod("HasDeclSecurity")),
                ("PermissionSet", "blob"),
            ),
            "ClassLayout": (
                ("PackingSize", "u2"), ("ClassSize", "u4"), ("Parent", idx("TypeDef")),
            ),
            "FieldLayout": (("Offset", "u4"), ("Field", idx("Field"))),
            "StandAloneSig": (("Signature", "blob"),),
            "EventMap": (
                ("Parent", idx("TypeDef")), ("EventList", idx("Event")),
            ),
            "EventPtr": (("Event", idx("Event")),),
            "Event": (
                ("EventFlags", "u2"), ("Name", "str"),
                ("EventType", cod("TypeDefOrRef")),
            ),
            "PropertyMap": (
                ("Parent", idx("TypeDef")), ("PropertyList", idx("Property")),
            ),
            "PropertyPtr": (("Property", idx("Property")),),
            "Property": (("Flags", "u2"), ("Name", "str"), ("Type", "blob")),
            "MethodSemantics": (
                ("Semantics", "u2"), ("Method", idx("MethodDef")),
                ("Association", cod("HasSemantics")),
            ),
            "MethodImpl": (
                ("Class", idx("TypeDef")), ("MethodBody", cod("MethodDefOrRef")),
                ("MethodDeclaration", cod("MethodDefOrRef")),
            ),
            "ModuleRef": (("Name", "str"),),
            "TypeSpec": (("Signature", "blob"),),
            "ImplMap": (
                ("MappingFlags", "u2"), ("MemberForwarded", cod("MemberForwarded")),
                ("ImportName", "str"), ("ImportScope", idx("ModuleRef")),
            ),
            "FieldRVA": (("RVA", "u4"), ("Field", idx("Field"))),
            "ENCLog": (("Token", "u4"), ("FuncCode", "u4")),
            "ENCMap": (("Token", "u4"),),
            "Assembly": (
                ("HashAlgId", "u4"), ("MajorVersion", "u2"), ("MinorVersion", "u2"),
                ("BuildNumber", "u2"), ("RevisionNumber", "u2"), ("Flags", "u4"),
                ("PublicKey", "blob"), ("Name", "str"), ("Culture", "str"),
            ),
            "AssemblyProcessor": (("Processor", "u4"),),
            "AssemblyOS": (
                ("OSPlatformId", "u4"), ("OSMajorVersion", "u4"),
                ("OSMinorVersion", "u4"),
            ),
            "AssemblyRef": (
                ("MajorVersion", "u2"), ("MinorVersion", "u2"),
                ("BuildNumber", "u2"), ("RevisionNumber", "u2"), ("Flags", "u4"),
                ("PublicKeyOrToken", "blob"), ("Name", "str"), ("Culture", "str"),
                ("HashValue", "blob"),
            ),
            "AssemblyRefProcessor": (
                ("Processor", "u4"), ("AssemblyRef", idx("AssemblyRef")),
            ),
            "AssemblyRefOS": (
                ("OSPlatformId", "u4"), ("OSMajorVersion", "u4"),
                ("OSMinorVersion", "u4"), ("AssemblyRef", idx("AssemblyRef")),
            ),
            "File": (("Flags", "u4"), ("Name", "str"), ("HashValue", "blob")),
            "ExportedType": (
                ("Flags", "u4"), ("TypeDefId", "u4"), ("TypeName", "str"),
                ("TypeNamespace", "str"),
                ("Implementation", cod("Implementation")),
            ),
            "ManifestResource": (
                ("Offset", "u4"), ("Flags", "u4"), ("Name", "str"),
                ("Implementation", cod("Implementation")),
            ),
            "NestedClass": (
                ("NestedClass", idx("TypeDef")), ("EnclosingClass", idx("TypeDef")),
            ),
            "GenericParam": (
                ("Number", "u2"), ("Flags", "u2"),
                ("Owner", cod("TypeOrMethodDef")), ("Name", "str"),
            ),
            "MethodSpec": (
                ("Method", cod("MethodDefOrRef")), ("Instantiation", "blob"),
            ),
            "GenericParamConstraint": (
                ("Owner", idx("GenericParam")), ("Constraint", cod("TypeDefOrRef")),
            ),
        }
        plan = plans.get(table)
        if plan is None:
            raise MetadataError(f"没有 {table} 表的列定义（实现遗漏）")
        return plan

    # ------------------------------------------------------ 表布局 / 解码

    def _layout_tables(self) -> None:
        """算出每张表在表流里的偏移，并整体解码成 dict 列表。"""
        base, size = self._stream_span(self.tables_stream_name)
        cursor = base + self._rows_end
        limit = base + size

        for name in TABLE_NAMES:
            rows = self.table_rows.get(name, 0)
            if rows == 0:
                continue
            plan = self._column_plan(name)
            widths = [self._spec_width(spec) for _, spec in plan]
            row_size = sum(widths)
            needed = row_size * rows
            if cursor + needed > limit:
                raise MetadataError(
                    f"表 {name} 数据越界（需要 {needed} 字节，"
                    f"剩余 {limit - cursor} 字节）——列布局可能算错"
                )
            self._table_span[name] = (cursor, row_size)

            out: list[dict] = []
            append = out.append
            for r in range(rows):
                off = cursor + r * row_size
                row: dict[str, object] = {}
                for (col, spec), width in zip(plan, widths):
                    if width == 1:
                        row[col] = self._data[off]
                    elif width == 2:
                        row[col] = struct.unpack_from("<H", self._data, off)[0]
                    else:
                        row[col] = struct.unpack_from("<I", self._data, off)[0]
                    off += width
                append(row)
            self._table_rows[name] = out
            cursor += needed

        self._tables_end = cursor - base
        # 表流尾部可能有对齐填充；若残留不等于填充字节，说明布局有问题
        tail = limit - cursor
        if tail > 0:
            pad = self._data[cursor:limit]
            if pad.strip(b"\0") and self.tables_stream_name == "#~":
                warn(
                    f"表流解析后仍有 {tail} 字节非零尾部数据，"
                    "表列布局可能不完全正确（请核对本模块的列定义）"
                )

    # ------------------------------------------------------ 解析主流程

    def _parse_all(self) -> None:
        self._parse_module()
        self._parse_assembly()
        self._parse_assembly_refs()
        self._parse_type_refs_and_specs()
        self._parse_types()
        self._build_type_ranges()
        self._build_nested_names()
        self._parse_fields()
        self._parse_params()
        self._parse_methods()
        self._parse_members()
        self._parse_properties_events()
        self._parse_generics()
        self._warn_pointer_tables()

    # ------------------------------------------------------ Module / Assembly

    def _parse_module(self) -> None:
        rows = self._table_rows.get("Module", [])
        assembly_name = ""
        assembly_rows = self._table_rows.get("Assembly", [])
        if assembly_rows:
            assembly_name = self.read_string(int(assembly_rows[0]["Name"]))
        self.module_name = ""
        for i, row in enumerate(rows):
            name = self.read_string(int(row["Name"]))
            self.images.append(
                ImageDef(
                    index=i,
                    name=name,
                    assembly_name=assembly_name,
                    module_mvid=self.read_guid(int(row["Mvid"])),
                    generation=int(row["Generation"]),
                    token=0x00000001 + i,
                )
            )
            if i == 0:
                self.module_name = name

    def _parse_assembly(self) -> None:
        for i, row in enumerate(self._table_rows.get("Assembly", [])):
            self.assemblies.append(
                AssemblyDef(
                    index=i,
                    name=self.read_string(int(row["Name"])),
                    culture=self.read_string(int(row["Culture"])),
                    major=int(row["MajorVersion"]),
                    minor=int(row["MinorVersion"]),
                    build=int(row["BuildNumber"]),
                    revision=int(row["RevisionNumber"]),
                    flags=int(row["Flags"]),
                    hash_alg=int(row["HashAlgId"]),
                    public_key=self.read_blob(int(row["PublicKey"])) or b"",
                )
            )

    def _parse_assembly_refs(self) -> None:
        for i, row in enumerate(self._table_rows.get("AssemblyRef", [])):
            ref = AssemblyRefDef(
                index=i,
                name=self.read_string(int(row["Name"])),
                culture=self.read_string(int(row["Culture"])),
                major=int(row["MajorVersion"]),
                minor=int(row["MinorVersion"]),
                build=int(row["BuildNumber"]),
                revision=int(row["RevisionNumber"]),
                flags=int(row["Flags"]),
                public_key_or_token=self.read_blob(int(row["PublicKeyOrToken"])) or b"",
            )
            self.assembly_refs.append(ref)
            self._assemblies_by_index[i + 1] = ref

    def _parse_type_refs_and_specs(self) -> None:
        """TypeRef 不在本模块暴露为对象，但父类消解要用它，故先建全名表。"""
        self._type_ref_fullnames: list[str] = []
        for row in self._table_rows.get("TypeRef", []):
            name = self.read_string(int(row["Name"]))
            namespace = self.read_string(int(row["Namespace"]))
            scope = int(row["ResolutionScope"])
            kind, index = self._decode_coded("ResolutionScope", scope)
            full = f"{namespace}.{name}" if namespace else name
            if kind == "TypeRef":
                outer = ""
                if 1 <= index <= len(self._type_ref_fullnames):
                    outer = self._type_ref_fullnames[index - 1]
                if outer:
                    full = f"{outer}.{name}"
            # TypeRef 允许前向引用（嵌套类型的 enclosing 可能在后面），
            # 这里用「先占位、后修正」的方式处理：见下方第二遍
            self._type_ref_fullnames.append(full)

        # 第二遍修正：嵌套 TypeRef 的 enclosing 可能在下标之后
        changed = True
        guard = 0
        while changed and guard < 8:
            changed = False
            guard += 1
            for i, row in enumerate(self._table_rows.get("TypeRef", [])):
                scope = int(row["ResolutionScope"])
                kind, index = self._decode_coded("ResolutionScope", scope)
                if kind != "TypeRef" or not (1 <= index <= len(self._type_ref_fullnames)):
                    continue
                name = self.read_string(int(row["Name"]))
                outer = self._type_ref_fullnames[index - 1]
                full = f"{outer}.{name}" if outer else name
                if full != self._type_ref_fullnames[i]:
                    self._type_ref_fullnames[i] = full
                    changed = True

        # TypeSpec 的签名留作原始 blob（泛型实例化，完整展开超出本模块目标）
        self._type_spec_signatures: list[bytes] = []
        for row in self._table_rows.get("TypeSpec", []):
            self._type_spec_signatures.append(self.read_blob(int(row["Signature"])) or b"")

    # ------------------------------------------------------ 类型

    def _parse_types(self) -> None:
        for i, row in enumerate(self._table_rows.get("TypeDef", [])):
            self.types.append(
                TypeDef(
                    index=i,
                    name=self.read_string(int(row["Name"])),
                    namespace=self.read_string(int(row["Namespace"])),
                    flags=int(row["Flags"]),
                    extends_ref=int(row["Extends"]),
                    field_list_start=int(row["FieldList"]),
                    method_list_start=int(row["MethodList"]),
                    field_start=0,
                    field_count=0,
                    method_start=0,
                    method_count=0,
                    nested_name="",
                    parent_index=-1,
                    token=0x02000001 + i,
                )
            )
        progress(f"解析类型 {len(self.types):,} 个")

    def _build_type_ranges(self) -> None:
        """用 FieldList / MethodList 的相邻区间算出每个类型拥有的字段与方法。

        这是 ECMA-335 的关键逻辑（见模块文档「关键实现细节 3」）：
            范围 = [list[i] - 1, list[i+1] - 1)，最后一个到表末尾。
            行号 1 → 下标 0（减 1）；行号 0 → 钳到 0。
        """
        total_fields = len(self._table_rows.get("Field", []))
        total_methods = len(self._table_rows.get("MethodDef", []))
        n = len(self.types)

        for i, t in enumerate(self.types):
            next_field = (
                self.types[i + 1].field_list_start if i + 1 < n else total_fields + 1
            )
            start = max(0, t.field_list_start - 1)
            end = max(0, next_field - 1)
            t.field_start = min(start, total_fields)
            t.field_count = max(0, min(end, total_fields) - t.field_start)

            next_method = (
                self.types[i + 1].method_list_start if i + 1 < n else total_methods + 1
            )
            start = max(0, t.method_list_start - 1)
            end = max(0, next_method - 1)
            t.method_start = min(start, total_methods)
            t.method_count = max(0, min(end, total_methods) - t.method_start)

        # 反查表：全局字段 / 方法下标 → 所属类型
        owner_field = [-1] * total_fields
        for t in self.types:
            for k in range(t.field_start, t.field_start + t.field_count):
                owner_field[k] = t.index
        self._owner_field = owner_field

        owner_method = [-1] * total_methods
        for t in self.types:
            for k in range(t.method_start, t.method_start + t.method_count):
                owner_method[k] = t.index
        self._owner_method = owner_method

    def _build_nested_names(self) -> None:
        """用 NestedClass 表拼出 `Outer.Inner`（与 il2cpp 的 nested_name 概念一致）。

        规则**严格**照 ECMA-335 / 官方 MetadataReader 的语义，不做任何"聪明"的
        命名空间去重：

            full(i) = ns(i) + "." + name(i)                 若 i 非嵌套
            full(i) = full(enclosing(i)) + "." + name(i)    若 i 嵌套
                    （此时**忽略** i 自己的 namespace 字段——规范里嵌套类型
                      的 namespace 字段无意义）

        踩过的坑（实测修正）：早期版本在拼接后做了一次
        「名字已以 namespace 开头就不重复加」的去重，结果把
        `UiParticles.UiParticles.Geometry`（namespace=UiParticles 的类
        `UiParticles` 里嵌套的 `Geometry`）错误地压成了 `UiParticles.Geometry`，
        导致该类型全名与字段类型双双对不上官方结果。
        正确做法就是老老实实递归拼接，不去重。
        """
        n = len(self.types)
        parent = [-1] * n
        for i, row in enumerate(self._table_rows.get("NestedClass", [])):
            nested = int(row["NestedClass"])
            enclosing = int(row["EnclosingClass"])
            if not (1 <= nested <= n):
                self._note_once(f"NestedClass 表第 {i} 行 NestedClass 越界: {nested}")
                continue
            if not (1 <= enclosing <= n):
                self._note_once(f"NestedClass 表第 {i} 行 EnclosingClass 越界: {enclosing}")
                continue
            parent[nested - 1] = enclosing - 1
            self.nested_classes.append(
                NestedClassDef(index=i, nested_index=nested - 1, enclosing_index=enclosing - 1)
            )
        self._type_parent_index = parent

        # 迭代式自底向上求全名（带记忆化 + 环保护，避免病态输入导致死循环）。
        #
        # 关键点：向上收集「未缓存」的链时，若撞到一个**已缓存**的祖先，
        # 必须用它的缓存结果当 seed，而不能把链顶当成根再自己拼 namespace
        # ——否则会退化成「只用短名」。早期版本就踩了这个坑：
        # 因为外层类型通常下标更小、先被处理，内层撞上已缓存祖先时
        # 会把 `TextDictionary.<>c` 错拼成 `<>c`。
        cache: list[str | None] = [None] * n
        for i in range(n):
            if cache[i] is not None:
                continue

            chain: list[int] = []
            seen: set[int] = set()
            seed: str | None = None
            cur = i
            while True:
                if cur in seen:  # 环（异常但可能出现在损坏文件里）
                    self._note_once(f"NestedClass 检测到环，类型下标 {cur}")
                    seed = None
                    break
                seen.add(cur)
                if cache[cur] is not None:
                    # 撞到已解析的祖先：直接借它的全名作为前缀
                    seed = cache[cur]
                    break
                chain.append(cur)
                nxt = parent[cur]
                if nxt < 0 or nxt >= n:
                    # cur 是真正的根，链到此为止
                    seed = None
                    break
                cur = nxt

            # 自顶向下回填（chain 是自底向上的，故 reversed）
            base_name = seed
            for idx in reversed(chain):
                if base_name is None:
                    # 链顶端的根：用自己的 namespace
                    ns = self.types[idx].namespace
                    name = self.types[idx].name
                    base_name = f"{ns}.{name}" if ns else name
                else:
                    base_name = f"{base_name}.{self.types[idx].name}"
                cache[idx] = base_name

        for t in self.types:
            t.parent_index = parent[t.index]
            t.nested_name = cache[t.index] or (
                f"{t.namespace}.{t.name}" if t.namespace else t.name
            )

    # ------------------------------------------------------ 字段 / 参数 / 方法

    def _parse_fields(self) -> None:
        for i, row in enumerate(self._table_rows.get("Field", [])):
            self.fields.append(
                FieldDef(
                    index=i,
                    name=self.read_string(int(row["Name"])),
                    flags=int(row["Flags"]),
                    signature=int(row["Signature"]),
                    token=0x04000001 + i,
                )
            )
        progress(f"解析字段 {len(self.fields):,} 个")

    def _parse_params(self) -> None:
        for i, row in enumerate(self._table_rows.get("Param", [])):
            self.parameters.append(
                ParameterDef(
                    index=i,
                    name=self.read_string(int(row["Name"])),
                    flags=int(row["Flags"]),
                    sequence=int(row["Sequence"]),
                    token=0x08000001 + i,
                )
            )

    def _parse_methods(self) -> None:
        total_params = len(self.parameters)
        n = len(self.types)
        for i, row in enumerate(self._table_rows.get("MethodDef", [])):
            blob_index = int(row["Signature"])
            param_count, generic_count = self._signature_counts(blob_index)
            # ParamList 的相邻区间（与字段归属同一套规则）
            start = max(0, int(row["ParamList"]) - 1)
            # 找下一个方法（按表行顺序）的 ParamList
            next_rows = self._table_rows.get("MethodDef", [])
            if i + 1 < len(next_rows):
                end = max(0, int(next_rows[i + 1]["ParamList"]) - 1)
            else:
                end = total_params
            start = min(start, total_params)
            end = min(max(end, start), total_params)
            self.methods.append(
                MethodDef(
                    index=i,
                    name=self.read_string(int(row["Name"])),
                    rva=int(row["RVA"]),
                    impl_flags=int(row["ImplFlags"]),
                    flags=int(row["Flags"]),
                    signature=blob_index,
                    param_list_start=int(row["ParamList"]),
                    parameter_start=start,
                    parameter_count=end - start,
                    declaring_type=self._owner_method[i] if i < len(self._owner_method) else -1,
                    signature_parameter_count=param_count,
                    generic_parameter_count=generic_count,
                    token=0x06000001 + i,
                )
            )
        progress(f"解析方法 {len(self.methods):,} 个")

    # ------------------------------------------------------ 其它表

    def _parse_members(self) -> None:
        for i, row in enumerate(self._table_rows.get("MemberRef", [])):
            self.member_refs.append(
                MemberRefDef(
                    index=i,
                    name=self.read_string(int(row["Name"])),
                    class_ref=int(row["Class"]),
                    signature=int(row["Signature"]),
                    token=0x0A000001 + i,
                )
            )

        for i, row in enumerate(self._table_rows.get("Constant", [])):
            self.constants.append(
                ConstantDef(
                    index=i,
                    element_type=int(row["Type"]),
                    parent_ref=int(row["Parent"]),
                    value=self.read_blob(int(row["Value"])) or b"",
                    token=0x0B000001 + i,
                )
            )

        for i, row in enumerate(self._table_rows.get("CustomAttribute", [])):
            self.custom_attributes.append(
                {
                    "index": i,
                    "parent": int(row["Parent"]),
                    "type": int(row["Type"]),
                    "value": self.read_blob(int(row["Value"])) or b"",
                    "token": 0x0C000001 + i,
                }
            )

        for i, row in enumerate(self._table_rows.get("InterfaceImpl", [])):
            self.interface_impls.append(
                {
                    "index": i,
                    "class": int(row["Class"]),
                    "interface": int(row["Interface"]),
                    "token": 0x09000001 + i,
                }
            )

        for i, row in enumerate(self._table_rows.get("ClassLayout", [])):
            self.class_layouts.append(
                {
                    "index": i,
                    "packing_size": int(row["PackingSize"]),
                    "class_size": int(row["ClassSize"]),
                    "parent": int(row["Parent"]),
                    "token": 0x0F000001 + i,
                }
            )

        for i, row in enumerate(self._table_rows.get("FieldLayout", [])):
            self.field_layouts.append(
                {
                    "index": i,
                    "offset": int(row["Offset"]),
                    "field": int(row["Field"]),
                    "token": 0x10000001 + i,
                }
            )

        for i, row in enumerate(self._table_rows.get("ImplMap", [])):
            self.impl_maps.append(
                {
                    "index": i,
                    "mapping_flags": int(row["MappingFlags"]),
                    "member_forwarded": int(row["MemberForwarded"]),
                    "import_name": self.read_string(int(row["ImportName"])),
                    "import_scope": int(row["ImportScope"]),
                    "token": 0x1C000001 + i,
                }
            )

        for i, row in enumerate(self._table_rows.get("FieldRVA", [])):
            self.field_rvas.append(
                {
                    "index": i,
                    "rva": int(row["RVA"]),
                    "field": int(row["Field"]),
                    "token": 0x1D000001 + i,
                }
            )

    def _parse_properties_events(self) -> None:
        """Property / Event 的归属与字段同一套「相邻区间」规则（经 PropertyMap / EventMap）。"""
        props = self._table_rows.get("Property", [])
        prop_maps = self._table_rows.get("PropertyMap", [])
        total_props = len(props)
        for i, row in enumerate(props):
            self.properties.append(
                {
                    "index": i,
                    "flags": int(row["Flags"]),
                    "name": self.read_string(int(row["Name"])),
                    "signature": int(row["Type"]),
                    "token": 0x17000001 + i,
                }
            )
        self._property_owner = [-1] * total_props
        for i, row in enumerate(prop_maps):
            start = max(0, int(row["PropertyList"]) - 1)
            if i + 1 < len(prop_maps):
                end = max(0, int(prop_maps[i + 1]["PropertyList"]) - 1)
            else:
                end = total_props
            for k in range(min(start, total_props), min(max(end, start), total_props)):
                self._property_owner[k] = int(row["Parent"]) - 1

        events = self._table_rows.get("Event", [])
        event_maps = self._table_rows.get("EventMap", [])
        total_events = len(events)
        for i, row in enumerate(events):
            self.events.append(
                {
                    "index": i,
                    "flags": int(row["EventFlags"]),
                    "name": self.read_string(int(row["Name"])),
                    "event_type": int(row["EventType"]),
                    "token": 0x14000001 + i,
                }
            )
        self._event_owner = [-1] * total_events
        for i, row in enumerate(event_maps):
            start = max(0, int(row["EventList"]) - 1)
            if i + 1 < len(event_maps):
                end = max(0, int(event_maps[i + 1]["EventList"]) - 1)
            else:
                end = total_events
            for k in range(min(start, total_events), min(max(end, start), total_events)):
                self._event_owner[k] = int(row["Parent"]) - 1

    def _parse_generics(self) -> None:
        for i, row in enumerate(self._table_rows.get("GenericParam", [])):
            owner = int(row["Owner"])
            kind, index = self._decode_coded("TypeOrMethodDef", owner)
            self.generic_params.append(
                {
                    "index": i,
                    "number": int(row["Number"]),
                    "flags": int(row["Flags"]),
                    "owner_kind": kind,
                    "owner_index": index - 1 if index else -1,
                    "name": self.read_string(int(row["Name"])),
                    "token": 0x2A000001 + i,
                }
            )

    def _warn_pointer_tables(self) -> None:
        """指针表（增量编译间接寻址）非空时给出诚实警告。"""
        for name in ("FieldPtr", "MethodPtr", "ParamPtr", "EventPtr", "PropertyPtr"):
            if self.table_rows.get(name, 0) > 0:
                self._note_once(
                    f"{name} 表有 {self.table_rows[name]} 行（增量编译的间接寻址），"
                    "本模块尚未接线，TypeDef 的字段/方法归属可能不准确"
                )

    # ------------------------------------------------------ 签名解析

    def _signature_counts(self, blob_index: int) -> tuple[int, int]:
        """从方法签名 blob 解出 (参数个数, 泛型参数个数)；失败返回 (-1, -1)。

        方法签名（ECMA-335 II.23.2.1）：
            CallingConvention u1（bit4 = GENERIC，bit5 = HASTHIS）
            [GenericParamCount]  压缩整数，仅当 GENERIC 置位
            ParamCount           压缩整数
            RetType, Param...
        """
        blob = self.read_blob(blob_index)
        if not blob:
            return (-1, -1)
        calling = blob[0]
        pos = 1
        generic_count = 0
        if calling & 0x10:  # GENERIC
            generic_count, pos = self._read_compressed(blob, pos)
            if generic_count is None:
                return (-1, -1)
        count, _ = self._read_compressed(blob, pos)
        if count is None:
            return (-1, -1)
        return (count, generic_count)

    def resolve_field_type(self, field: FieldDef | int) -> str | None:
        """把字段签名解析成类型名（尽力而为，遵循 il2cpp 同名方法的语义）。

        与 `il2cpp_meta.resolve_field_type` 的差别：
            IL2CPP 那边是「按索引轨道推算的启发式」（对复合类型只给到元素类型名）；
            Mono 这边是**真实签名**，数组/泛型/指针都会如实出现在结果里
            （如 ``System.Byte[]``、``System.Collections.Generic.List`1<System.Int32>``）。
            因此 Mono 版没有 `type_confidence` 的不确定性——解出来就是对的。

        解析不出来（blob 损坏）返回 None，不猜。
        """
        blob_index = field.signature if isinstance(field, FieldDef) else int(field)
        return self._decode_signature(blob_index, "field")

    def resolve_property_type(self, prop: dict) -> str | None:
        """把 Property 签名解析成属性类型（PROPERTY 调用约定，II.23.2.5）。"""
        return self._decode_signature(int(prop["signature"]), "property")

    def resolve_type_spec(self, spec_index: int) -> str | None:
        """解出 TypeSpec 表第 index 行的签名文本（泛型实例化等）。"""
        if not (0 <= spec_index < len(self._type_spec_signatures)):
            return None
        blob = self._type_spec_signatures[spec_index]
        if not blob:
            return None
        name, _ = self._decode_type(blob, 0)
        return name

    def resolve_field_type_detail(self, field: FieldDef | int) -> dict:
        """与 il2cpp 版对齐的置信度包装。

        Mono 的签名是可完整解出的官方格式，成功即 `exact`；
        blob 缺失或损坏为 `unknown`。保留这个 API 只是为了让上层
        （如 index.py 的 search_fields）不必区分两种引擎。
        """
        name = self.resolve_field_type(field)
        return {
            "name": name,
            "delta": None,
            "confidence": "exact" if name is not None else "unknown",
        }

    def resolve_method_signature(self, method: MethodDef | int) -> dict | None:
        """解析方法的完整签名（返回类型 + 参数类型）。

        返回 {'calling_convention', 'has_this', 'generic_param_count',
              'return_type', 'parameter_types', 'raw'}；无法解析返回 None。
        """
        blob_index = method.signature if isinstance(method, MethodDef) else int(method)
        blob = self.read_blob(blob_index)
        if not blob:
            return None
        calling = blob[0]
        pos = 1
        generic_count = 0
        if calling & 0x10:
            generic_count, pos = self._read_compressed(blob, pos)
            if generic_count is None:
                return None
        count, pos = self._read_compressed(blob, pos)
        if count is None:
            return None

        ret, pos = self._decode_type(blob, pos)
        if ret is None:
            return None
        params: list[str] = []
        for _ in range(count):
            if pos is None or pos >= len(blob):
                break
            if blob[pos] == ELEMENT_TYPE_SENTINEL:  # vararg 的 ... 分隔
                pos += 1
            item, pos = self._decode_type(blob, pos)
            if item is None:
                break
            params.append(item)

        return {
            "calling_convention": calling,
            "has_this": bool(calling & 0x20),
            "generic_param_count": generic_count,
            "return_type": ret,
            "parameter_types": params,
            "raw": blob,
        }

    def _decode_signature(self, blob_index: int, kind: str) -> str | None:
        """解出「带调用约定前缀」的签名。

        kind 决定按哪种规范解读首字节——这一点很关键，因为
        ELEMENT_TYPE 的值与调用约定值在低位上是重叠的
        （例如 0x06 既是 FIELD 调用约定、也是 ELEMENT_TYPE_CMOD_REQD 之后的编码区）。
        所以**不能靠猜首字节**，必须由调用方告知这张表是什么签名：

            field     → FIELD (0x06)，II.23.2.4：0x06 + Type
            property  → PROPERTY (0x08)，II.23.2.5：0x08 + [HASTHIS] + ParamCount + Type + ...
            typespec  → 裸 Type，无前缀，II.23.2.12
        """
        blob = self.read_blob(blob_index)
        if not blob:
            return None

        if kind == "field":
            if blob[0] != 0x06:
                self._note_once(
                    f"字段签名首字节不是 FIELD(0x06) 而是 0x{blob[0]:02X}，"
                    "按规范跳过该字段（不猜）"
                )
                return None
            name, _ = self._decode_type(blob, 1)
            return name

        if kind == "property":
            pos = 0
            calling = blob[pos]
            pos += 1
            if not (calling & 0x08):  # PROPERTY 位（低 4 位含 HASTHIS）
                self._note_once(
                    f"属性签名调用约定异常: 0x{calling:02X}，按规范跳过（不猜）"
                )
                return None
            count, pos = self._read_compressed(blob, pos)
            if count is None:
                return None
            name, _ = self._decode_type(blob, pos)
            return name

        # 裸类型（TypeSpec）
        name, _ = self._decode_type(blob, 0)
        return name

    def _decode_type(self, blob: bytes, pos: int | None) -> tuple[str | None, int | None]:
        """递归解出一个类型签名，返回 (文本, 下一个位置)。

        完全遵循 ECMA-335 II.23.2.12；遇到未知元素类型返回 (None, None)，
        绝不返回猜测的字符串。
        """
        if pos is None or pos >= len(blob):
            return (None, None)
        et = blob[pos]
        pos += 1

        if et in ELEMENT_TYPE_NAMES:
            return (ELEMENT_TYPE_NAMES[et], pos)

        if et in (ELEMENT_TYPE_PTR, ELEMENT_TYPE_BYREF, ELEMENT_TYPE_SZARRAY,
                  ELEMENT_TYPE_PINNED):
            inner, pos = self._decode_type(blob, pos)
            if inner is None:
                return (None, None)
            suffix = {
                ELEMENT_TYPE_PTR: "*",
                ELEMENT_TYPE_BYREF: "&",
                ELEMENT_TYPE_SZARRAY: "[]",
                ELEMENT_TYPE_PINNED: "",
            }[et]
            return (f"{inner}{suffix}", pos)

        if et in (ELEMENT_TYPE_VALUETYPE, ELEMENT_TYPE_CLASS):
            token, pos = self._read_compressed(blob, pos)
            if token is None:
                return (None, None)
            name = self._type_def_or_ref_name(token)
            return (name, pos)

        if et == ELEMENT_TYPE_VAR:
            number, pos = self._read_compressed(blob, pos)
            return (f"!{number}" if number is not None else None, pos)

        if et == ELEMENT_TYPE_MVAR:
            number, pos = self._read_compressed(blob, pos)
            return (f"!!{number}" if number is not None else None, pos)

        if et == ELEMENT_TYPE_ARRAY:
            inner, pos = self._decode_type(blob, pos)
            if inner is None or pos is None:
                return (None, None)
            rank, pos = self._read_compressed(blob, pos)
            if rank is None:
                return (None, None)
            num_sizes, pos = self._read_compressed(blob, pos)
            if num_sizes is None:
                return (None, None)
            for _ in range(num_sizes):
                _, pos = self._read_compressed(blob, pos)
            num_lo_bounds, pos = self._read_compressed(blob, pos)
            if num_lo_bounds is None:
                return (None, None)
            for _ in range(num_lo_bounds):
                _, pos = self._read_compressed(blob, pos)
            return (f"{inner}[{',' * max(0, rank - 1)}]", pos)

        if et == ELEMENT_TYPE_GENERICINST:
            kind = blob[pos] if pos < len(blob) else None
            if kind not in (ELEMENT_TYPE_VALUETYPE, ELEMENT_TYPE_CLASS):
                return (None, None)
            pos += 1
            token, pos = self._read_compressed(blob, pos)
            if token is None:
                return (None, None)
            base = self._type_def_or_ref_name(token)
            arg_count, pos = self._read_compressed(blob, pos)
            if arg_count is None:
                return (None, None)
            args: list[str] = []
            for _ in range(arg_count):
                arg, pos = self._decode_type(blob, pos)
                if arg is None:
                    return (None, None)
                args.append(arg)
            if base is None:
                return (None, None)
            return (f"{base}<{', '.join(args)}>", pos)

        if et == ELEMENT_TYPE_FNPTR:
            # 函数指针：跳过整段签名，不展开（诚实：只标注存在）
            return ("fnptr", None)

        if et in (ELEMENT_TYPE_CMOD_REQD, ELEMENT_TYPE_CMOD_OPT):
            token, pos = self._read_compressed(blob, pos)
            if token is None:
                return (None, None)
            inner, pos = self._decode_type(blob, pos)
            if inner is None:
                return (None, None)
            mod = self._type_def_or_ref_name(token)
            tag = "modreq" if et == ELEMENT_TYPE_CMOD_REQD else "modopt"
            return (f"{tag}({mod}) {inner}", pos)

        if et == ELEMENT_TYPE_TYPEDBYREF:
            return ("System.TypedReference", pos)

        if et == ELEMENT_TYPE_INTERNAL:
            # 运行时内部类型，元数据里不该出现
            return (None, None)

        if et == ELEMENT_TYPE_SENTINEL:
            return (None, None)

        return (None, None)

    def _type_def_or_ref_name(self, token: int) -> str | None:
        """把 TypeDefOrRef 编码 token 解析成全名（2 位 tag，ECMA-335 II.24.2.6）。"""
        tag = token & 0x03
        index = token >> 2
        if index == 0:
            return None
        if tag == 0:  # TypeDef
            if 1 <= index <= len(self.types):
                return self.types[index - 1].nested_name
            return None
        if tag == 1:  # TypeRef
            if 1 <= index <= len(self._type_ref_fullnames):
                return self._type_ref_fullnames[index - 1]
            return None
        if tag == 2:  # TypeSpec
            if 1 <= index <= len(self._type_spec_signatures):
                blob = self._type_spec_signatures[index - 1]
                if blob:
                    name, _ = self._decode_type(blob, 0)
                    return name
            return None
        return None

    # ------------------------------------------------------ coded index

    def _decode_coded(self, coded_name: str, value: int) -> tuple[str | None, int]:
        """解 coded index，返回 (表名, 1 基行号)；保留 tag 返回 (None, 0)。"""
        tables = CODED_INDEX_TABLES[coded_name]
        tag_bits = (len(tables) - 1).bit_length() if len(tables) > 1 else 1
        tag = value & ((1 << tag_bits) - 1)
        index = value >> tag_bits
        if tag >= len(tables) or tables[tag] is None:
            return (None, 0)
        return (tables[tag], index)

    def decode_type_def_or_ref(self, value: int) -> TypeDef | None:
        """把 TypeDefOrRef 编码值解析成 TypeDef（指向 TypeRef/TypeSpec 时返回 None）。"""
        kind, index = self._decode_coded("TypeDefOrRef", value)
        if kind == "TypeDef" and 1 <= index <= len(self.types):
            return self.types[index - 1]
        return None

    def resolve_type_def_or_ref_name(self, value: int) -> str | None:
        """把 TypeDefOrRef 编码值解析成类型名（含 TypeRef / TypeSpec）。"""
        kind, index = self._decode_coded("TypeDefOrRef", value)
        if kind == "TypeDef" and 1 <= index <= len(self.types):
            return self.types[index - 1].nested_name
        if kind == "TypeRef" and 1 <= index <= len(self._type_ref_fullnames):
            return self._type_ref_fullnames[index - 1]
        if kind == "TypeSpec" and 1 <= index <= len(self._type_spec_signatures):
            blob = self._type_spec_signatures[index - 1]
            if blob:
                name, _ = self._decode_type(blob, 0)
                return name
        return None

    # ------------------------------------------------------ 索引

    def _build_indexes(self) -> None:
        index: dict[str, int] = {}
        for t in self.types:
            index.setdefault(t.nested_name, t.index)
        for t in self.types:
            index.setdefault(t.full_name, t.index)
        self._type_by_fullname = index
        self._sorted_type_indexes = sorted(
            range(len(self.types)), key=lambda i: self.types[i].nested_name
        )
        # P1-4：父下标 → 直接子类下标列表。**惰性**构建（见 `_children_map`）。
        self._children: dict[int, list[int]] | None = None

    # ------------------------------------------------------ 生命周期

    def close(self) -> None:
        """释放（本模块不持有文件句柄，保留此方法以对齐 il2cpp_meta 的用法）。"""
        self._data = b""

    def __enter__(self) -> Metadata:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"<Metadata mono {self.version_string} types={len(self.types):,} "
            f"methods={len(self.methods):,} fields={len(self.fields):,}>"
        )

    # ------------------------------------------------------ 属性

    @property
    def type_count(self) -> int:
        return len(self.types)

    @property
    def method_count(self) -> int:
        return len(self.methods)

    @property
    def field_count(self) -> int:
        return len(self.fields)

    @property
    def parameter_count(self) -> int:
        return len(self.parameters)

    @property
    def assembly_name(self) -> str:
        return self.assemblies[0].name if self.assemblies else self.module_name

    # ------------------------------------------------------ 类型解析

    def parent_of(self, type_def: TypeDef) -> TypeDef | None:
        """取直接基类。

        只在基类是**本程序集内**的 TypeDef 时返回对象；指向 TypeRef
        （如 `System.Object`、`UnityEngine.MonoBehaviour`）时返回 None
        ——这是诚实的行为：外部类型不在本文件的 TypeDef 表里，
        需要的话用 `parent_name_of()` 拿名字。
        """
        return self.decode_type_def_or_ref(type_def.extends_ref)

    def parent_name_of(self, type_def: TypeDef) -> str | None:
        """取直接基类的名字（含外部程序集的 TypeRef）。无基类返回 None。"""
        if type_def.extends_ref == 0:
            return None
        return self.resolve_type_def_or_ref_name(type_def.extends_ref)

    def base_chain(self, type_def: TypeDef, limit: int = 32) -> list[TypeDef]:
        """沿继承链上溯（只走本程序集内可解析的段，防环）。"""
        chain: list[TypeDef] = []
        seen: set[int] = {type_def.index}
        cur = type_def
        while len(chain) < limit:
            parent = self.parent_of(cur)
            if parent is None or parent.index in seen:
                break
            seen.add(parent.index)
            chain.append(parent)
            cur = parent
        return chain

    def base_chain_names(self, type_def: TypeDef, limit: int = 32) -> list[str]:
        """沿继承链上溯，**名称**版本（能跨程序集，遇到 TypeRef 就停在该名字）。"""
        names: list[str] = []
        cur = type_def
        seen: set[int] = {type_def.index}
        while len(names) < limit:
            name = self.parent_name_of(cur)
            if not name:
                break
            names.append(name)
            parent = self.parent_of(cur)
            if parent is None or parent.index in seen:
                break
            seen.add(parent.index)
            cur = parent
        return names

    def is_enum(self, type_def: TypeDef) -> bool:
        """判定是否枚举（直接基类为 System.Enum），与 il2cpp 版语义一致。"""
        return self.parent_name_of(type_def) == "System.Enum"

    def is_value_type(self, type_def: TypeDef) -> bool:
        """判定是否值类型（struct / enum）。"""
        return self.parent_name_of(type_def) in ("System.ValueType", "System.Enum")

    def is_subclass_of(self, type_def: TypeDef, base_full_name: str) -> bool:
        """判定是否继承自指定类（含间接继承，可跨程序集名称）。"""
        if type_def.nested_name == base_full_name or type_def.full_name == base_full_name:
            return True
        for name in self.base_chain_names(type_def):
            if name == base_full_name:
                return True
        return False

    def derived_set(self, base_full_name: str,
                    include_self: bool = True) -> set[int]:
        """所有继承自该类的类型**下标**（含间接继承）。P1-4。

        与 `il2cpp_meta.derived_set` 的**接口和语义一致**，但这里**不建邻接表**：

        - Mono 样本只有 1,387 个类型，逐条判定的实测净增约 **+1.6 ms**
          （落在噪声里），建表的收益抵不过多一份要维护的索引；
        - 更要紧的是 Mono 的基类是**名字**（可能指向外部程序集、解析不出
          TypeDef 下标），按名字反推子类图容易在「同名」「跨 dll」两处出错 ——
          不值得为一个量不出来的收益冒语义风险。

        保留这个方法的真正理由是**调用方不必按引擎分叉**：
        `index.py` 里 `subtype_of` 只有一条代码路径。
        """
        out: set[int] = set()
        for t in self.types:
            if not include_self and (
                t.nested_name == base_full_name or t.full_name == base_full_name
            ):
                continue
            if self.is_subclass_of(t, base_full_name):
                out.add(t.index)
        return out

    def image_of_type(self, type_def: TypeDef) -> str:
        """取某类型所属的模块名（Mono 的一个 .dll = 一个模块）。

        单模块程序集里所有类型都属于同一个映像；保留此方法是为了与
        `il2cpp_meta.image_of_type` 的调用方式对齐。
        """
        return self.module_name

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
        """取某方法的参数（**已剔除 Sequence==0 的返回值行**，按 Sequence 排序）。"""
        start, count = method.parameter_start, method.parameter_count
        if start < 0 or count <= 0 or start + count > len(self.parameters):
            return []
        rows = [p for p in self.parameters[start : start + count] if p.sequence != 0]
        rows.sort(key=lambda p: p.sequence)
        return rows

    def properties_of(self, type_def: TypeDef) -> list[dict]:
        """取某类型声明的属性。"""
        owner = getattr(self, "_property_owner", [])
        return [
            dict(p, owner=type_def.index)
            for i, p in enumerate(self.properties)
            if i < len(owner) and owner[i] == type_def.index
        ]

    def events_of(self, type_def: TypeDef) -> list[dict]:
        """取某类型声明的事件。"""
        owner = getattr(self, "_event_owner", [])
        return [
            dict(e, owner=type_def.index)
            for i, e in enumerate(self.events)
            if i < len(owner) and owner[i] == type_def.index
        ]

    def generic_parameters_of(self, type_def: TypeDef) -> list[dict]:
        """取某类型的泛型参数。"""
        return [
            g for g in self.generic_params
            if g["owner_kind"] == "TypeDef" and g["owner_index"] == type_def.index
        ]

    # ------------------------------------------------------ 常量

    def constant_of(self, member_kind: str, member_index: int):
        """取某成员（field/param/property）的常量值。

        返回 `(element_type, value)`；`value` 对基本类型是 Python 标量，
        对不认识/无法解码的类型返回原始 bytes。找不到返回 None。

        诚实说明：只解码 ELEMENT_TYPE 基本类型（II.22.9 的 Value 列规则）；
        不做 CustomAttribute blob 解码。
        """
        want_table = {"field": 0, "param": 1, "property": 2}[member_kind]
        for c in self.constants:
            kind, index = self._decode_coded("HasConstant", c.parent_ref)
            expected = {"Field": "field", "Param": "param", "Property": "property"}
            if expected.get(kind) != member_kind:
                continue
            if index - 1 != member_index:
                continue
            return (c.element_type, self._decode_constant_value(c.element_type, c.value))
        return None

    @staticmethod
    def _decode_constant_value(element_type: int, raw: bytes):
        """按 ELEMENT_TYPE 解码 Constant.Value（II.22.9）。无法解码返回原始 bytes。"""
        fmt = {
            ELEMENT_TYPE_BOOLEAN: "<B",
            ELEMENT_TYPE_I1: "<b",
            ELEMENT_TYPE_U1: "<B",
            ELEMENT_TYPE_CHAR: "<H",
            ELEMENT_TYPE_I2: "<h",
            ELEMENT_TYPE_U2: "<H",
            ELEMENT_TYPE_I4: "<i",
            ELEMENT_TYPE_U4: "<I",
            ELEMENT_TYPE_I8: "<q",
            ELEMENT_TYPE_U8: "<Q",
            ELEMENT_TYPE_R4: "<f",
            ELEMENT_TYPE_R8: "<d",
        }.get(element_type)
        if fmt is not None:
            if len(raw) < struct.calcsize(fmt):
                return raw
            value = struct.unpack_from(fmt, raw, 0)[0]
            return bool(value) if element_type == ELEMENT_TYPE_BOOLEAN else value
        if element_type == ELEMENT_TYPE_STRING:
            # 字符串常量存的是 UTF-16LE 字节
            return raw.decode("utf-16-le", errors="replace")
        if element_type in (ELEMENT_TYPE_CLASS, ELEMENT_TYPE_OBJECT):
            # null 常量长度为 0
            return None if not raw else raw
        return raw

    # ------------------------------------------------------ 搜索

    def search_types(
        self,
        pattern: str,
        limit: int = 50,
        subtype_of: str = "",
        assembly: str = "",
        exclude_compiler_generated: bool = True,
    ) -> list[TypeDef]:
        """按子串搜索类型名（大小写不敏感），签名与 il2cpp 版一致。"""
        needle = pattern.lower()
        out: list[TypeDef] = []

        for idx in self._sorted_type_indexes:
            t = self.types[idx]
            if needle and needle not in t.nested_name.lower() and needle not in t.full_name.lower():
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
        """列出所有继承自某类的类型（不含自身）。"""
        out: list[TypeDef] = []
        for t in self.types:
            if t.nested_name == base_full_name or t.full_name == base_full_name:
                continue
            if self.is_subclass_of(t, base_full_name):
                out.append(t)
                if len(out) >= limit:
                    break
        return out

    # ------------------------------------------------------ 装配体

    def type_by_name(self, full_name: str) -> TypeDef | None:
        """按完整名精确查类型（支持嵌套全名与原始全名）。"""
        idx = self._type_by_fullname.get(full_name)
        return self.types[idx] if idx is not None else None

    def member_of(self, index: int):
        """按全局下标取字段或方法（与 il2cpp 版一致：字段在前、方法在后）。"""
        if 0 <= index < len(self.fields):
            return ("field", self.fields[index])
        shifted = index - len(self.fields)
        if 0 <= shifted < len(self.methods):
            return ("method", self.methods[shifted])
        return None

    def owner_of_field(self, field_index: int) -> TypeDef | None:
        """反查某字段属于哪个类型（O(1)，走预建的反查表）。"""
        if 0 <= field_index < len(self._owner_field):
            idx = self._owner_field[field_index]
            if 0 <= idx < len(self.types):
                return self.types[idx]
        return None

    def owner_of_method(self, method_index: int) -> TypeDef | None:
        """反查某方法属于哪个类型。"""
        if 0 <= method_index < len(self._owner_method):
            idx = self._owner_method[method_index]
            if 0 <= idx < len(self.types):
                return self.types[idx]
        return None

    # ------------------------------------------------------ 统计 / 自检

    def stats(self) -> dict:
        """解析统计（用于校验正确性）。"""
        return {
            "file": str(self.path),
            "file_size": len(self._data),
            "runtime": self.image.runtime_kind if self.image else "?",
            "metadata_version_string": self.version_string,
            "metadata_root_version": self.version,
            "tables_version": list(self.table_version),
            "heap_sizes": self.heap_sizes,
            "streams": {k: {"offset": v[0], "size": v[1]} for k, v in sorted(self.streams.items())},
            "module": self.module_name,
            "assembly": self.assembly_name,
            "types": len(self.types),
            "fields": len(self.fields),
            "methods": len(self.methods),
            "parameters": len(self.parameters),
            "properties": len(self.properties),
            "events": len(self.events),
            "assembly_refs": len(self.assembly_refs),
            "member_refs": len(self.member_refs),
            "constants": len(self.constants),
            "custom_attributes": len(self.custom_attributes),
            "generic_params": len(self.generic_params),
            "nested_classes": len(self.nested_classes),
            "table_row_counts": dict(self.table_rows),
            "warnings": list(self.warnings),
        }

    def validate(self) -> dict:
        """自洽性校验：**不依赖任何外部真值**，纯内部一致性。

        最有力的三条：
            · Σ field_count  应等于 Field 表行数（ECMA-335 的相邻区间规则）
            · Σ method_count 应等于 MethodDef 表行数
            · Σ parameter_count 应等于 Param 表行数
        只要有一条不等，就说明 FieldList/MethodList/ParamList 的区间换算错了
        ——这是判断「列布局是否解析正确」最直接的内部证据。

        另外还检查签名 blob 的可用性（解不出参数个数的方法数）与嵌套名拼装情况。
        """
        sum_fields = sum(t.field_count for t in self.types)
        sum_methods = sum(t.method_count for t in self.types)
        sum_params = sum(m.parameter_count for m in self.methods)
        sum_props = sum(1 for _ in self.properties)
        sum_events = sum(1 for _ in self.events)

        table_fields = self.table_rows.get("Field", 0)
        table_methods = self.table_rows.get("MethodDef", 0)
        table_params = self.table_rows.get("Param", 0)

        # 归属覆盖率：反查表里有多少字段/方法成功归到某个类型
        owned_fields = sum(1 for x in self._owner_field if x >= 0)
        owned_methods = sum(1 for x in self._owner_method if x >= 0)

        # 签名参数个数与 Param 表区间的交叉校验。
        #
        # **重要（实测修正过的语义）**：Param 表**不是**每个参数一行。
        # 按 ECMA-335 II.22.33，只有当参数「有名字 / 有特性 / 有默认值」时才建行，
        # 否则可以没有行。因此：
        #     合法：Param 区间行数 <= 签名声明参数个数
        #     非法：Param 区间行数 >  签名声明参数个数（说明区间换算多了行）
        # 实测两个真实样本（Archon Soul 7/6,611、Kinny 3/5,221）全部是「少于」，
        # 且这些方法都是编译器生成的 `<...>g__X|N_M` 局部函数，其隐藏参数无名字。
        # 早期版本把「必须相等」当成断言，那是**断言写错了**，不是解析错了。
        sig_ok = 0
        sig_fewer = 0
        sig_more = 0
        sig_unknown = 0
        sig_more_examples: list[str] = []
        for m in self.methods:
            if m.signature_parameter_count < 0:
                sig_unknown += 1
                continue
            # Param 区间里剔除返回值后应 <= 签名参数个数
            actual = sum(1 for p in self.parameters[
                m.parameter_start : m.parameter_start + m.parameter_count
            ] if p.sequence != 0)
            if actual == m.signature_parameter_count:
                sig_ok += 1
            elif actual < m.signature_parameter_count:
                sig_fewer += 1
            else:
                sig_more += 1
                if len(sig_more_examples) < 5:
                    sig_more_examples.append(
                        f"{m.name}: Param行={actual} > 签名={m.signature_parameter_count}"
                    )

        nested_named = sum(1 for t in self.types if t.parent_index >= 0 and t.nested_name)

        return {
            "sum_field_count": sum_fields,
            "fields_table_count": table_fields,
            "fields_ok": sum_fields == table_fields,
            "sum_method_count": sum_methods,
            "methods_table_count": table_methods,
            "methods_ok": sum_methods == table_methods,
            "sum_parameter_count": sum_params,
            "params_table_count": table_params,
            "params_ok": sum_params == table_params,
            "owned_fields": owned_fields,
            "owned_methods": owned_methods,
            "owner_coverage_ok": owned_fields == table_fields and owned_methods == table_methods,
            "properties": sum_props,
            "events": sum_events,
            "nested_class_rows": self.table_rows.get("NestedClass", 0),
            "nested_named_types": nested_named,
            "nested_rows_vs_named_ok": self.table_rows.get("NestedClass", 0) == nested_named,
            "signature_params_match": sig_ok,
            "signature_params_fewer_in_param_table": sig_fewer,
            "signature_params_more_in_param_table": sig_more,
            "signature_params_more_examples": sig_more_examples,
            "signature_params_unknown": sig_unknown,
            # 合法条件：Param 表的行数绝不能**多于**签名声明的参数个数
            "signature_params_ok": sig_more == 0,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------- 便捷入口


def load(path: str | Path) -> Metadata:
    """加载并解析一个托管程序集。"""
    return Metadata(path)


def summarize(path: str | Path) -> dict:
    """解析并返回统计信息（含自检）。"""
    with Metadata(path) as meta:
        info = meta.stats()
        info["validation"] = meta.validate()
        return info
