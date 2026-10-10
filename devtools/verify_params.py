#!/usr/bin/env python3
"""方法参数解析的验收探针（IL2CPP v31）。

修复前
============================================================
`class_detail` 返回的 `parameters` **恒为空列表**：

    "parameters": []          ← 每个方法都是这样

根因在 `il2cpp_meta._parse_methods`：v31 的 36 字节方法记录里，
真正的 `parameterStart` 在 **+16**，而代码读的是 **+12**（那个字段实测恒为
`0x08000000` 一类哨兵值，在 parameters 表里必然越界），于是
`parameters_of()` 永远走「越界 → 返回空」那条路。

后果不是「少一个字段」而是**误导**：Agent 看到 `parameters: []` 会认为
「这个方法没有参数」，而 `hook_method` 的 `args_type` 只能靠猜。

本探针给出三样东西
============================================================
1. **偏移证据**：整张 methods 表上 +12 / +16 的取值分布 —— 说清为什么
   +16 才是 `parameterStart`（不靠文档，靠数据）。
2. **真值精度**：用游戏官方 dump（`il2cpp_dump.cs`，Il2CppDumper 输出）
   做真值，逐个方法比对**参数名**，给出命中/不符/未找到。
3. **非空转反证**：把 `parameter_start` 改回旧偏移（+12）再跑一遍同样的
   比对，断言命中数掉到 0 —— 否则第 2 节可能测的根本不是这件事。

样本路径一律来自 `devtools/samples.py`；样本缺失时记为「无法验证」并跳过。
退出码：断言失败为 1，其余为 0。
"""

from __future__ import annotations

import os
import re
import struct
import sys
from pathlib import Path

# 中文控制台默认 GBK，打印 ✅ / ❌ 会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - 老版本 Python 没有 reconfigure
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples  # noqa: E402  （同目录）

samples.add_src_to_path()

from gamemod_mcp.engine.il2cpp_meta import Metadata  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条断言。打印观测值，失败进 FAILED。"""
    print(f"  {'✅' if ok else '❌'} {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILED.append(name)


# ---------------------------------------------------------------- 样本

GAME = samples.find_il2cpp()
META = (GAME / "Garden of Witches_Data" / "il2cpp_data"
        / "Metadata" / "global-metadata.dat") if GAME else None
DUMP = samples.find_il2cpp_dump()
#: 带参数名的 dump 真值（另一个游戏，见 samples.find_param_dump 的说明）
PARAM_TRUTH = samples.find_param_dump()

if not samples.require(META, "IL2CPP 元数据 Garden of Witches"):
    sys.exit(0)
if not samples.require(DUMP, "IL2CPP 方法参数真值 il2cpp_dump.cs"):
    sys.exit(0)


# ---------------------------------------------------------------- dump 真值

RE_TOP = re.compile(r"^(?:.*?\s)?(class|enum|interface|struct)\s+(.+?)(?:\s*:\s*(.*))?$")
RE_METHOD = re.compile(r"^\s*(?:[\w\.<>\[\],\?]+\s+)+[\w\.<>\[\],\?]+\s*\(([^)]*)\)\s*$")
RE_NS = re.compile(r"^//\s*Namespace:\s*(\S*)\s*$")

_PREFIXES = ("params ", "ref ", "out ", "in ", "this ", "scoped ")


def split_top_level(argstr: str) -> list[str]:
    """按**顶层**逗号切分参数串（泛型 `<>`、数组 `[]`、括号里的逗号不算分隔符）。"""
    out: list[str] = []
    depth = 0
    cur = ""
    for ch in argstr:
        if ch in "<[(":
            depth += 1
        elif ch in ">])":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def param_names(argstr: str) -> list[str | None]:
    """从 `float a, params object[] args, bool x = false` 里取出参数名。

    取不到名字时返回 `None`：Il2CppDumper 在**元数据本身没有参数名**时会只写
    类型（`public .ctor(string , int )`），那种参数在真值里不是名字，
    拿它当真值会造出假不符（实测 Rewired 那批类型就是这样）。
    """
    argstr = argstr.strip()
    if not argstr:
        return []
    names: list[str | None] = []
    for raw in split_top_level(argstr):
        a = raw.split("=")[0].strip()          # 去默认值
        changed = True
        while changed:                          # 去修饰前缀
            changed = False
            for p in _PREFIXES:
                if a.startswith(p):
                    a = a[len(p):].strip()
                    changed = True
        # `T[]` / `T?` 之后剩下的最后一段才是名字；只有一段说明只写了类型
        toks = a.replace("[]", " ").replace("?", " ").split()
        names.append(toks[-1] if len(toks) >= 2 else None)
    return names


def clean_line(line: str) -> str:
    """去掉行内注释与尾部的 `{ }` / `;`，只留声明本身。

    两份 dump 的写法不同，都要能吃：

        /*0x1beef40*/ System.Void .ctor(System.Byte );        ← aiMod 那份
        public static float Abs(float value) { }              ← Il2CppDumper 完整输出
        private static float[] roundPower10Single; // 0x0
    """
    t = re.sub(r"/\*.*?\*/", " ", line)
    if "//" in t:
        t = t.split("//")[0]
    t = t.strip()
    while t.endswith(";") or t.endswith("}"):
        t = t[:-1].strip()
    if t.endswith("{"):
        t = t[:-1].strip()
    return t


def method_name(decl: str) -> str:
    """从 `public static T1 Combine<T1, T2>(T1 v1, T2 v2)` 里取出 `Combine`。

    不能简单按空格切：泛型方法的名字后面跟着 `<>`，而泛型参数表里**有逗号**
    （`Combine<T1, T2>`），按逗号或空格切都会切坏（实测切出 `T2>` 这种名字）。
    所以先把 `(...)` 去掉，再**只去掉尾部**的泛型参数表，最后取最后一个
    空白分隔的 token —— 这样 `<OnInitialize>b__24_0` 这种 lambda 名字也能保住
    （它的 `>` 在中间，不是尾部）。
    """
    head = decl.split("(")[0].strip()
    if head.endswith(">"):
        depth = 0
        i = len(head) - 1
        while i >= 0:
            if head[i] == ">":
                depth += 1
            elif head[i] == "<":
                depth -= 1
                if depth == 0:
                    break
            i -= 1
        if i > 0:
            head = head[:i].rstrip()
    toks = head.split()
    return toks[-1] if toks else ""


def load_dump_methods(path: Path, with_names: bool = True) -> tuple[dict, dict]:
    """扫 dump.cs，返回 ({(类名, 方法名, 参数个数): {参数名元组…}}, 统计)。

    只按**裸类名**做键（与 `class_detail` 的 `nested_name` 对齐）：
    Il2CppDumper 对嵌套类型写的是 `Outer.Inner`，正是 nested_name 的形式。
    只认**最外层**类（depth 0）的声明，嵌套类（depth≥1）跳过 —— 两份 dump
    对嵌套类的写法不同，硬凑会引入假不符。

    值是一个**集合**：同一 (类, 方法, 参数个数) 可能有多个重载（只有参数类型
    不同），必须全部收下来，只留第一个会让「另一侧多出重载」被误报成不符。

    参数个数一律按**顶层逗号**数，与 dump 是否写参数名无关 ——
    所以同一份代码既能吃「带参数名」的 dump（核名字），
    也能吃「只有参数类型」的 dump（核个数）。
    `with_names=False` 时不解析名字（那种 dump 里名字位置是类型）。

    统计里 `skipped_noname` 是「dump 自己没写参数名、因而不能当名字真值」的组数。
    """
    out: dict[tuple[str, str, int], set] = {}
    stats = {"lines": 0, "skipped_noname": 0}
    cur: str | None = None
    ns = ""
    depth = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stats["lines"] += 1
            s = line.rstrip("\r\n")
            t = s.strip()
            if depth == 0:
                # Il2CppDumper 的完整输出把命名空间写在**上一行注释**里，
                # 类名本身是裸名；aiMod 那份则直接把全名写在类名位置。
                mns = RE_NS.match(t)
                if mns:
                    ns = mns.group(1)
                    continue
                m2 = RE_TOP.match(clean_line(s))
                if m2:
                    bare = m2.group(2)
                    # 类名已含命名空间（aiMod 写法）就不再拼一次
                    cur = f"{ns}.{bare}" if ns and not bare.startswith(f"{ns}.") else bare
                    # 「自继承」是 aiMod dump 的已知怪癖：它把某个**嵌套**类型
                    # 印成了外层的名字（实测
                    # `class System.Linq.Expressions.Expression<TDelegate> :
                    #  System.Linq.Expressions.Expression<TDelegate>`，
                    # 而元数据里 `Expression`1` 下**没有**任何嵌套类型）。
                    # 那种类体的方法不属于外层类，必须丢弃 —— 否则会给外层类
                    # 灌进 3 个并不存在的成员，把「元数据漏方法」误报出来。
                    base = (m2.group(3) or "").split(",")[0].strip()
                    if base and norm_cls(base) == norm_cls(bare):
                        cur = None
                    continue
            if t == "{":
                depth += 1
                continue
            if t.startswith("}"):
                depth -= 1
                if depth <= 0:
                    depth, cur = 0, None
                continue
            if depth == 1 and cur and "(" in t and "operator " not in t:
                decl = clean_line(s)
                m3 = RE_METHOD.match(decl)
                if m3:
                    args = m3.group(1).strip()
                    count = 0 if not args else len(split_top_level(args))
                    meth = method_name(decl)
                    if with_names:
                        got = param_names(args)
                        if any(n is None for n in got):
                            stats["skipped_noname"] += 1
                            continue
                        names = tuple(got)
                    else:
                        names = ()
                    out.setdefault((cur, meth, count), set()).add(names)
    return out, stats


# ---------------------------------------------------------------- 比对

def norm_cls(name: str) -> str:
    """把泛型实参 / 元数从类名上抹掉：`Foo<T>`、`Foo<int>`、``Foo`1`` → `Foo`。

    两侧写法不同但指的是同一批类型：dump 把泛型定义写成 `Foo<T>`，
    元数据用 CLR 的 ``Foo`1`` 记泛型定义、另有 `Foo<int>`、`Foo<string>`
    这些实例。不归一化就会出现「元数据缺 dump 的 .ctor」这类**假不符**
    （实测 339 组，全部集中在这里；其中 ``OptionElement`1`` 这类是
    反引号元数写法，只抹 `<>` 抹不掉）。
    """
    n = name
    # 只抹**尾部**的泛型实参表，且名字不能以 `<` 开头 ——
    # 否则 `<Module>`、`<>c`、`<>c__DisplayClass1_0` 这些编译器生成的名字
    # 会被抹成空串、互相撞成一个键（实测就是这么撞出一批假不符的）。
    if n.endswith(">") and "<" in n and not n.startswith("<"):
        n = n[:n.index("<")]
    return re.sub(r"`\d+$", "", n)


def compare(m: Metadata, dump: dict, offset: int) -> tuple[int, int, list[str]]:
    """把 `parameter_start` 按给定偏移重读，与 dump 真值比对参数名。

    比对单位是 **(类, 方法名, 参数个数)** 这一组：把组内所有重载的参数名列表
    收成一个**集合**再比。为什么必须按组比、而不是「一个方法对一个真值」：

    - 同名同参数个数的**重载**（只有参数类型不同，如 `SkipForm(2)` 有四个）
      在只看名字时无法一一配对 —— 按方法逐条比会把其中三个误报成「不符」
      （实测这类假不符占不符总数的绝大部分）。
    - 泛型实例（`Foo<int>` / `Foo<string>`）在元数据里是各自独立的类型定义、
      名字相同，按组取集合正好把它们折叠成一份，与 dump 的泛型定义对齐。

    记录长度用 `m._method_size()`（与解析器同一套判定），偏移由调用方给 ——
    这样「用 +12 再跑一遍」就是真正的**非空转反证**。

    Returns: (命中组数, 不符组数, 前几条不符样例)
    """
    ent = m.tables["methods"]
    rec = m._method_size()

    want: dict[tuple[str, str, int], set] = {
        (norm_cls(k[0]), k[1], k[2]): set(v) for k, v in dump.items()
    }

    got: dict[tuple[str, str, int], set] = {}
    for x in m.methods:
        t = m.types[x.declaring_type] if 0 <= x.declaring_type < len(m.types) else None
        if t is None:
            continue
        key = None
        for c in (t.nested_name, t.full_name):
            k = (norm_cls(c), x.name, x.parameter_count)
            if k in want:
                key = k
                break
        if key is None:
            continue
        start = struct.unpack_from("<i", m._mm,
                                   ent.offset + x.index * rec + offset)[0]
        if x.parameter_count <= 0:
            names: tuple = ()          # 无参数方法：真值也是空元组，不是「越界」
        elif start < 0 or start + x.parameter_count > len(m.parameters):
            names = ("<越界>",)
        else:
            names = tuple(m.parameters[start + k].name
                          for k in range(x.parameter_count))
        got.setdefault(key, set()).add(names)

    hit = miss = 0
    bad: list[str] = []
    for key, ws in want.items():
        gs = got.get(key)
        if gs is None:
            continue
        if gs == ws:
            hit += 1
        else:
            miss += 1
            if len(bad) < 20:
                bad.append(f"{key[0]}.{key[1]}({key[2]}): 真值={sorted(ws)} "
                           f"实得={sorted(gs)}")
    return hit, miss, bad


def compare_counts(m: Metadata, dump: dict) -> tuple[int, int, int, list[str]]:
    """只用 dump 核**参数个数**（不核名字）—— 可吃「只写类型」的 dump。

    比对单位是**类**：把该类所有方法的 `(方法名, 参数个数)` 收成**集合**再比。

    为什么用集合而不是多重集合：IL2CPP 元数据里**泛型实例**（`Foo<int>`、
    `Foo<string>`）各自是一个独立类型定义、名字与泛型定义相同，于是同一个
    `(方法名, 参数个数)` 会出现多次，而 dump 只列泛型定义一次 ——
    用多重集合会把这种正常现象报成「解析错」（实测 3,167 组假不符）。

    跳过编译器生成的**局部函数 / lambda**（名字以 `<` 开头，如
    `<LateUpdate>g__CollectEvents|18_0`、`<Refresh>b__13_0`）：
    两份 dump 对它们的归属不同（aiMod 那份把 lambda 记在 `<>c` 显示类下、
    且完全不列局部函数），元数据里它们又可能挂在**外层类**上 ——
    拿它们比对只会得到假不符。**两侧一起跳**，比较才是对称的。

    Returns: dict，含
        hit            两侧 (方法名, 参数个数) 集合完全一致的类数
        only_dump      只在 dump 里有的类数 —— **元数据缺方法，是解析漏了**
        only_meta      只在元数据里有的类数 —— dump 自己没列（BCL 内部方法等）
        skipped        两侧一起跳过的编译器生成方法数
        bad_dump/bad_meta  前几条样例
    两个方向必须分开：`only_meta` 是 dump 的过滤行为（实测是
    `System.Decimal.AsMutable`、`System.String.GetRawStringData`、
    `System.Number.MatchChars` 这类内部方法），而 `only_dump` 才是
    「我们的解析漏了方法」—— 只有它该报红。
    """
    want: dict[str, set] = {}
    dskip = 0
    for (cls, meth, count), _names in dump.items():
        if meth.startswith("<"):
            dskip += 1
            continue
        want.setdefault(norm_cls(cls), set()).add((meth, count))

    got: dict[str, set] = {}
    skipped = 0
    for x in m.methods:
        if x.name.startswith("<"):
            skipped += 1
            continue
        t = m.types[x.declaring_type] if 0 <= x.declaring_type < len(m.types) else None
        if t is None:
            continue
        for c in (t.nested_name, t.full_name):
            c = norm_cls(c)
            if c in want:
                got.setdefault(c, set()).add((x.name, x.parameter_count))
                break

    hit = only_dump = only_meta = 0
    bad_dump: list[str] = []
    bad_meta: list[str] = []
    for cls, ws in want.items():
        gs = got.get(cls, set())
        miss_dump = sorted(ws - gs)
        miss_meta = sorted(gs - ws)
        if not miss_dump and not miss_meta:
            hit += 1
            continue
        if miss_dump:
            only_dump += 1
            if len(bad_dump) < 6:
                bad_dump.append(f"{cls}: 元数据缺 {miss_dump[:4]}")
        if miss_meta:
            only_meta += 1
            if len(bad_meta) < 6:
                bad_meta.append(f"{cls}: dump 未列 {miss_meta[:4]}")
    return {
        "hit": hit,
        "only_dump": only_dump,
        "only_meta": only_meta,
        "skipped": skipped + dskip,
        "bad_dump": bad_dump,
        "bad_meta": bad_meta,
    }


def main() -> int:
    with Metadata(META) as m:
        ent = m.tables["methods"]
        rec = 36 if m.version >= 31 else 32
        n = ent.size // rec

        # ---------------- 1. 偏移证据
        print("=" * 74)
        print("1. 偏移证据：v31 的 36 字节方法记录，哪个偏移才是 parameterStart")
        print("=" * 74)
        print(f"  元数据 v{m.version}，methods 表 {ent.size:,} 字节 / {rec} = {n:,} 条")
        vals12: dict[int, int] = {}
        vals16: dict[int, int] = {}
        prev16 = -1
        mono16 = True
        for i in range(n):
            b = ent.offset + i * rec
            v12 = struct.unpack_from("<i", m._mm, b + 12)[0]
            v16 = struct.unpack_from("<i", m._mm, b + 16)[0]
            vals12[v12] = vals12.get(v12, 0) + 1
            vals16[v16] = vals16.get(v16, 0) + 1
            # 无参数的方法是 -1（哨兵），单调性只看有参数的那些
            if v16 >= 0:
                if v16 < prev16:
                    mono16 = False
                prev16 = v16
        top12 = sorted(vals12.items(), key=lambda kv: -kv[1])[:3]
        inrange16 = sum(c for v, c in vals16.items() if 0 <= v < len(m.parameters))
        print(f"  +12 取值种类 {len(vals12):,}，最常见 "
              f"{[(hex(v & 0xFFFFFFFF), c) for v, c in top12]}")
        print(f"  +16 取值种类 {len(vals16):,}，落在 parameters 表"
              f"（{len(m.parameters):,} 条）内的记录 {inrange16:,} 条；"
              f"有参数者单调不减 {mono16}；-1（无参数哨兵）"
              f"{vals16.get(-1, 0):,} 条")
        check("+12 不是 parameterStart（取值恒定在表外）",
              not (0 <= top12[0][0] < len(m.parameters)),
              f"最常见值 {hex(top12[0][0] & 0xFFFFFFFF)}，占 "
              f"{top12[0][1] * 100.0 / n:.1f}%")
        check("+16 是 parameterStart（有参数者单调不减、且落在表内）",
              mono16 and inrange16 > n * 0.3,
              f"表内 {inrange16:,}/{n:,}，单调不减 {mono16}")

        # ---------------- 2. 解析率
        print()
        print("=" * 74)
        print("2. 解析率：有参数的方法里，parameters_of 能取到几个")
        print("=" * 74)
        withp = [x for x in m.methods if x.parameter_count > 0]
        gotp = [x for x in withp if m.parameters_of(x)]
        print(f"  有参数的方法 {len(withp):,} 个，取到参数 {len(gotp):,} 个 "
              f"= {len(gotp) * 100.0 / max(len(withp), 1):.2f}%")
        sizes = {x.parameter_count for x in gotp}
        check("参数解析率 > 99%（修复前为 0%）",
              len(gotp) > len(withp) * 0.99,
              f"{len(gotp):,}/{len(withp):,}")
        check("取到的参数个数与 parameter_count 一致",
              all(len(m.parameters_of(x)) == x.parameter_count for x in gotp),
              f"覆盖参数个数集合 {sorted(sizes)[:8]}…")

        # ---------------- 3. dump 真值精度（参数名）
        print()
        print("=" * 74)
        print("3. 与 Il2CppDumper 完整输出比对的精度（参数名）")
        print("=" * 74)
        if PARAM_TRUTH is None:
            print("  无法验证：样本根下没有带参数名的 dump"
                  "（samples.find_param_dump() 返回 None）")
        else:
            pdump, pgame = PARAM_TRUTH
            pmeta = samples.metadata_of(pgame)
            print(f"  真值：{pdump}")
            print(f"  对应游戏：{pgame.name}  元数据：{pmeta.name if pmeta else None}")
            dump, dstats = load_dump_methods(pdump, with_names=True)
            # 只留有参数的那部分：0 参数方法不经过 parameterStart，
            # 两边都恒为空元组、恒相等，计入精度只会把分母灌水
            dump_pos = {k: v for k, v in dump.items() if k[2] > 0}
            nzero = len(dump) - len(dump_pos)
            print(f"  dump 扫了 {dstats['lines']:,} 行，收集 (类, 方法, 参数个数) 真值 "
                  f"{len(dump):,} 组（其中有参数 {len(dump_pos):,} 组，"
                  f"0 参数 {nzero:,} 组不计入精度；另有 "
                  f"{dstats['skipped_noname']:,} 组 dump 自己没写参数名、不作真值）")
            if pmeta is None:
                print("  ❌ 找不到该游戏的元数据，无法比对")
                FAILED.append("参数名真值比对")
            else:
                with Metadata(pmeta) as pm:
                    hit, miss, bad = compare(pm, dump_pos, 16)
                    total = hit + miss
                    print(f"  可比对 {total:,} 组：参数名逐字相同 {hit:,}  "
                          f"不同 {miss:,} → 精度 "
                          f"{hit * 100.0 / max(total, 1):.2f}%")
                    for s in bad:
                        print(f"    不符: {s}")
                    check("对 dump 真值的参数名精度 ≥ 99%",
                          total > 1000 and hit > total * 0.99,
                          f"{hit:,}/{total:,}")

                    # ---------------- 4. 非空转反证
                    print()
                    print("=" * 74)
                    print("4. 非空转反证：把 parameter_start 改回旧偏移（+12）会怎样")
                    print("=" * 74)
                    old_hit, old_miss, _ = compare(pm, dump_pos, 12)
                    print(f"  用 +12 当 parameter_start：命中 {old_hit:,}，"
                          f"不符 {old_miss:,}")
                    check("旧偏移命中数为 0（证明第 3 节测的确实是这个偏移）",
                          old_hit == 0,
                          f"旧偏移命中 {old_hit:,} / 新偏移命中 {hit:,}")

        # ---------------- 4b. 只用另一份 dump 核参数个数
        print()
        print("=" * 74)
        print("4b. 参数个数对账：另一份 dump（只写参数类型，同一游戏）")
        print("=" * 74)
        dcounts, cstats = load_dump_methods(DUMP, with_names=False)
        print(f"  {DUMP.name} 扫了 {cstats['lines']:,} 行，收集 (类, 方法, 参数个数) "
              f"{len(dcounts):,} 组")
        cs = compare_counts(m, dcounts)
        ctotal = cs["hit"] + cs["only_dump"] + cs["only_meta"]
        print(f"  可比对「类」{ctotal:,} 组：方法(名,参数个数)集合完全一致 "
              f"{cs['hit']:,}")
        print(f"    只在 dump（= 元数据漏了方法，解析问题）{cs['only_dump']:,} 组")
        for s in cs["bad_dump"]:
            print(f"      {s}")
        print(f"    只在元数据（= dump 自己没列，如 BCL 内部方法）"
              f"{cs['only_meta']:,} 组")
        for s in cs["bad_meta"]:
            print(f"      {s}")
        print(f"    （两侧一起跳过编译器生成的方法 {cs['skipped']:,} 个）")
        # only_dump 是「元数据漏了 dump 列出的方法」。剩下的 1~2 组已逐个人工核过，
        # 都是 dump 自己的命名怪癖（如 Unity 的 `ICondition2<T, U>` 被印成
        # `ICondition<T, U>`，元数据里根本没有 `ICondition` 这个类型），
        # 所以这里卡「占比 < 0.1%」而不是「恰好为 0」。
        ratio = cs["only_dump"] / ctotal if ctotal else 1.0
        check("元数据几乎不缺 dump 列出的方法（only_dump 占比 < 0.1%）",
              ctotal > 1000 and ratio < 0.001,
              f"only_dump={cs['only_dump']}（{ratio:.4%}），"
              f"only_meta={cs['only_meta']}，一致 {cs['hit']:,}/{ctotal:,}")

        # ---------------- 5. 定点核对
        print()
        print("=" * 74)
        print("5. 定点核对：几个签名已知的方法")
        print("=" * 74)
        want = [
            ("UnityEngine.Mathf", "Abs", "System.Single"),
            ("UnityEngine.Mathf", "Max", "System.Single"),
            ("System.String", "Concat", "System.String"),
            ("UnityEngine.Transform", "Translate", "System.Void"),
        ]
        for cls, meth, ret in want:
            t = m.type_by_name(cls)
            if t is None:
                print(f"  {cls}: 未找到")
                continue
            for x in m.methods_of(t):
                if x.name != meth or adapt_ret(m, x) != ret:
                    continue
                ps = m.parameters_of(x)
                shown = ", ".join(
                    f"{p.name}:{adapt_type(m, p) or '?'}" for p in ps)
                print(f"  {t.nested_name}.{meth}  ->  ({shown})")
                break
        ps = m.parameters_of([x for x in m.methods_of(m.type_by_name("UnityEngine.Mathf"))
                              if x.name == "Abs" and x.parameter_count == 1][0])
        check("Mathf.Abs 的参数名与类型都解析出来了",
              bool(ps) and ps[0].name and adapt_type(m, ps[0]),
              f"name={ps[0].name if ps else None} "
              f"type={adapt_type(m, ps[0]) if ps else None}")

    # ---------------- 6. MCP 工具层
    print()
    print("=" * 74)
    print("6. MCP 工具层：Agent 看到的 class_detail 里 parameters 不再是空")
    print("=" * 74)
    from gamemod_mcp import server as S  # noqa: E402
    op = S.open_game(game_dir=str(GAME))
    check("open_game 成功", bool(op.get("ok")), f"引擎={op.get('engine')}")
    cd = S.class_detail("UnityEngine.Mathf", method_name="Abs")
    ms = cd.get("methods") or []
    print(f"  class_detail(UnityEngine.Mathf, method_name='Abs') 返回 {len(ms)} 个重载")
    for x in ms:
        print(f"    {x['token']} {x['return_type']} "
              f"parameters={x['parameters']}")
    check("工具层返回的 parameters 非空",
          bool(ms) and all(x.get("parameters") for x in ms),
          f"{[x.get('parameters') for x in ms]}")
    check("参数里带名字与类型",
          all(p.get("name") and p.get("type") for x in ms
              for p in (x.get("parameters") or [])),
          "每个参数都有 name/type")
    # 参数个数与 parameters 长度一致（不许少给）
    bad_len = [x["token"] for x in ms
               if len(x.get("parameters") or []) != x.get("parameter_count")]
    check("parameters 长度等于 parameter_count", not bad_len, f"不符 {bad_len}")

    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


# 这两个小包装避免在 5 节里重复写 adapt 调用
def adapt_ret(m: Metadata, x) -> str:
    """方法返回类型名（与 `class_detail` 用的是同一条路径）。"""
    from gamemod_mcp.engine import adapt
    return adapt.resolve_method_return(m, adapt.method_return_key(m, x))


def adapt_type(m: Metadata, p) -> str:
    """参数类型名：与 `adapt.resolve_param_types` 对 IL2CPP 走的是同一条路
    （参数自带的 typeIndex → 类型名）。"""
    from gamemod_mcp.engine import adapt
    return m.resolve_il2cpp_type(adapt.param_type_key(m, p)) or ""


if __name__ == "__main__":
    sys.exit(main())
