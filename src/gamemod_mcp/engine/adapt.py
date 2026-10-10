"""引擎适配层：让索引层不必关心元数据来自 IL2CPP 还是 Mono。

为什么需要这一层
--------------------------------------------------
IL2CPP 与 Mono 是两个**根本不同**的元数据体系：

    · IL2CPP：类型/字段/方法各自的索引轨道相互独立，
      字段的 `typeIndex` 甚至不在自己的索引空间里
      （要靠「比它小的最大 byval 键」启发式推算，90% 可解）。
    · Mono：标准 ECMA-335，字段/方法的类型都编码在 `#Blob` 签名里，
      能**精确**解出完整类型名（数组、泛型、指针都对）。

两者在「类型/字段/方法/参数的归属关系」上是同构的，
差异集中在**如何表示与解析类型引用**。
把这点差异收进本模块，索引层就能共用一套代码。

设计原则
--------------------------------------------------
    · 不猜：解不出来就返回 None，由调用方决定怎么呈现
    · 不藏：把 `confidence` 透出来（IL2CPP 是启发式，Mono 是精确）
    · 不改动引擎模块：本层只做「读」的适配，不侵入 il2cpp_meta / mono_meta
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------- 引擎判定

ENGINE_IL2CPP = "il2cpp"
ENGINE_MONO = "mono"


def engine_of(meta: Any) -> str:
    """判断一个 Metadata 实例属于哪个引擎。

    用「有哪些字段」来判定，而不是 isinstance —— 这样即使上层
    做了包装（比如带缓存的代理对象）也能正确识别。
    """
    # il2cpp_meta.TypeDef 有 declaring_il2cpp_type；mono_meta 没有
    types = getattr(meta, "types", None)
    if types:
        return (ENGINE_IL2CPP if hasattr(types[0], "declaring_il2cpp_type")
                else ENGINE_MONO)
    # 没有类型时退而看 Metadata 自身的属性
    if hasattr(meta, "table_order") or hasattr(meta, "_byval_to_name"):
        return ENGINE_IL2CPP
    return ENGINE_MONO


def engine_label(meta: Any) -> str:
    """给用户看的引擎名。"""
    return "IL2CPP" if engine_of(meta) == ENGINE_IL2CPP else "Mono (.NET)"


# ---------------------------------------------------------------- 类型归属

def declaring_ref(meta: Any, t: Any) -> int | None:
    """类型的外层声明引用（用于嵌套类型还原）。

    IL2CPP 存的是 Il2CppType 索引（不是 typedef 索引），Mono 存的是
    直接的父类型下标。两者语义不同，但都只用于「有没有外层」的判断，
    索引层不做进一步解释，故统一透出原始值即可。
    """
    if engine_of(meta) == ENGINE_IL2CPP:
        return getattr(t, "declaring_il2cpp_type", None)
    pi = getattr(t, "parent_index", -1)
    return pi if pi is not None and pi >= 0 else None


def parent_ref(meta: Any, t: Any) -> int | None:
    """基类引用。

    IL2CPP 是 Il2CppType 索引；Mono 是 TypeDefOrRef 编码值，其中
    低位 2 bit 是 tag（0=TypeDef、1=TypeRef、2=TypeSpec），
    真正的行号要右移 2 位。这里只透出可比较的原始值，
    真正的基类名由 `parent_name` 拿。
    """
    if engine_of(meta) == ENGINE_IL2CPP:
        return getattr(t, "parent_il2cpp_type", None)
    return getattr(t, "extends_ref", None)


def parent_name(meta: Any, t: Any) -> str:
    """基类的可读名字（解析不出来返回空串，不猜）。"""
    try:
        if engine_of(meta) == ENGINE_IL2CPP:
            p = meta.parent_of(t)
            return p.nested_name if p is not None else ""
        # Mono 有专门的方法处理跨程序集基类（TypeRef）
        fn = getattr(meta, "parent_name_of", None)
        if fn is not None:
            return fn(t) or ""
        p = meta.parent_of(t)
        return p.nested_name if p is not None else ""
    except Exception:
        return ""


# ---------------------------------------------------------------- 字段类型

def field_type_key(meta: Any, f: Any) -> Any:
    """取出字段的「类型引用键」。

    这个键之后要交给 `resolve_field` 解析。
    IL2CPP 用 typeIndex（int）；Mono 用 #Blob 签名下标（int）。
    两者都是 int，但含义不同，所以必须成对使用这两个函数。
    """
    if engine_of(meta) == ENGINE_IL2CPP:
        return getattr(f, "type_index", None)
    return getattr(f, "signature", None)


def resolve_field(meta: Any, key: Any) -> dict:
    """把字段类型引用键解析成 {name, confidence, ...}。

    永远返回 dict，解不出来就是 name=None / confidence="unknown"。
    """
    if key is None:
        return {"name": None, "confidence": "unknown"}
    try:
        if engine_of(meta) == ENGINE_IL2CPP:
            # IL2CPP：走「比它小的最大 byval 键」启发式
            return meta.resolve_field_type_detail(int(key))
        # Mono：签名里是精确类型（FieldDef 或 blob 下标都接受）
        return meta.resolve_field_type_detail(int(key))
    except Exception:
        return {"name": None, "confidence": "unknown"}


def field_is_static(meta: Any, f: Any) -> bool | None:
    """字段是否静态。

    **IL2CPP v31 的字段记录里没有 flags 槽**，因此这里返回 None
    表示「元数据里查不到」。不要为了填满字段而猜一个 False ——
    那会让 Agent 误以为它验证过。
    Mono 有 flags，可以给出确定答案。
    """
    if engine_of(meta) == ENGINE_IL2CPP:
        return None
    flag = getattr(f, "is_static", None)
    if isinstance(flag, bool):
        return flag
    return None


# ---------------------------------------------------------------- 方法类型

def method_return_key(meta: Any, m: Any) -> Any:
    """方法返回值的类型引用键。"""
    if engine_of(meta) == ENGINE_IL2CPP:
        return getattr(m, "return_type", None)
    return getattr(m, "signature", None)


def resolve_method_return(meta: Any, key: Any) -> str | None:
    """解析方法的返回类型名。"""
    if key is None:
        return None
    try:
        if engine_of(meta) == ENGINE_IL2CPP:
            return meta.resolve_il2cpp_type(int(key))
        # Mono：签名下标 → 解出返回类型
        sig = meta.resolve_method_signature(int(key))
        return sig.get("return_type") if sig else None
    except Exception:
        return None


# ---------------------------------------------------------------- 参数

def param_type_key(meta: Any, p: Any) -> Any:
    """参数的类型引用键。

    IL2CPP 的参数记录里有 typeIndex；Mono 的 Param 表**不含类型**，
    类型只存在于方法签名里。所以 Mono 这里返回 None，
    由 `resolve_param_types` 从方法签名整体解出。
    """
    if engine_of(meta) == ENGINE_IL2CPP:
        return getattr(p, "type_index", None)
    return None


def resolve_param_types(meta: Any, m: Any) -> list[str]:
    """解出一个方法的参数类型列表（按顺序）。

    IL2CPP：逐个参数用 typeIndex 解析（delta 0，准确率高）。
    Mono：从方法签名一次性解出（精确）。
    解不出来就返回空列表，不补空位。
    """
    try:
        if engine_of(meta) == ENGINE_IL2CPP:
            out: list[str] = []
            for p in meta.parameters_of(m):
                n = meta.resolve_il2cpp_type(getattr(p, "type_index", None))
                out.append(n or "")
            return out
        sig = meta.resolve_method_signature(getattr(m, "signature", None))
        if not sig:
            return []
        return list(sig.get("parameter_types") or [])
    except Exception:
        return []


# ---------------------------------------------------------------- 映像

def image_of_type(meta: Any, t: Any) -> str:
    """类型所属的程序集/映像名。"""
    try:
        return meta.image_of_type(t) or ""
    except Exception:
        return ""


# ---------------------------------------------------------------- 能力声明

def capabilities_of(engine: str) -> dict:
    """按**引擎名**给出能力声明，不需要解析器实例。

    单独抽出来是为了让 `Index.index_info()` 能回答「这个引擎支不支持某能力」
    而**不必把整份元数据解析起来**：这个问题的答案只取决于引擎名，
    而 IL2CPP 20,072 类型的全量解析实测约 497 ms。
    （缓存命中时索引本就是按需解析的，见 `Index.meta` 属性。）
    """
    if engine == ENGINE_IL2CPP:
        return {
            "engine": ENGINE_IL2CPP,
            "label": "IL2CPP (Unity)",
            "field_offset_in_metadata": False,
            "field_type_precision": "启发式（约 90% 可解，其余需运行时确认）",
            "field_is_static_available": False,
            "method_address_in_metadata": False,
            # IL2CPP 的映像在进程启动时就全都在了，没有「等程序集加载」这件事。
            "lazy_assemblies": False,
            # IL2CPP 的 global-metadata.dat 里含引擎自己的类型，
            # 所以 `UnityEngine.*` 静态也能查到。
            "static_index_covers_frameworks": True,
            "notes": [
                "字段偏移与方法地址都必须通过运行时探针获取",
                "字段记录无 flags 槽，无法从元数据判断 static",
                "字段类型靠索引轨道推算，复合类型只给到元素类型",
            ],
        }
    return {
        "engine": ENGINE_MONO,
        "label": "Mono (.NET)",
        "field_offset_in_metadata": False,
        "field_type_precision": "精确（标准 ECMA-335 签名，含数组/泛型）",
        "field_is_static_available": True,
        # 订正：Mono 程序集的 MethodDef 表里**确实**有 RVA，但它指向 IL 字节码，
        # 不是可 Hook 的机器码（Mono 是 JIT，代码地址要运行时
        # `mono_compile_method` 才拿得到，且**不在 mono 模块内**）。
        # 这个能力位的语义是「能不能只靠元数据得到 Hook 落点」—— 答案是**不能**，
        # 所以必须是 False（曾经写成 True，与 §1 的核心断言直接矛盾）。
        "method_address_in_metadata": False,
        # 实测（Archon Soul）：注入后 0.5 秒时只有 13 个程序集，游戏起来后是 164 个。
        # 在这中间去 Hook/调用托管方法**会把游戏打崩**（矩阵实测：Archon Soul
        # 在 13 个程序集时被 Hook 并强制调用 get_frameCount，进程随后消失）。
        # 所以注入之后必须等程序集加载稳定再返回，见 engine/state.py 的
        # `_settle_assemblies`。
        "lazy_assemblies": True,
        # 静态索引只收游戏自己的程序集（Assembly-CSharp.dll），
        # `UnityEngine.*` 静态查不到（实测 4/4 样本）；这个位用来决定
        # 「未找到类」时要不要把这句话告诉调用方。详见下面 notes。
        "static_index_covers_frameworks": False,
        "notes": [
            "字段类型从 #Blob 签名精确解出",
            "方法地址必须通过运行时探针获取（mono_compile_method 触发 JIT 后的机器码地址）",
            "元数据里的 RVA 指向 IL 字节码，不是机器码，不能用来 Hook",
            "JIT 代码不在 mono 模块内，所以运行时也拿不到有意义的 RVA（只有绝对地址）",
            "可直接用 ILSpy/dnSpy 反编译拿到接近源码的 C#",
            # 实测（4 个 Mono 样本）：静态索引默认只收游戏自己的程序集
            # （Assembly-CSharp.dll，1,387 类型），`UnityEngine.*` 静态查不到 ——
            # 这不是工具坏了，是没索引那份 dll。要查就得用运行时探针。
            "静态索引默认只覆盖游戏自己的程序集（Assembly-CSharp.dll）；"
            "UnityEngine.* 等框架类型静态查不到，要用运行时探针（runtime_class_info）",
            "程序集是懒加载的：刚注入时只有十几个，游戏起来后可达 160+；"
            "注入后会自动等它加载稳定再返回",
        ],
    }


def capabilities(meta: Any) -> dict:
    """本引擎在**元数据层面**能提供什么、不能提供什么。

    这份声明会透给 Agent，让它知道哪些字段是「真的没有」
    而不是「工具坏了」。两个引擎的能力差异是客观存在的：
    """
    return capabilities_of(engine_of(meta))
