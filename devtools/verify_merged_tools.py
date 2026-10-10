"""验证「合并后」的工具行为：find_derived_classes(assembly=) 与 class_detail(method_name=)。

背景：本次精简删掉了 3 个工具（detect_games / find_monobehaviours /
get_method_source），其中后两个的能力并入：
    find_monobehaviours → find_derived_classes(assembly=...)
    get_method_source  → class_detail(method_name=...)

合并最容易出的错不是「跑不起来」，而是**行为悄悄变了**：
    · find_monobehaviours 原本只返回字段子集（无 index）
    · get_method_source 原本用 include_inherited=True 查找
若合并时漏掉这些细节，Agent 会拿到形状不同的结果或假的「未找到」。
因此本脚本专门对着真实游戏验证这些语义。

    python devtools/verify_merged_tools.py
"""

from __future__ import annotations

import io
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8",
                               errors="replace")

import samples  # noqa: E402
samples.add_src_to_path()

PASS, FAIL = [], []


def check(label: str, cond: bool, detail: str = "") -> None:
    tag = "✅" if cond else "❌"
    print(f"  {tag} {label}" + (f" — {detail}" if detail else ""))
    (PASS if cond else FAIL).append(label)


def show(title: str) -> None:
    print()
    print("=" * 76)
    print(title)
    print("=" * 76)


def main() -> int:
    # 优先用 Mono 样本（有 MonoBehaviour 且基类在框架程序集里，最能暴露问题）
    mono_dir = samples.find_mono()
    il2cpp_dir = samples.find_il2cpp()

    print("样本：")
    print(f"  Mono   : {mono_dir or '(无)'}")
    print(f"  IL2CPP : {il2cpp_dir or '(无)'}")

    if not mono_dir and not il2cpp_dir:
        print("\n两个引擎的样本都不存在 → 无法验证，跳过（不假装通过）")
        return 0

    from gamemod_mcp.engine.index import Index
    from gamemod_mcp.engine import discovery

    # ---------------------------------------------------------------- Mono
    if mono_dir:
        show("1. Mono 样本：find_derived_classes(assembly=) 取代 find_monobehaviours")
        mono = discovery.mono_paths(mono_dir)
        asm = None
        for a in (mono.get("assemblies") or []):
            if "Assembly-CSharp.dll" in a:
                asm = a
                break
        if not asm:
            asm = (mono.get("assemblies") or [None])[0]
        print(f"  程序集: {asm}")
        idx = Index.open(asm, cache_dir=str(samples.CACHE_TEST))

        all_mb = idx.find_derived_classes("UnityEngine.MonoBehaviour", limit=5000)
        check("不限程序集时能找到 MonoBehaviour 子类",
              all_mb.get("found") is True and all_mb.get("total", 0) > 0,
              f"total={all_mb.get('total')}")

        filtered = idx.find_derived_classes("UnityEngine.MonoBehaviour",
                                           assembly="Assembly-CSharp", limit=5000)
        check("assembly 过滤生效", filtered.get("total", 0) <= all_mb.get("total", 0),
              f"过滤后={filtered.get('total')} / 全部={all_mb.get('total')}")
        check("过滤后确实都是该程序集",
              all("Assembly-CSharp" in (it.get("image") or "")
                  for it in (filtered.get("items") or [])),
              f"样例={[it.get('image') for it in (filtered.get('items') or [])[:3]]}")
        check("assembly_filter 被如实回显",
              filtered.get("assembly_filter") == "Assembly-CSharp",
              str(filtered.get("assembly_filter")))

        # 与原 find_monobehaviours 的关键差异：这里每项都有 index
        items = filtered.get("items") or []
        check("每项都带 index（原 find_monobehaviours 没有）",
              all("index" in it for it in items),
              f"首项键={sorted(items[0].keys()) if items else '无'}")

        # ---- total 必须是**真实总数**，不能是被 limit 截断后的条数 ----
        # 曾经这里返回 total=len(items)，于是 limit=5 时报 total=5，
        # Agent 会以为「这个游戏只有 5 个脚本」。实测 Archon Soul 有 644 个。
        show("1b. total 是匹配总数，不受 limit 影响（契约 §3.1 第 5 条）")
        small = idx.find_derived_classes("UnityEngine.MonoBehaviour", limit=5)
        full = idx.find_derived_classes("UnityEngine.MonoBehaviour", limit=5000)
        check("limit=5 时 total 仍等于真实总数",
              small.get("total") == full.get("total"),
              f"limit=5 报 total={small.get('total')}，全量 total={full.get('total')}")
        check("limit=5 时 returned=5 且 has_more=True",
              small.get("returned") == 5 and small.get("has_more") is True,
              f"returned={small.get('returned')} has_more={small.get('has_more')}")
        check("未截断时 has_more=False",
              full.get("has_more") is False,
              f"has_more={full.get('has_more')} returned={full.get('returned')}")
        check("被截断时给出提示", bool(small.get("note")),
              str(small.get("note"))[:70])

        show("2. Mono 样本：class_detail(method_name=) 取代 get_method_source")
        # 取一个有方法的类
        cls = None
        for it in items:
            nm = it.get("full_name")
            if not nm:
                continue
            det = idx.class_detail(nm)
            ms = [m for m in (det.get("methods") or []) if m.get("name") == "Update"]
            if ms:
                cls = nm
                break
        if not cls:
            print("  ⚠️  未找到带 Update 的类 → 跳过本段（不假装通过）")
        else:
            print(f"  目标类: {cls}")
            # Update 通常是继承自 MonoBehaviour 的方法，
            # 正好验证「合并后仍带 include_inherited」
            one = idx.class_detail(cls, include_inherited=True)
            hits = [m for m in (one.get("methods") or []) if m.get("name") == "Update"]
            check("能查到 Update 方法", len(hits) > 0, f"命中 {len(hits)} 个")
            if hits:
                h = hits[0]
                check("方法带 token", h.get("token") is not None,
                      f"token={h.get('token')}")
                check("方法带 parameter_count", "parameter_count" in h,
                      f"parameter_count={h.get('parameter_count')}")
                check("方法带 parameters 列表", isinstance(h.get("parameters"), list),
                      f"parameters={h.get('parameters')}")

        # 继承来的方法必须有 parameter_count —— 这是本次修的字段形状不一致。
        # 不能只看第一个类就下结论：必须主动扫描直到找到**真的**有继承方法的类，
        # 否则这条断言会因为「没测到」而空过，等于没验证。
        show("3. 继承方法的字段形状必须与自身方法一致（本次修复）")
        target_cls, inh_ms = None, []
        scanned = 0
        for it in items[:120]:
            nm = it.get("full_name")
            if not nm:
                continue
            scanned += 1
            try:
                det = idx.class_detail(nm, include_inherited=True)
            except Exception:
                continue
            cand = [m for m in (det.get("methods") or []) if "declared_by" in m]
            if cand:
                target_cls, inh_ms = nm, cand
                break
        print(f"  扫描 {scanned} 个类后命中：{target_cls}")
        if inh_ms:
            miss = [m["name"] for m in inh_ms
                    if "parameter_count" not in m or "parameters" not in m]
            check("继承方法都带 parameter_count/parameters",
                  not miss,
                  f"检查了 {len(inh_ms)} 个继承方法" +
                  (f"，缺失: {miss[:5]}" if miss else ""))
            bad_shape = [m["name"] for m in inh_ms
                         if not isinstance(m.get("parameters"), list)]
            check("继承方法的 parameters 是列表", not bad_shape,
                  str(bad_shape[:5]))
        else:
            check("能扫描到带继承方法的类（否则本段无法验证）", False,
                  f"扫了 {scanned} 个类都没有继承方法")

        idx.close()

    # ---------------------------------------------------------------- IL2CPP
    if il2cpp_dir:
        show("4. IL2CPP 样本：合并后仍能正常查询")
        meta = discovery.il2cpp_paths(il2cpp_dir).get("metadata")
        print(f"  元数据: {meta}")
        idx2 = Index.open(meta, cache_dir=str(samples.CACHE_ADAPT_TEST))
        r = idx2.search_classes(pattern="", limit=1)
        check("索引可查询", r.get("total", 0) > 0, f"type total={r.get('total')}")

        # cache_hit 必须如实反映（P0-3）
        info = idx2.index_info()
        check("cache_hit 是布尔值", isinstance(info.get("cache_hit"), bool),
              f"cache_hit={info.get('cache_hit')}")

        # 再开一次应当命中缓存
        idx2.close()
        idx3 = Index.open(meta, cache_dir=str(samples.CACHE_ADAPT_TEST))
        info3 = idx3.index_info()
        check("第二次打开命中缓存（cache_hit=True）",
              info3.get("cache_hit") is True,
              f"cache_hit={info3.get('cache_hit')}")
        idx3.close()

        show("5. 强制重建时 cache_hit 必须为 False（旧实现恒为 True）")
        idx4 = Index.open(meta, cache_dir=str(samples.CACHE_ADAPT_TEST),
                          force_rebuild=True)
        info4 = idx4.index_info()
        check("force_rebuild 后 cache_hit=False",
              info4.get("cache_hit") is False,
              f"cache_hit={info4.get('cache_hit')}")
        idx4.close()

    print()
    print("=" * 76)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    _sys.exit(main())
