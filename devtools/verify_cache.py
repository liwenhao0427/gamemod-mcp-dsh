"""验收：**索引缓存的生命周期**（P3-2）—— 看得到、有边界、在用的不会被误删。

## 为什么需要它

缓存文件名 = `<引擎>-<元数据 sha256 前 16 位>.sqlite`。这个键选得很对
（换游戏/换版本必然重建，不会串数据），但它有一个**没人管过的副作用**：

    「玩过多少游戏」+「游戏更新过多少次」都会**永久**堆在缓存目录里。

实测一个 IL2CPP 游戏的索引 25.7 MB，而 Mod 开发本来就要反复更新游戏 ——
每次更新换一个新文件，旧文件再也不可能被命中。以前既没有清理，
也没有任何工具返回缓存占了多少（`get_diagnostics` 只报一个路径字符串）。

本脚本测三件事，**顺序就是重要性**：

1. **安全**（最重要）：清理**只许**动形状正确的缓存文件。
   缓存目录里可能有用户自己的东西，删文件是不可逆的 ——
   所以这里专门放一个「很大的非缓存文件」，证明它一根汗毛都不会掉。
2. **护栏**：未超上限不删、最近用过的几个不删、太新的不删、
   当前正开着的那个不删；且 `min_keep=0` 真的会删（防 `files[-0:]` 那种
   「看着像护栏、其实什么都保护」的反向 bug）。
3. **可见**：`cache_stats` / `index_info()["cache"]` 的数字必须与磁盘独立算出的
   一致；真删了才出现 `cache_evicted`，没删就不许出现。

## 反证（非空转）

- 把文件名正则放宽成 `.*` → 第 1 节那条「非缓存文件不会被删」**必须报红**。
- 把 `min_age_seconds` 调成 0 且上限调成 0 → **必须真的删掉**，
  证明第 2 节里那些「没删」的结论不是恒真。
- 把 `min_keep` 调成 0 → 必须删掉全部候选（含最近用过的那个）。

## 绝不碰用户的真实缓存

真实缓存目录（`~/.gamemod/cache`）在本脚本里**只读**：只统计、只在
默认护栏下跑一次 prune（默认护栏的结果必然是「什么都不删」），
并在事后复查文件数与总大小未变。所有删除类断言都在临时目录里做。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from gamemod_mcp import server as S                       # noqa: E402
from gamemod_mcp.engine import index as ix                # noqa: E402

PASS = 0
FAIL = 0
WARN = 0

MB = 1024 * 1024


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


def make_cache_file(d: Path, engine: str, digest: str, nbytes: int,
                    age_seconds: float = 0.0) -> Path:
    """造一个**形状正确**的假缓存文件（内容是填充字节，不要求是真 sqlite）。"""
    p = d / f"{engine}-{digest}.sqlite"
    p.write_bytes(b"\0" * nbytes)
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(p, (old, old))
    return p


def dir_signature(d: Path) -> tuple[int, int]:
    """目录的（文件数, 总字节）—— 用于证明「测试没动过真实缓存」。"""
    files = [p for p in d.glob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


# ================================================================ 1. 安全
def test_safety(tmp: Path) -> None:
    section("1. 安全 —— 只许动形状正确的缓存文件（删文件不可逆）")
    d = tmp / "shape"
    d.mkdir(parents=True, exist_ok=True)

    good = [
        "il2cpp-b6304a75b752e1ac.sqlite",
        "mono-27b7ee933801e9b2.sqlite",
        "ue-0123456789abcdef.sqlite",
    ]
    bad = [
        "notes.txt",                                 # 用户自己的东西
        "il2cpp-XXX.sqlite",                         # 摘要不是十六进制
        "il2cpp-b6304a75b752e1a.sqlite",             # 摘要只有 15 位
        "il2cpp-b6304a75b752e1ac.sqlite.bak",        # 备份
        "il2cpp-b6304a75b752e1ac.sqlite-journal",    # sqlite 事务日志
        "IL2CPP-b6304a75b752e1ac.sqlite",            # 引擎名大小写不对
        "cache.db",                                  # 别的工具
    ]
    for n in good:
        (d / n).write_bytes(b"\0" * 1024)
    for n in bad:
        (d / n).write_bytes(b"\0" * 1024)

    found = {p.name for p in ix._cache_files(d)}
    check("形状正确的三个都被认出来", found == set(good),
          f"认出 {sorted(found)}")
    check("形状不对的一个都没被当成缓存（共 %d 个）" % len(bad),
          not (found & set(bad)),
          f"误认：{sorted(found & set(bad))}" if found & set(bad)
          else f"正确忽略：{sorted(bad)}")

    # 关键安全断言：一个**巨大**的非缓存文件，在「严重超限」时也不许被删。
    d2 = tmp / "safety"
    d2.mkdir(parents=True, exist_ok=True)
    victim = d2 / "我的笔记.txt"
    victim.write_bytes(b"x" * (8 * MB))
    make_cache_file(d2, "il2cpp", "a" * 16, 8 * MB, age_seconds=400 * 86400)

    r = ix.prune_cache(d2, max_total_bytes=1, min_keep=0, min_age_seconds=0)
    check("超限到极致时，非缓存文件依然**一根汗毛都没动**",
          victim.is_file() and victim.stat().st_size == 8 * MB,
          f"受害者仍在={victim.is_file()}，大小={victim.stat().st_size if victim.is_file() else 0}")
    check("同一轮里形状正确的老缓存**确实被删了**（证明上面不是「什么都没做」）",
          r["deleted"] == ["il2cpp-" + "a" * 16 + ".sqlite"],
          f"deleted={r['deleted']}")

    # 非空转反证：把正则放宽成「什么都匹配」，受害者必须被删 ——
    # 若这一步删不掉，说明上面的安全断言其实测不到东西。
    orig_re = ix._CACHE_NAME_RE
    try:
        ix._CACHE_NAME_RE = __import__("re").compile(r".*")
        d3 = tmp / "falsify"
        d3.mkdir(parents=True, exist_ok=True)
        v3 = d3 / "我的笔记.txt"
        v3.write_bytes(b"x" * (8 * MB))
        make_cache_file(d3, "il2cpp", "b" * 16, 8 * MB, age_seconds=400 * 86400)
        r3 = ix.prune_cache(d3, max_total_bytes=1, min_keep=0, min_age_seconds=0)
        check("反证：正则放宽成 .* 后，非缓存文件**会**被删",
              not v3.is_file(),
              f"放宽后 deleted={r3['deleted']} —— 说明第 1 节的安全断言确实在起作用")
    finally:
        ix._CACHE_NAME_RE = orig_re
    check("反证后正则已复原", ix._CACHE_NAME_RE is orig_re)


# ================================================================ 2. 统计
def test_stats(tmp: Path) -> None:
    section("2. cache_stats —— 数字必须与磁盘独立算出的一致")
    d = tmp / "stats"
    d.mkdir(parents=True, exist_ok=True)

    sizes = [3 * MB, 1 * MB, 5 * MB]
    for i, n in enumerate(sizes):
        make_cache_file(d, "il2cpp", f"{i:016x}", n, age_seconds=(3 - i) * 86400)
    # 非缓存文件**不该**被算进缓存占用
    (d / "readme.md").write_bytes(b"y" * (2 * MB))

    st = ix.cache_stats(d)
    check("files 只数形状正确的（3 个，不含 readme.md）", st["files"] == 3,
          f"files={st['files']}")
    check("total_bytes 等于那 3 个的大小之和", st["total_bytes"] == sum(sizes),
          f"报告 {st['total_bytes']}，独立算出 {sum(sizes)}")
    check("dir 就是传入的目录", Path(st["dir"]) == d, st["dir"])

    empty = tmp / "not-there"
    st2 = ix.cache_stats(empty)
    check("目录不存在时如实报 0，而不是抛异常",
          st2["files"] == 0 and st2["total_bytes"] == 0,
          f"{st2}")


# ================================================================ 3. 护栏
def test_guards(tmp: Path) -> None:
    section("3. prune_cache 的护栏 —— 未超限不删、近的不删、开着的不删")

    # 3.1 未超上限：一个都不许删
    d = tmp / "under"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        make_cache_file(d, "il2cpp", f"{i:016x}", 4 * MB, age_seconds=100 * 86400)
    r = ix.prune_cache(d, max_total_bytes=100 * MB)
    check("总大小未超上限 → 一个都没删", r["deleted"] == [],
          f"deleted={r['deleted']}，总量 {r['total_bytes_before']}")
    check("如实给出 skipped 原因（而不是留空让人猜）",
          bool(r.get("skipped")), r.get("skipped", ""))
    check("三个文件都还在", len(ix._cache_files(d)) == 3)

    # 3.2 超上限：从**最久没用**的一端删，删到不超为止
    d2 = tmp / "over"
    d2.mkdir(parents=True, exist_ok=True)
    # 造 6 个各 5 MB（= 30 MB）的老文件，mtime 依次变新：00 最老
    names = [f"{i:016x}" for i in range(6)]
    for i, nm in enumerate(names):
        make_cache_file(d2, "il2cpp", nm, 5 * MB,
                        age_seconds=(100 - i * 10) * 86400)
    r2 = ix.prune_cache(d2, max_total_bytes=12 * MB, min_keep=0,
                        min_age_seconds=0)
    expect = [f"il2cpp-{n}.sqlite" for n in names[:4]]   # 最老的 4 个
    check("超限时按「最久没用优先」删（最老的 4 个）", r2["deleted"] == expect,
          f"deleted={r2['deleted']}")
    check("删到总量回到上限以内", r2["total_bytes_after"] <= 12 * MB,
          f"after={r2['total_bytes_after']} 上限={12 * MB}")
    check("剩下的正好是最近用过的 2 个",
          {p.name for p in ix._cache_files(d2)} ==
          {f"il2cpp-{n}.sqlite" for n in names[4:]},
          f"剩下 {sorted(p.name for p in ix._cache_files(d2))}")
    check("freed_bytes 与「删掉的文件大小之和」一致",
          r2["freed_bytes"] == 4 * 5 * MB, f"freed={r2['freed_bytes']}")
    check("total_bytes_before / after 自洽（before - freed == after）",
          r2["total_bytes_before"] - r2["freed_bytes"] == r2["total_bytes_after"],
          f"{r2['total_bytes_before']} - {r2['freed_bytes']} = "
          f"{r2['total_bytes_after']}")
    real = sum(p.stat().st_size for p in ix._cache_files(d2))
    check("after 与磁盘实际占用一致", real == r2["total_bytes_after"],
          f"磁盘 {real}，报告 {r2['total_bytes_after']}")

    # 3.3 幂等：再跑一次不该删东西
    r3 = ix.prune_cache(d2, max_total_bytes=12 * MB, min_keep=0, min_age_seconds=0)
    check("同一条件再跑一次 → 没得删了（幂等）", r3["deleted"] == [],
          f"deleted={r3['deleted']}")

    # 3.4 min_keep 保护最近用过的 N 个
    d4 = tmp / "keep"
    d4.mkdir(parents=True, exist_ok=True)
    knames = [f"{i:016x}" for i in range(6)]
    for i, nm in enumerate(knames):
        make_cache_file(d4, "mono", nm, 5 * MB, age_seconds=(100 - i * 10) * 86400)
    r4 = ix.prune_cache(d4, max_total_bytes=1, min_keep=4, min_age_seconds=0)
    check("min_keep=4：最近用过的 4 个受保护，只删最老的 2 个",
          r4["deleted"] == [f"mono-{n}.sqlite" for n in knames[:2]],
          f"deleted={r4['deleted']}")
    check("受保护的 4 个确实都还在",
          {p.name for p in ix._cache_files(d4)} ==
          {f"mono-{n}.sqlite" for n in knames[2:]},
          f"剩下 {sorted(p.name for p in ix._cache_files(d4))}")
    check("就算还超上限也停手（护栏优先于上限）",
          r4["total_bytes_after"] > 1 and not r4["deleted"][2:],
          f"after={r4['total_bytes_after']}（仍超上限，但只剩受保护的）")

    # 3.5 min_age 保护太新的
    d5 = tmp / "age"
    d5.mkdir(parents=True, exist_ok=True)
    make_cache_file(d5, "il2cpp", "1" * 16, 5 * MB, age_seconds=1 * 3600)   # 1 小时前
    make_cache_file(d5, "il2cpp", "2" * 16, 5 * MB, age_seconds=30 * 86400)  # 30 天前
    r5 = ix.prune_cache(d5, max_total_bytes=1, min_keep=0,
                        min_age_seconds=7 * 86400)
    check("7 天内用过的（1 小时前）不删", "il2cpp-" + "1" * 16 + ".sqlite"
          not in r5["deleted"], f"deleted={r5['deleted']}")
    check("很久没用的（30 天前）才删",
          r5["deleted"] == ["il2cpp-" + "2" * 16 + ".sqlite"],
          f"deleted={r5['deleted']}")

    # 3.6 protect：正开着的那个绝不删（哪怕它最老、而且超限）
    d6 = tmp / "protect"
    d6.mkdir(parents=True, exist_ok=True)
    old = make_cache_file(d6, "il2cpp", "f" * 16, 9 * MB, age_seconds=999 * 86400)
    make_cache_file(d6, "il2cpp", "e" * 16, 9 * MB, age_seconds=998 * 86400)
    r6 = ix.prune_cache(d6, protect=[old.name], max_total_bytes=1,
                        min_keep=0, min_age_seconds=0)
    check("被 protect 的那个（最老、开着）不删", old.is_file(),
          f"deleted={r6['deleted']}")
    check("protect 之外的老缓存照删",
          r6["deleted"] == ["il2cpp-" + "e" * 16 + ".sqlite"],
          f"deleted={r6['deleted']}")

    # 3.7 min_keep=0 必须真的会删（防 `files[-0:]` 把全部当成受保护）
    d7 = tmp / "keepzero"
    d7.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        make_cache_file(d7, "ue", f"{i:016x}", 4 * MB, age_seconds=100 * 86400)
    r7 = ix.prune_cache(d7, max_total_bytes=1, min_keep=0, min_age_seconds=0)
    check("min_keep=0 → 真的会把候选全删光（不是「看着像护栏」）",
          len(r7["deleted"]) == 3 and not ix._cache_files(d7),
          f"deleted={len(r7['deleted'])} 个，剩下 {len(ix._cache_files(d7))} 个")

    # 非空转反证：默认护栏（min_age=7 天）下，上面这批「很老」的文件才会被删；
    # 把 min_age 调到无穷大，同一个目录必须一个都删不掉。
    d8 = tmp / "falsify-age"
    d8.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        make_cache_file(d8, "ue", f"{i:016x}", 4 * MB, age_seconds=100 * 86400)
    r8 = ix.prune_cache(d8, max_total_bytes=1, min_keep=0,
                        min_age_seconds=10 ** 9)
    check("反证：min_age 调到极大后一个都删不掉 → 上面「会删」的结论非恒真",
          r8["deleted"] == [] and len(ix._cache_files(d8)) == 3,
          f"deleted={r8['deleted']}，skipped={r8.get('skipped', '')[:40]}")


# ================================================================ 4. 端到端
def test_e2e(tmp: Path) -> bool:
    section("4. 端到端 —— open 时真的会清理、命中时真的会 touch、返回里看得见")
    mono = samples.find_mono()
    if not samples.require(mono, "Mono (Archon Soul)"):
        return False
    asm = None
    for cand in (mono / "Archon Soul_Data" / "Managed").glob("*.dll"):
        if cand.name == "Assembly-CSharp.dll":
            asm = cand
    if asm is None:
        print("  \u26a0\ufe0f  无法验证：样本里没有 Assembly-CSharp.dll")
        return False

    cache = tmp / "e2e"
    cache.mkdir(parents=True, exist_ok=True)
    prior = os.environ.get("GAMEMOD_CACHE_DIR")

    # 4.1 open 会调用 prune_cache（用 monkeypatch 记录调用点，证明真的接上了）
    calls: list[str] = []
    orig_prune = ix.prune_cache

    def spy(cache_dir=None, **kw):
        calls.append(str(cache_dir))
        return orig_prune(cache_dir, **kw)

    ix.prune_cache = spy
    try:
        idx = ix.Index.open(asm, cache_dir=cache)
    finally:
        ix.prune_cache = orig_prune
    check("open 时调用了 prune_cache（新键没有构建索引）",
          calls == [str(cache)], f"calls={calls}")
    info = idx.index_info()
    check("index_info 里有 cache 统计", isinstance(info.get("cache"), dict),
          f"{info.get('cache')}")
    check("cache.files 与磁盘一致", info["cache"]["files"] == 1,
          f"files={info['cache']['files']}")
    check("cache.total_bytes 等于那个库的大小",
          info["cache"]["total_bytes"] == idx.db_path.stat().st_size,
          f"{info['cache']['total_bytes']} vs {idx.db_path.stat().st_size}")
    check("这次没删东西 → 不许出现 cache_evicted（不给恒定噪声字段）",
          "cache_evicted" not in info,
          f"键={sorted(info.keys())[:6]}...")
    idx.close()

    # 4.2 命中缓存时必须 touch（否则 LRU 会把天天在用的缓存先删掉）
    old = time.time() - 30 * 86400
    os.utime(idx.db_path, (old, old))
    mtime_before = idx.db_path.stat().st_mtime
    idx2 = ix.Index.open(asm, cache_dir=cache)
    hit = bool(idx2.cache_hit)
    mtime_after = idx2.db_path.stat().st_mtime
    check("第二次打开确实是缓存命中（前提成立）", hit, f"cache_hit={hit}")
    check("命中时 mtime 被刷新（30 天前 → 现在）",
          mtime_after > mtime_before + 20 * 86400,
          f"before={time.strftime('%Y-%m-%d', time.localtime(mtime_before))} "
          f"after={time.strftime('%Y-%m-%d', time.localtime(mtime_after))}")
    idx2.close()

    # 4.3 真删了才出现 cache_evicted（把上限压到 1 字节、去掉 min_keep 护栏，
    #     触发一次真清理；此时唯一还能保命的就是「正开着的那个」）
    make_cache_file(cache, "ue", "c" * 16, 6 * MB, age_seconds=400 * 86400)
    make_cache_file(cache, "mono", "d" * 16, 6 * MB, age_seconds=300 * 86400)
    calls.clear()
    ix.prune_cache = lambda cache_dir=None, **kw: orig_prune(
        cache_dir, **{**kw, "max_total_bytes": 1, "min_keep": 0,
                      "min_age_seconds": 0})
    try:
        idx3 = ix.Index.open(asm, cache_dir=cache)
    finally:
        ix.prune_cache = orig_prune
    info3 = idx3.index_info()
    check("超限被清理时，index_info 如实给出 cache_evicted",
          isinstance(info3.get("cache_evicted"), dict),
          f"{info3.get('cache_evicted')}")
    check("cache_evicted.files 非空且是真删掉的名字",
          bool(info3.get("cache_evicted", {}).get("files")),
          f"files={info3.get('cache_evicted', {}).get('files')}")
    check("cache_evicted 报的上限是**这一次实际用的**（1 字节），不是模块默认值",
          info3.get("cache_evicted", {}).get("limit_bytes") == 1,
          f"limit_bytes={info3.get('cache_evicted', {}).get('limit_bytes')}；"
          f"模块默认是 {ix.CACHE_MAX_TOTAL_BYTES}")
    check("cache_evicted.reason 里说的上限与 limit_bytes 一致（报告不许说谎）",
          "1 字节" in str(info3.get("cache_evicted", {}).get("reason", "")),
          f"reason={info3.get('cache_evicted', {}).get('reason')}")
    check("当前正开着的库**不在**被删名单里",
          idx3.db_path.name not in
          (info3.get("cache_evicted", {}).get("files") or []),
          f"正开着 {idx3.db_path.name}")
    check("清理后 index_info 的 cache 统计与磁盘一致",
          info3["cache"]["files"] == len(ix._cache_files(cache)),
          f"报告 {info3['cache']['files']}，磁盘 {len(ix._cache_files(cache))}")
    idx3.close()

    # 4.4 open_game（工具层）也要透出 cache 统计
    if prior is None:
        os.environ["GAMEMOD_CACHE_DIR"] = str(cache)
    try:
        og = S.open_game(metadata_path=str(asm), engine="mono")
        idx_info = (og or {}).get("index") or {}
        check("open_game 的 index 概况里有 cache 统计",
              isinstance(idx_info.get("cache"), dict),
              f"cache={idx_info.get('cache')}")
        check("open_game 报的 cache 与直接统计一致",
              (idx_info.get("cache") or {}).get("total_bytes") ==
              ix.cache_stats(cache)["total_bytes"],
              f"{idx_info.get('cache')}")
    finally:
        if prior is None:
            os.environ.pop("GAMEMOD_CACHE_DIR", None)
        S.close_session()
    return True


# ================================================================ 5. 真实缓存
def test_real_readonly() -> None:
    section("5. 真实缓存目录 —— **只读**，且默认护栏下不许删任何东西")
    real = ix.default_cache_dir()
    if not real.is_dir():
        print(f"  \u26a0\ufe0f  无法验证：本机没有 {real}")
        return

    before = dir_signature(real)
    st = ix.cache_stats(real)
    sqlite_total = sum(p.stat().st_size for p in real.glob("*.sqlite"))
    check("cache_stats 的总量与独立点算的 *.sqlite 总量一致",
          st["total_bytes"] == sqlite_total,
          f"{st['total_bytes']} / {sqlite_total} 字节（{st['total_bytes'] / MB:.1f} MB）")
    check("真实缓存里没有「形状不对」的文件被误当缓存",
          st["files"] == len(list(real.glob("*.sqlite"))),
          f"认出 {st['files']} 个，实际 *.sqlite {len(list(real.glob('*.sqlite')))} 个")

    # 用**默认**护栏跑一次 —— 必须什么都不删（否则就是在删用户的数据）
    r = ix.prune_cache(real)
    check("默认护栏下真实缓存一个都没删", r["deleted"] == [],
          f"deleted={r['deleted']}，skipped={str(r.get('skipped'))[:56]}")
    after = dir_signature(real)
    check("真实缓存目录跑完后（文件数, 总字节）完全未变", before == after,
          f"前 {before} / 后 {after}")


# ================================================================ 6. 诊断字段
def test_diagnostics() -> None:
    section("6. get_diagnostics —— 缓存占用必须看得见")
    d = S.get_diagnostics()
    check("diagnostics 返回 ok", d.get("ok") is True, d.get("error", ""))
    cache = d.get("cache")
    check("有 cache 字段且是 dict", isinstance(cache, dict), f"{cache}")
    if isinstance(cache, dict):
        check("cache 里有 dir / files / total_bytes",
              {"dir", "files", "total_bytes"} <= set(cache),
              f"键={sorted(cache)}")
        check("files / total_bytes 是数字（不是字符串）",
              isinstance(cache.get("files"), int)
              and isinstance(cache.get("total_bytes"), int),
              f"files={cache.get('files')!r} total_bytes={cache.get('total_bytes')!r}")
        real = ix.cache_stats()
        check("报的就是本机默认缓存目录的真数",
              cache.get("total_bytes") == real["total_bytes"]
              and cache.get("dir") == real["dir"],
              f"{cache}")
    check("旧的 cache_dir（只有一个路径字符串）已移除 —— 它看不到占用",
          "cache_dir" not in d,
          "改为 cache={dir, files, total_bytes}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gamemod-cache-verify-"))
    try:
        print(f"临时工作目录：{tmp}（所有删除类断言都在这里做）")
        test_safety(tmp)
        test_stats(tmp)
        test_guards(tmp)
        test_e2e(tmp)
        test_real_readonly()
        test_diagnostics()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("=" * 78)
    if FAIL:
        print(f"\u274c 通过 {PASS} 项，失败 {FAIL} 项（警告 {WARN}）")
    else:
        print(f"\u2705 通过 {PASS} 项，失败 0 项"
              + (f"（警告 {WARN}）" if WARN else ""))
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
