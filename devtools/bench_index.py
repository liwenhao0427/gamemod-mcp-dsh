"""索引性能基准：把「开索引」与「每次查询」的成本拆开量清楚。

为什么需要这个
============================================================
`docs/项目梳理与优化建议.md` 的 P1-2 / P1-3 都是**性能**改动，而性能改动
最容易变成「我觉得快了」。本项目的一贯要求是「数字必须是跑出来的」，
所以改之前先量、改之后再量，两次都要有留痕。

它量三件事，对应三个不同的优化方向：

1. **开索引**（P1-2，已完成）—— `Index.open` 命中缓存时仍然全量解析元数据。
   这里把 `_load_meta` / 缓存命中 / 强制重建 / 纯 SQL 下限分开量，
   差额就是「不加载解析器」能省下的部分。
2. **查询**（P1-3，已完成）—— `search_*` 原先把**全量**结果解析一遍塞进
   句柄，与 `limit` 无关。这里把 ① SQL 取回 ② 解析这一页
   ③ 修改前要多付的「全量进句柄」分开量。
   > 时间只用来**量化收益**；「确实没再全量解析」这件事由
   > `devtools/verify_lazy_handles.py` 用**计数断言**证明（解析条数
   > 恰好等于 limit），因为时间会闪、计数不会。
3. **继承过滤**（P1-4，已完成）—— `subtype_of` 原先是逐行走基类链。
   这里跑多轮打印**分布**（最小/中位/最大）并把首轮单列 ——
   首轮要建邻接表（一次 O(类型数)），其后才是不变量。
   语义没变这件事由 `devtools/verify_subtype.py` 用「与旧的逐条判定
   完全相等」证明（61 项断言）。

用法：
    python devtools/bench_index.py              # 两个引擎都跑
    python devtools/bench_index.py --engine il2cpp

注意：本脚本是**基准**不是**验收**，所以不叫 `verify_*`，
也不进 `run_all_verify.py` 的清单 —— 它没有「通过/失败」，
只有实测数字。数字随机器而变，不要把它当成回归门禁。
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import statistics
import sys
import time
from pathlib import Path

# 中文控制台默认 GBK，emoji 会直接抛 UnicodeEncodeError。
# 全项目 20 个脚本里有 18 个都做了这件事，本脚本照做。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
if not isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import samples  # noqa: E402

samples.add_src_to_path()
from gamemod_mcp.engine import index as I  # noqa: E402
from gamemod_mcp.engine import pe, ue  # noqa: E402

#: 每个查询重复多少次取最小值（排除首次预热与系统抖动）
REPEATS = 5


def ms(t0: float, t1: float | None = None) -> float:
    return ((t1 if t1 is not None else time.perf_counter()) - t0) * 1000


def best(fn):
    """跑 REPEATS 次取最小耗时，同时返回最后一次的结果。"""
    ts = []
    result = None
    for _ in range(REPEATS):
        t0 = time.perf_counter()
        result = fn()
        ts.append(ms(t0))
    return min(ts), result


CASES = [
    ("il2cpp", "IL2CPP",
     lambda: samples.find_il2cpp(),
     "Garden of Witches_Data/il2cpp_data/Metadata/global-metadata.dat"),
    ("mono", "Mono",
     lambda: samples.find_mono(),
     "Archon Soul_Data/Managed/Assembly-CSharp.dll"),
]


def bench_open(label: str, mpath: Path) -> None:
    print("-" * 76)
    print(f"[P1-2] 开索引成本  ——  {label}")
    print("-" * 76)

    t0 = time.perf_counter()
    meta = I.Index._load_meta(mpath, "")
    t1 = time.perf_counter()
    n_types = len(meta.types)
    meta.close()
    print(f"  _load_meta 全量解析          {ms(t0, t1):9.1f} ms"
          f"   类型 {n_types:,}")

    t1 = time.perf_counter()
    idx = I.Index.open(mpath)
    t2 = time.perf_counter()
    db = idx.db_path
    print(f"  Index.open 缓存命中          {ms(t1, t2):9.1f} ms"
          f"   cache_hit={idx.cache_hit}")
    # 这里给的是**确定性证据**，不是时间：缓存命中后 `_meta` 必须仍是 None。
    # 时间会闪（机器负载、杀毒扫描），而这个布尔值不会 ——
    # 它直接回答「命中缓存到底有没有解析元数据」。
    parsed = idx._meta is not None
    print(f"  缓存命中后是否已解析元数据：{'是 ← 仍在全量解析！' if parsed else '否（惰性生效）'}")
    idx.close()

    t2 = time.perf_counter()
    idx2 = I.Index.open(mpath, force_rebuild=True)
    t3 = time.perf_counter()
    print(f"  Index.open 强制重建          {ms(t2, t3):9.1f} ms")
    idx2.close()

    # 纯 SQL 下限：校验 + 连库，不碰解析器
    t3 = time.perf_counter()
    valid = I.Index._is_valid(db)
    t4 = time.perf_counter()
    conn = sqlite3.connect(str(db))
    conn.execute("SELECT COUNT(*) FROM types").fetchone()
    t5 = time.perf_counter()
    conn.close()
    print(f"  _is_valid（含一次 connect）  {ms(t3, t4):9.1f} ms   valid={valid}")
    print(f"  connect + COUNT(*)           {ms(t4, t5):9.1f} ms")
    # 注意：不要用「解析耗时 / 缓存命中耗时」当收益比例 —— 那个式子只在
    # **修复前**才有意义（那时命中路径里确实含解析）。修好之后它已经
    # 不含解析，比值会算出 2000% 这种荒唐数字（实测过）。
    # 收益要看的是「命中 vs 全量解析」这两个绝对值：
    print(f"  → 每次缓存命中，比全量解析省下 ≈ {ms(t0, t1):.1f} ms"
          f"（{ms(t1, t2):.1f} ms vs {ms(t0, t1):.1f} ms）")
    print(f"  → 缓存命中的理论下限 ≈ {ms(t3, t5):.1f} ms"
          f"（校验 + 连库，不碰解析器）")


def bench_query(label: str, mpath: Path) -> None:
    print("-" * 76)
    print(f"[P1-3] 单次查询成本  ——  {label}")
    print("-" * 76)
    idx = I.Index.open(mpath)

    # P1-2 之后，缓存命中的索引**不解析**元数据；第一次碰 `meta` 才解析。
    # 必须先预热：否则第一条查询会把那次全量解析（IL2CPP 实测约 500 ms）
    # 算进「② 解析这一页」，看起来像查询本身慢得离谱 —— 实测确实会出现
    # 「解析 20 条花 503 ms」这种假象。预热后下面的数字才是纯查询成本。
    lazy_pending = idx._meta is None
    t0 = time.perf_counter()
    _ = idx.meta
    t1 = time.perf_counter()
    if lazy_pending:
        print(f"  （预热：首次访问 meta 触发惰性解析 {ms(t0, t1):.1f} ms，"
              f"不计入下面的查询耗时）")

    for tag, sql in (
        ("全表 pattern=''", "SELECT * FROM types ORDER BY nested_name"),
        ("pattern='Player'",
         "SELECT * FROM types WHERE name LIKE '%Player%' ORDER BY nested_name"),
    ):
        t0 = time.perf_counter()
        rows = list(idx.conn.execute(sql))
        t1 = time.perf_counter()
        # ② 解析这一页：P1-3 之后，工具路径**只**做这一步
        [idx._row_to_type(r) for r in rows[:20]]
        t2 = time.perf_counter()
        # ③ 修改前会额外把全部行解析进句柄；现在不再发生，
        #    这里只是把「已省掉的那部分」量出来做对照。
        [idx._row_to_type(r) for r in rows]
        t3 = time.perf_counter()
        before = ms(t0, t3)
        after = ms(t0, t2)
        saved = ms(t2, t3)
        print(f"  {tag}   行数 {len(rows):,}")
        print(f"    ① SQL 取回                 {ms(t0, t1):9.1f} ms")
        print(f"    ② 解析这一页（20 条）      {ms(t1, t2):9.1f} ms"
              f"   ← P1-3 之后只剩这一步")
        print(f"    ③ 全量进句柄               {saved:9.1f} ms"
              f"   ← P1-3 已省掉（原占 {saved / max(before, 1e-9) * 100:.0f}%）")
        print(f"    修改前合计 ≈ ①+②+③        {before:9.1f} ms")
        print(f"    现在合计   ≈ ①+②          {after:9.1f} ms"
              f"   （省 {saved / max(before, 1e-9) * 100:.0f}%）")

    # 真实工具路径：耗时应 ≈ ①+②，与结果集大小基本无关
    dt, r = best(lambda: idx.search_classes("", limit=20))
    print(f"  真实 search_classes('', limit=20)  {dt:9.1f} ms"
          f"   命中 {r['total']:,}")
    print(f"    （「只解析 20 条而非 {r['total']:,} 条」由 "
          f"devtools/verify_lazy_handles.py 计数断言把关）")

    print("-" * 76)
    print(f"[P1-4] 继承过滤成本  ——  {label}")
    print("-" * 76)
    # 这里**故意不用 `best()`**：`best()` 取 N 次最小值，适合量「单条路径的
    # 最优成本」，但不适合量「两条路径之间的微小差值」——取最小值等于专挑
    # 噪声里最有利的那一次，符号都能翻。实测踩过：同一个查询两次跑分别得到
    # −4.9 ms 与 +14.7 ms，于是文档先写成「秒级」、又错改成「甚至为负」。
    # 所以改成跑多轮、打印**分布**（最小/中位/最大），让开销的大小与抖动
    # 一起暴露出来。
    ROUNDS = 12
    ps: list[float] = []
    ss: list[float] = []
    a = b = None
    for _ in range(ROUNDS):
        t0 = time.perf_counter()
        a = idx.search_classes("", limit=20)
        ps.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        b = idx.search_classes("", limit=20,
                               subtype_of="UnityEngine.MonoBehaviour")
        ss.append((time.perf_counter() - t0) * 1000)
    ds = [s - p for p, s in zip(ps, ss)]
    lab1 = "pattern=''"
    lab2 = "pattern='' + subtype_of"
    lab3 = f"→ 净增（{ROUNDS} 轮）"
    print(f"  {'':26}{'最小':>9}{'中位':>9}{'最大':>9}")
    print(f"  {lab1:26}{min(ps):9.1f}{statistics.median(ps):9.1f}"
          f"{max(ps):9.1f}   命中 {a['total']:,}")
    print(f"  {lab2:26}{min(ss):9.1f}"
          f"{statistics.median(ss):9.1f}{max(ss):9.1f}   命中 {b['total']:,}")
    print(f"  {lab3:26}{min(ds):+9.1f}"
          f"{statistics.median(ds):+9.1f}{max(ds):+9.1f}")
    if min(ds) < 0 < max(ds):
        sign = "有正有负 → 与噪声同量级，别报一个精确数字"
    elif min(ds) > 0:
        sign = "全为正 → 是真开销"
    else:
        sign = "全为负 → 过滤免费"
    print(f"  → 符号：{sign}")
    # P1-4 之后必须把「首轮」单列：第一轮要建邻接表（一次 O(类型数)），
    # 之后才是不变量。混在一起看会误以为过滤本身很贵。
    rest = statistics.median(ds[1:]) if len(ds) > 1 else ds[0]
    print(f"  → 首轮（含建邻接表）净增 {ds[0]:+.1f} ms；"
          f"其后 {len(ds) - 1} 轮中位 {rest:+.1f} ms")
    print(f"  → 抖动 {max(ds) - min(ds):.1f} ms（文档 §4.8 曾称「秒级」；"
          f"但也不该只看一个数）")

    print("-" * 76)
    print(f"[参考] 各查询耗时（{REPEATS} 次取最小）  ——  {label}")
    print("-" * 76)
    for name, fn in (
        ("search_classes('Player')", lambda: idx.search_classes("Player", limit=20)),
        ("search_classes('')", lambda: idx.search_classes("", limit=20)),
        ("search_fields('a')", lambda: idx.search_fields("a", limit=20)),
        ("search_methods('a')", lambda: idx.search_methods("a", limit=20)),
        ("find_derived_classes(MonoBehaviour)",
         lambda: idx.find_derived_classes("UnityEngine.MonoBehaviour")),
        ("class_hierarchy(down, depth=1)",
         lambda: idx.class_hierarchy("UnityEngine.MonoBehaviour",
                                     direction="down", depth=1)),
    ):
        dt, _ = best(fn)
        print(f"  {name:<42} {dt:9.1f} ms")

    idx.close()


def bench_dup_parse() -> None:
    """P1-5：重复解析 —— 同一次调用里把同一份数据解析两遍。

    每个场景都量**修复前的写法**与**修复后的写法**，直接给出省下多少。
    """
    print()
    print("=" * 76)
    print("[P1-5] 重复解析成本（修复前 vs 修复后）")
    print("=" * 76)

    print("-" * 76)
    print("① inspect_binary：PE 被解析几遍")
    print("-" * 76)
    game = samples.find_il2cpp()
    if game is None:
        print("  IL2CPP 样本不在本机，无法验证（跳过）")
    else:
        exe = samples.game_exe(game)
        print(f"  样本 {exe.name}")
        n = 5
        t0 = time.perf_counter()
        for _ in range(n):
            pe.parse(exe)
        t1 = time.perf_counter()
        for _ in range(n):
            pe.summarize(exe)
        t2 = time.perf_counter()
        p_ms = ms(t0, t1) / n
        s_ms = ms(t1, t2) / n
        print(f"  pe.parse       单次 {p_ms:7.2f} ms")
        print(f"  pe.summarize   单次 {s_ms:7.2f} ms"
              f"   ← 内部又 parse 了一遍")
        print(f"  修复前 state.inspect_binary = pe_parse + pe_summarize"
              f" = {p_ms + s_ms:7.2f} ms（其中 {p_ms:.2f} ms 白付）")
        print(f"  修复后 state.inspect_binary = pe_summarize"
              f"        = {s_ms:7.2f} ms  → 省 {p_ms:.2f} ms")

    print("-" * 76)
    print("② ue_list_entries：容器索引被解析几遍")
    print("-" * 76)
    for label, finder in (("pak v12 (CatapultHero)", samples.find_ue_pak_v12),
                          ("pak v11 (Dinoblade)", samples.find_ue_pak_v11)):
        g = finder()
        if g is None:
            print(f"  {label}: 样本不在本机，无法验证（跳过）")
            continue
        utocs = sorted(g.rglob("*.utoc"))
        paks = sorted(g.rglob("*.pak"))
        print(f"  {label}:  pak={len(paks)}  utoc={len(utocs)}")
        for p in (utocs[:1] + paks[:1]):
            is_utoc = p.suffix.lower() == ".utoc"
            read = ue.read_iostore_index if is_utoc else ue.read_pak_index
            listing = ue.list_iostore_entries if is_utoc else ue.list_pak_entries

            # 修复前：read 一次判 ok，list 内部再 read 一次
            t0 = time.perf_counter()
            r1 = read(str(p))
            t1 = time.perf_counter()
            entries_before = listing(str(p))
            t2 = time.perf_counter()

            # 修复后：read 一次，list 复用它的结果
            t3 = time.perf_counter()
            r2 = read(str(p))
            t4 = time.perf_counter()
            entries_after = listing(str(p), report=r2)
            t5 = time.perf_counter()

            before = ms(t0, t2) / 1000
            after = ms(t3, t5) / 1000
            # 修复只应省时间，绝不该改变结果
            same = len(entries_before) == len(entries_after)
            print(f"    {p.name}  ({p.stat().st_size / 1024 / 1024:.1f} MB,"
                  f" 条目 {len(entries_after):,})")
            print(f"      read_*_index    {ms(t0, t1) / 1000:6.3f}s  "
                  f"ok={r1.get('ok')}")
            print(f"      修复前（两次解析）  {before:6.3f}s")
            print(f"      修复后（复用 report）{after:6.3f}s"
                  f"  → 省 {before - after:.3f}s"
                  f"（{(1 - after / max(before, 1e-9)) * 100:.0f}%）")
            print(f"      条目数一致：{same}"
                  f"（{len(entries_before):,} vs {len(entries_after):,}）"
                  + ("" if same else "  ← 不一致，修复有问题！"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="", choices=["", "il2cpp", "mono"],
                    help="只跑某一个引擎（默认两个都跑）")
    ap.add_argument("--only-dup", action="store_true",
                    help="只量 P1-5 重复解析（与引擎无关，跑一次即可）")
    args = ap.parse_args()

    if args.only_dup:
        bench_dup_parse()
        return 0

    ran = 0
    for key, label, finder, rel in CASES:
        if args.engine and args.engine != key:
            continue
        game = finder()
        print()
        print("=" * 76)
        if game is None:
            # 样本不在本机：如实记为「无法验证」，不假装跑过
            print(f"{label}: 样本不在本机，无法验证（跳过）")
            print("=" * 76)
            continue
        mpath = game / rel
        print(f"{label}   {mpath}")
        print(f"存在={mpath.is_file()}")
        print("=" * 76)
        bench_open(label, mpath)
        bench_query(label, mpath)
        ran += 1

    bench_dup_parse()

    if ran == 0:
        print("\n没有可用的样本，未产出任何数字。")
        return 2
    print()
    print("=" * 76)
    print("以上为实测值。本脚本没有「通过/失败」，只有数字 ——")
    print("性能改动的验收方式是「改前改后各跑一次并对比」。")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
