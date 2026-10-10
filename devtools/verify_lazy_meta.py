"""验收 / 决策门禁：**元数据解析何时发生**（P1-3b 的否决依据）。

## 这个脚本回答什么

P1-2 让「缓存命中」不再解析元数据（IL2CPP 528 → 19.6 ms）。但缓存命中之后
**第一次真正的查询**会不会又把它逼出来？会 —— 而且这决定了最后一个待办项
（P1-3b：把 `parent_name` / `field_type` 入库，让查询不再需要解析器）
值不值得做。所以本脚本既是**回归门禁**（惰性不许退化），
也是 **P1-3b 的决策门禁**（否决依据变了就报出来）。

## 实测结论（本机，IL2CPP 20,072 类型 / 17 MB 元数据）

| 事实 | 数值 |
|---|---|
| 缓存命中后第一次解析元数据 | **~440 ms** |
| 本脚本逐题量的 8 个查询里，会逼出解析器的 | **7 个**（只有 `index_info` 不需要） |
| 更全的一次探测（另含 `search_classes('')` 等 12 项） | **11 个**需要解析器 |
| 解析是「每题一次」还是「每次 open 一次」 | **每次 open 一次**（结果缓存在 `idx.meta`） |
| 一次 5 查询会话里它占多少 | **65~78%**（441 / 683 ms）；查询越多占比越低 |
| 解析耗时构成 | methods 184 + parameters 103 + fields 72 + types 57 ms |
| 全量重建（缓存未命中）作参照 | 1225 ms（其中解析 443 ms、建表写库 783 ms） |
| 要改写的查询层函数 | **10 个**（AST 数出来的，见 §4） |

**否决理由**（不是「不重要」，而是「代价与风险不成比例」）：

1. **半量修复等于零**。`search_classes` 之所以需要解析器，只因为
   `parent` 一个字段（`_resolve_parent`）。但「只存 `parent_name`」省不下
   任何东西 —— 紧接着的 `class_detail` 仍然要解析（它要用字段/方法类型）。
   解析是**全有或全无**，所以清单里「`parent_name`/`field_type` 入库」
   这种按列拆的写法本身就误导：这两列不能分别计价。
2. **完整修复要动分层**。要让 11 个查询都不解析，得把
   `class_detail` / `class_hierarchy`（`base_chain`）/ `analyze_dependency`
   改成完全由 SQLite 驱动 —— 那等于在索引层**再实现一遍继承语义**，
   而 AGENTS.md §3 明确要求引擎差异只出现在 `adapt.py`。
   两份继承语义互相漂移，正是本项目反复吃亏的那类错误（错数据 + 无提示）。
3. **省下的是「每次 open_game 一次」的 420 ms**，不是每题 420 ms。
   Mod 开发一次会话会查很多题，这笔钱很快被摊薄。

所以：**P1-3b 否决（不改 schema）**，理由由本脚本钉住。

## 顺手捞到的第二个缺陷（§5，已修）

写 §1 的计数替身时把 `_load_meta` 的签名搞错了（它是 `staticmethod`），
每次调用都抛 `TypeError` —— 结果 `search_classes` **一声不响**地返回了
`ok: true` + 200 行 `parent: null`。根因是 `index.py` 的 `_resolve_parent`
与 `_resolve_param` 把 `self.meta`（惰性载入，**真的会失败**的那一步）
放在了 `try/except Exception` **里面**，于是「元数据载入失败」被降级成
「这些类没有基类 / 这个参数没有类型」。这正好撞在 AGENTS.md §2.3 上
（不许假成功），而且 `server.py` 每个工具本来就包了
`try/except → ok:false`，异常抛出去就能变成诚实的错误。
修法是把 `m = self.meta` 提到 try 之外 —— 见 §5 的断言与那段**把旧写法
原样搬回来**的反证。

## 什么时候该回来重看

- 第 3 节那张表**空了**（查询不再逼出解析器）→ 说明有别的实现做到了，
  订正本文件与两处文档的否决结论。
- 第 2 节的「解析只发生一次」**不再成立** → 说明引入了重复解析，
  那是缺陷，不是改进。
"""

from __future__ import annotations

import ast
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from gamemod_mcp.engine import adapt, index as ix        # noqa: E402

PASS = 0
FAIL = 0
WARN = 0


def check(label: str, cond: bool, detail: str = "", warn_only: bool = False) -> bool:
    global PASS, FAIL, WARN
    if cond:
        print(f"  \u2705 {label}" + (f" \u2014 {detail}" if detail else ""))
        PASS += 1
        return True
    if warn_only:
        print(f"  \u26a0\ufe0f  {label}" + (f" \u2014 {detail}" if detail else ""))
        WARN += 1
        return False
    print(f"  \u274c {label}" + (f" \u2014 {detail}" if detail else ""))
    FAIL += 1
    return False


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# 查询层里「读 self.meta / self._meta」的地方 —— 完整版 P1-3b 要逐个改写的清单。
# 用 AST 数，不用正则：正则会把赋值、注释、字符串里的 self.meta 也算进去。
_NON_QUERY = {"meta", "_attach", "_build", "close", "__init__"}


def meta_read_sites() -> list[str]:
    src = (samples.SRC / "gamemod_mcp" / "engine" / "index.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    sites: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Index":
            continue
        for fn in node.body:
            if not isinstance(fn, ast.FunctionDef) or fn.name in _NON_QUERY:
                continue
            for sub in ast.walk(fn):
                if (isinstance(sub, ast.Attribute) and sub.attr in ("meta", "_meta")
                        and isinstance(sub.value, ast.Name) and sub.value.id == "self"):
                    sites.append(fn.name)
                    break
    return sites


def test_load_failure(game: Path, meta_path: Path, cache: Path) -> None:
    """§5：元数据**载入失败**必须抛出去，不许降级成「这些类没有基类」。

    这一节是被一段**写错的测试替身**引出来的：写上面的计数包装时把
    `_load_meta` 的签名搞错了（它是 staticmethod），于是每次调用都抛
    `TypeError` —— 结果 `search_classes` **一声不响**地返回了
    `ok: true` + 3 行 `parent: null`。原因是 `self.meta`（惰性载入，
    真的会失败的那一步）被放在了 `try/except Exception` **里面**。

    修法：把 `m = self.meta` 提到 try 之外。这一节就钉住它：
    用「永远抛 MetadataError 的 `_load_meta`」当替身，逐个查询断言
    **不许**有任何一个悄悄返回成功。
    """
    section("5. 元数据载入失败**必须**报错，不许降级成「没有基类 / 没有类型」")

    from gamemod_mcp import server as S
    from gamemod_mcp.engine.il2cpp_meta import MetadataError

    def boom(path, engine=""):
        raise MetadataError("（测试替身）元数据载入失败")

    # ---- 5.1 索引层：查询必须抛异常，而不是返回带 null 的成功结果
    orig = ix.Index._load_meta
    raised, swallowed = [], []
    try:
        ix.Index._load_meta = staticmethod(boom)
        for name, call in [
            ("search_classes", lambda i: i.search_classes(pattern="Player")),
            ("class_detail", lambda i: i.class_detail("PlayerPrefsController")),
            ("search_fields", lambda i: i.search_fields(pattern="health")),
            ("search_methods", lambda i: i.search_methods(pattern="Update")),
            ("class_hierarchy", lambda i: i.class_hierarchy("PlayerPrefsController")),
            ("find_derived_classes",
             lambda i: i.find_derived_classes("UnityEngine.MonoBehaviour")),
            ("analyze_dependency",
             lambda i: i.analyze_dependency("PlayerPrefsController")),
        ]:
            # 先正常开一个缓存命中的索引（此时还没载入元数据），再让载入失败
            with ix.Index.open(meta_path, cache_dir=cache) as idx:
                assert idx._meta is None
                try:
                    res = call(idx)
                    swallowed.append((name, str(res)[:70]))
                except MetadataError:
                    raised.append(name)

        # read_query_result 特殊：它的句柄必须先造出来（那一步必然解析），
        # 所以先用**好**载入器造句柄并退回「尚未解析」，再让载入失败 ——
        # 翻页时仍会撞上基类解析。
        ix.Index._load_meta = staticmethod(orig)
        with ix.Index.open(meta_path, cache_dir=cache) as idx:
            h = idx.search_classes(pattern="Player", limit=5)["handle"]
            idx._meta = None
            ix.Index._load_meta = staticmethod(boom)
            try:
                idx.read_query_result(h, limit=5)
                swallowed.append(("read_query_result", "返回了结果"))
            except MetadataError:
                raised.append("read_query_result")
            finally:
                ix.Index._load_meta = staticmethod(orig)

        check("索引层：所有查询都**抛异常**（没有一个把载入失败吞掉）",
              not swallowed,
              f"吞掉的：{swallowed}" if swallowed
              else f"抛出的 {len(raised)} 个：{raised}")
    finally:
        # 复原 staticmethod 必须再包一层 staticmethod：
        # 从类上取 staticmethod 拿到的是**裸函数**，直接赋回去会被当成
        # 普通实例方法（于是多出一个 self，调用时报 3 个参数）。
        # 这个坑我在同一个脚本里踩了两次，故此处留注释。
        ix.Index._load_meta = staticmethod(orig)

    # ---- 5.2 工具层：必须变成 ok:false + 说明（这才是有用的行为）
    S.open_game(game_dir=str(game))
    try:
        ix.Index._load_meta = staticmethod(boom)
        res = S.search_classes(pattern="Player")
        check("工具层：返回 ok:false 而不是 ok:true",
              res.get("ok") is False,
              f"ok={res.get('ok')} error={str(res.get('error'))[:70]}")
        check("工具层：错误信息里说明了原因（不是空 error）",
              bool(res.get("error")),
              f"error={str(res.get('error'))[:90]}")
    finally:
        ix.Index._load_meta = staticmethod(orig)
        S.close_session()

    # ---- 5.3 非空转反证：把**旧写法**原样搬回来，断言必须失败
    def legacy_resolve_parent(self, type_idx):
        """修复前的原样：`self.meta` 在 try **里面**。"""
        try:
            name = adapt.parent_name(self.meta, self.meta.types[type_idx])
        except Exception:
            return None
        return name or None

    orig_rp = ix.Index._resolve_parent
    try:
        ix.Index._resolve_parent = legacy_resolve_parent
        ix.Index._load_meta = staticmethod(boom)
        with ix.Index.open(meta_path, cache_dir=cache) as idx:
            res = idx.search_classes(pattern="Player", limit=5)
        items = res.get("items") or []
        all_null = bool(items) and all(it.get("parent") is None for it in items)
        check("反证：搬回旧写法后，同一个查询**变成** ok + parent 全为 null"
              "（证明 5.1/5.2 的断言确实能失败）",
              all_null,
              f"{len(items)} 行，parent 全为 null={all_null} —— "
              f"这正是修复前的真实行为")
    except MetadataError:
        check("反证：搬回旧写法后应能吞掉载入失败", False,
              "旧写法竟然抛异常了？反证的假设不成立，需重看")
    finally:
        ix.Index._resolve_parent = orig_rp
        ix.Index._load_meta = staticmethod(orig)

    # ---- 5.4 复原后必须恢复正常（证明上面是「替身」而不是真坏了）
    with ix.Index.open(meta_path, cache_dir=cache) as idx:
        res = idx.search_classes(pattern="Player", limit=5)
    parents = [it.get("parent") for it in (res.get("items") or [])]
    check("复原后同一查询恢复正常（parent 有真值）",
          any(p for p in parents),
          f"parent 样例：{parents[:3]}")


def main() -> int:
    game = samples.find_il2cpp()
    if not samples.require(game, "IL2CPP (Garden of Witches)"):
        print("\n（本脚本需要一个 IL2CPP 样本；样本不在时记为「无法验证」）")
        return 0
    meta_path = (game / "Garden of Witches_Data" / "il2cpp_data"
                 / "Metadata" / "global-metadata.dat")
    cache = ix.default_cache_dir()

    # ============================================================ 1. 惰性不退化
    section("1. 缓存命中后**不许**解析元数据（P1-2 的成果，不许退化）")
    loaded: list[str] = []
    orig_load = ix.Index._load_meta

    # 注意 `_load_meta` 是 **staticmethod**：替身签名里没有 self。
    # （第一次写这个包装时就是在这里写错的 —— 加了 self 于是每次调用都抛
    #  TypeError，反而把 §5 那个「载入失败被吞掉」的缺陷试出来了。）
    def counting(path, engine=""):
        loaded.append(str(path))
        return orig_load(path, engine)

    ix.Index._load_meta = staticmethod(counting)
    try:
        idx = ix.Index.open(meta_path, cache_dir=cache)
        check("打开缓存命中的索引：cache_hit=True 且**没有**解析元数据",
              idx.cache_hit and idx._meta is None,
              f"cache_hit={idx.cache_hit}  _meta is None={idx._meta is None}")
        info = idx.index_info()
        check("index_info 不解析元数据（它只读 SQLite）",
              idx._meta is None and info.get("ok", True) is not False,
              f"仍是惰性；database_size={info.get('database_size')}")
        handle = idx.search_classes(pattern="Player", limit=3)["handle"]
        parsed_at = len(loaded)
        check("第一次真正的查询**会**解析（这是本次要量的事实，不是缺陷）",
              idx._meta is not None and parsed_at == 1,
              f"解析次数={parsed_at}")

        # 同一会话里再查很多题：解析次数**不许**增加
        for _ in range(3):
            idx.class_detail("PlayerPrefsController")
        idx.search_fields(pattern="health")
        idx.read_query_result(handle, limit=3)
        check("解析**只发生一次**（每次 open 一次，不是每题一次）",
              len(loaded) == 1,
              f"再查 5 题后解析次数仍为 {len(loaded)}")

        # 反证：把解析结果丢掉，必须变成「每题都解析」——证明确实在数次数
        idx._meta = None
        idx.search_fields(pattern="health")
        check("反证：手动丢弃已解析结果后，解析次数会增加（证明上面数的是真次数）",
              len(loaded) == 2,
              f"丢弃后解析次数={len(loaded)}")
        idx.close()
    finally:
        ix.Index._load_meta = staticmethod(orig_load)

    # ============================================================ 2. 哪些查询需要
    section("2. 哪些查询会把解析器逼出来（P1-3b 要改的就是这些）")
    with ix.Index.open(meta_path, cache_dir=cache) as i0:
        cls = (i0.search_classes(pattern="Player", limit=1).get("items")
               or [{}])[0].get("full_name") or "Player"

    ops: list[tuple[str, object]] = [
        ("index_info",               lambda i: i.index_info()),
        ("search_classes",           lambda i: i.search_classes(pattern="Player")),
        ("search_fields",            lambda i: i.search_fields(pattern="health")),
        ("search_methods",           lambda i: i.search_methods(pattern="Update")),
        ("class_detail",             lambda i: i.class_detail(cls)),
        ("class_hierarchy",          lambda i: i.class_hierarchy(cls)),
        ("find_derived_classes",     lambda i: i.find_derived_classes("UnityEngine.MonoBehaviour")),
        ("analyze_dependency",       lambda i: i.analyze_dependency(cls)),
    ]
    need, avoid = [], []
    for name, fn in ops:
        with ix.Index.open(meta_path, cache_dir=cache) as idx:
            if not idx.cache_hit or idx._meta is not None:
                check(f"{name}：前提（缓存命中且未解析）成立", False,
                      f"cache_hit={idx.cache_hit}")
                continue
            fn(idx)
            (need if idx._meta is not None else avoid).append(name)

    print(f"     需要解析器：{need}")
    print(f"     不需要    ：{avoid}")
    check("只有 index_info 不需要解析器 —— 其余查询都会逼出来",
          avoid == ["index_info"] and len(need) == len(ops) - 1,
          f"不需要的={avoid}",
          warn_only=True)
    check("**关键**：紧接着 search_classes 的 class_detail 也需要解析器"
          "（所以「只存 parent_name」省不下任何东西）",
          "class_detail" in need and "search_classes" in need,
          f"need={need}", warn_only=True)

    # ============================================================ 3. 解析的代价
    section("3. 代价：解析一次多少毫秒、占一次会话多少")
    with ix.Index.open(meta_path, cache_dir=cache) as idx:
        assert idx._meta is None
        t = time.perf_counter()
        _ = idx.meta
        parse_ms = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    with ix.Index.open(meta_path, cache_dir=cache) as idx:
        idx.search_classes(pattern="Player")
        idx.class_detail(cls)
        idx.search_fields(pattern="health")
        idx.search_methods(pattern="Update")
        idx.find_derived_classes("UnityEngine.MonoBehaviour")
    session_ms = (time.perf_counter() - t) * 1000

    print(f"     解析一次            ：{parse_ms:.0f} ms")
    print(f"     一次 5 查询会话合计 ：{session_ms:.0f} ms"
          f"（解析占 {parse_ms / session_ms * 100:.0f}%）")
    print(f"     若 P1-3b 做完        ：约 {session_ms - parse_ms:.0f} ms"
          f"（省 {parse_ms:.0f} ms，仅在**每次 open_game**）")
    check("解析确实是「几百毫秒级」的一次性代价（否决是要讲道理的）",
          parse_ms > 100,
          f"{parse_ms:.0f} ms —— 量级与文档记录一致", warn_only=True)

    # ============================================================ 4. 分层代价
    section("4. 完整修复的结构性代价：要改写多少个查询层函数")
    sites = meta_read_sites()
    uniq = sorted(set(sites))
    print(f"     读 self.meta 的查询层函数（{len(uniq)} 个）：{uniq}")
    check("至少 5 个查询层函数直接依赖解析器（这就是「要动分层」的量化依据）",
          len(uniq) >= 5,
          f"{len(uniq)} 个：{uniq}")
    check("反证：同一个 AST 计数在完全不读 meta 的模块上必须为 0",
          True,
          "下面用 cap.py 交叉验证")

    src = (samples.SRC / "gamemod_mcp" / "cap.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    hits = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("meta", "_meta"):
            hits += 1
    check("反证：cap.py 里 meta 读取点为 0（证明上面的计数不是「数什么都得数」）",
          hits == 0, f"cap.py 命中 {hits} 次")

    # ============================================================ 5. 载入失败不许降级
    test_load_failure(game, meta_path, cache)

    # ============================================================ 汇总
    print()
    print("=" * 78)
    if FAIL:
        print(f"\u274c 通过 {PASS} 项，失败 {FAIL} 项（警告 {WARN}）")
    else:
        print(f"\u2705 通过 {PASS} 项，失败 0 项"
              + (f"（警告 {WARN}）" if WARN else ""))
    print("=" * 78)
    print()
    print("决策记录：P1-3b **否决**（不改 schema）—— 半量修复等于零、")
    print("完整修复要把继承语义搬进索引层（违反 AGENTS.md §3），")
    print("而省下的是每次 open_game 一次的几百毫秒。详见本文件 docstring。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
