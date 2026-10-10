"""索引层：把解析出的元数据落成可高速查询的 SQLite 库（带缓存）。

为什么要单独做索引层
--------------------------------------------------
`global-metadata.dat` 有 17 MB、2 万个类、13 万个方法。
每次 Agent 提问都重新解析一遍是不可接受的（约 2 秒），
而 Agent 在一次任务里会问几十上百次。

因此这里做两件事：
    1. **持久化缓存**：以元数据文件的 sha256 为键，建一次 SQLite，之后秒开。
    2. **进程内缓存**：同一个 session 内复用已打开的连接。

关于 `read_query_result`
--------------------------------------------------
AiMod 的同类工具本质上解决同一个问题：**工具返回值不能太大**。
Agent 的上下文有限，一次返回 13 万个方法会把上下文炸掉。
本项目的做法与之等价但更简单：
    所有列表类查询都返回 `{items, total, offset, limit, has_more}`，
    并且把完整结果写进一个 **查询结果句柄表**，
    之后可用 `read_query_result(handle, offset, limit, pattern="")` 翻页；
    给 `pattern` 即在同一结果内部再过滤（原先独立的
    `search_query_result` 就是它在 `pattern=""` 时的特例，已按契约第 3 条合并）。
这样既保证单次返回精简，又保证信息不丢失。

句柄的命名与**生命周期**（P2-4）
--------------------------------------------------
句柄形如 `il2cpp-g3-classes-1`，三段各有用途：

    il2cpp   引擎名 —— 给人看：拿着句柄能看出它属于哪个会话；
    g3       **代次号**，进程内每新建一个句柄表就 +1；
    classes-1 原来的「结果种类 + 序号」。

`g3` 这一段不是装饰，是**修 bug 用的**。`ResultStore` 是 `Index` 的实例属性，
切游戏就换一个实例、序号从 0 重来，于是旧会话的 `classes-1` 会在新会话里
**静默命中另一份结果集**。实测（修之前）：

    IL2CPP 游戏 search_classes('Player') -> 句柄 'classes-1'，首条 PlayerPrefsController
    切到 Mono 游戏 search_classes('a')   -> 句柄 'classes-1'，首条 __StaticArrayInitTypeSize=26136
    再读 IL2CPP 那个句柄                 -> ok=True，读出来的是 **Mono 的那一条**

`ok=True` 但数据是另一个游戏的 —— 正是本项目最不能接受的一类缺陷。
带上代次号后，旧句柄在新会话里查不到，会如实报「不存在或已过期」。
"""

from __future__ import annotations

import hashlib
import itertools
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from ..logutil import progress, warn
from . import adapt
from .il2cpp_meta import METADATA_MAGIC, Metadata, MetadataError
from .mono_meta import Metadata as MonoMetadata

# ---------------------------------------------------------------- 缓存目录

# 2：types 表增加 nested_name（嵌套感知全名）、declaring_il2cpp 列；
#    字段类型改由 Metadata.resolve_field_type 解析（索引约定与其它成员不同）
SCHEMA_VERSION = 2


def default_cache_dir() -> Path:
    """默认缓存目录：用户主目录下的 .gamemod/cache。"""
    base = os.environ.get("GAMEMOD_CACHE_DIR")
    if base:
        return Path(base)
    return Path.home() / ".gamemod" / "cache"


# ---------------------------------------------------------------- 缓存生命周期
#
# 缓存文件名 = `<引擎>-<元数据 sha256 前 16 位>.sqlite`，于是：
#   · 同一个游戏 + 同一份元数据 → 同一个文件（命中复用）；
#   · 游戏一更新（元数据变了）→ **换一个新文件**，旧文件再也不会被命中。
# 所以「玩过多少游戏」加上「游戏更新过多少次」都会永久堆在缓存目录里，
# 既没人清理，也没有任何地方报告它占了多少 —— 一个 26 MB 起步的纯泄漏
# （实测一个 IL2CPP 游戏的索引 25.7 MB，而且 Mod 开发本来就要反复更新游戏）。
#
# 这里补三件事：**看得到**（`cache_stats`）、**有边界**（`prune_cache`）、
# **在用的不会被误删**（命中时 touch，让 mtime 成为真的「最后使用时间」）。

#: 缓存文件名的形状。**只清理严格符合这个形状的文件**：缓存目录里可能有
#: 别的东西（用户自己的笔记、别的工具的数据、半截的临时文件），
#: 而删文件是不可逆的 —— 宁可漏删，不可错删。
_CACHE_NAME_RE = re.compile(r"^(il2cpp|mono|ue)-[0-9a-f]{16}\.sqlite$")

#: 自动清理的保守边界。刻意留得宽：删掉一个以后还要用的缓存，
#: 代价是重新解析 0.5~12 秒；多留一个的代价只是 26 MB。
#: 按这三个数，5 个游戏的 130 MB 永远不会被碰。
CACHE_MAX_TOTAL_BYTES = 2 * 1024 ** 3   # 2 GiB
CACHE_MIN_KEEP = 4                      # 无论超多少，最近用过的 4 个都不删
CACHE_MIN_AGE_SECONDS = 7 * 86400       # 7 天内用过的都不删


def _cache_files(cache_dir: str | Path | None = None) -> list[Path]:
    """缓存目录里**形状正确**的索引文件，按 mtime 升序（最久没用的在前）。

    目录不存在或读不了 → 返回空列表（这不是错误：缓存目录本来就允许不存在）。
    """
    cache = Path(cache_dir) if cache_dir else default_cache_dir()
    try:
        entries = list(cache.iterdir())
    except OSError:
        return []
    out: list[Path] = []
    for p in entries:
        if not _CACHE_NAME_RE.match(p.name):
            continue
        try:
            if p.is_file():
                out.append(p)
        except OSError:
            continue

    def _mtime(p: Path) -> float:
        # 排序键也要能容错：iterdir 与 stat 之间文件可能正好被删掉。
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(out, key=_mtime)


def cache_stats(cache_dir: str | Path | None = None) -> dict[str, Any]:
    """缓存占用概况（**只读**，不删任何东西）。

    给 `get_diagnostics` / `index_info` 用：缓存是个会一直长大的目录，
    不报出来就等于让用户自己猜磁盘去哪了。
    """
    cache = Path(cache_dir) if cache_dir else default_cache_dir()
    files = _cache_files(cache)
    total = 0
    for p in files:
        try:
            total += p.stat().st_size
        except OSError:
            continue
    return {"dir": str(cache), "files": len(files), "total_bytes": total}


def _touch(path: Path) -> None:
    """把 mtime 更新为「现在」，即「这个缓存刚被用过」。

    为什么必须有：sqlite 的**读**不会改文件 mtime，而 `prune_cache` 的 LRU
    完全依赖 mtime。不 touch 的话，一个天天在用的缓存，在 LRU 眼里永远是
    「创建那一天用过的」，反而会先被删掉。
    失败就算了：只影响清理精度，不值得让「开索引」失败。
    """
    try:
        os.utime(path, None)
    except OSError:
        pass


def prune_cache(
    cache_dir: str | Path | None = None,
    *,
    protect: Iterable[str | Path] = (),
    max_total_bytes: int = CACHE_MAX_TOTAL_BYTES,
    min_keep: int = CACHE_MIN_KEEP,
    min_age_seconds: float = CACHE_MIN_AGE_SECONDS,
) -> dict[str, Any]:
    """按「最久没用」的顺序删缓存，直到总大小回到 `max_total_bytes` 以内。

    三条护栏（少任何一条都可能删掉还在用的东西）：

    1. **只删形状正确的文件**（`_CACHE_NAME_RE`）——其它一律不碰；
    2. `min_keep` 个最近用过的**永不删**，哪怕它们自己就超了上限；
    3. `min_age_seconds` 内用过的**永不删**（mtime 由命中时的 `_touch` 维护）。

    跳过某个文件不会让它后面的文件也失去机会（挨个判，不提前收工）。
    删不掉（例如 Windows 上文件被另一个进程占着）就记进 `errors`，
    **不**让整个操作失败 —— 已经有别的文件被删掉了，谎报成功更糟。

    返回的是 dict 而不是 bool：删了哪几个、释放了多少，都要看得见。
    """
    cache = Path(cache_dir) if cache_dir else default_cache_dir()
    files = _cache_files(cache)
    protect_names = {Path(p).name for p in protect}

    total = 0
    for p in files:
        try:
            total += p.stat().st_size
        except OSError:
            continue

    out: dict[str, Any] = {
        "ok": True,
        "dir": str(cache),
        "candidates": len(files),
        "total_bytes_before": total,
        "total_bytes_after": total,
        "deleted": [],
        "freed_bytes": 0,
        "errors": [],
        # 实际生效的边界（不是模块默认值）：调用方可能传了别的上限，
        # 报告里必须说**这一次**用了什么，否则就是拿默认值冒充实况。
        "max_total_bytes": max_total_bytes,
        "min_keep": min_keep,
        "min_age_seconds": min_age_seconds,
    }
    if total <= max_total_bytes:
        out["skipped"] = (
            f"总大小 {total} 字节未超过上限 {max_total_bytes}，无需清理")
        return out

    # 最后 min_keep 个（mtime 最大的）受保护。
    # 注意 min_keep=0 时 `files[-0:]` 是整个列表 —— 那会把**所有**文件都保护起来，
    # 一个也删不掉（看着像「护栏生效」，其实是 bug），所以这里显式分支。
    keep_names = ({p.name for p in files[len(files) - min_keep:]}
                  if min_keep > 0 else set())
    now = time.time()

    for p in files:                      # 已按 mtime 升序：最久没用的在最前
        if out["total_bytes_after"] <= max_total_bytes:
            break
        if p.name in keep_names or p.name in protect_names:
            continue
        try:
            st = p.stat()
        except OSError as exc:
            out["errors"].append(f"{p.name}: 读不到状态（{exc}）")
            continue
        if now - st.st_mtime < min_age_seconds:
            continue
        try:
            p.unlink()
        except OSError as exc:
            # 最常见：Windows 上这个库正被另一个进程打开着
            out["errors"].append(f"{p.name}: 删不掉（{exc}）")
            continue
        out["deleted"].append(p.name)
        out["freed_bytes"] += st.st_size
        out["total_bytes_after"] -= st.st_size

    if not out["deleted"]:
        out["skipped"] = "超过上限，但剩下的文件都受护栏保护（最近用过 / 太新 / 已保护）"
    return out


def _file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    """流式计算文件摘要（17 MB 文件约 30 ms）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- 查询结果句柄
#
# P1-3：句柄背后的结果**惰性物化**。
#
# 原来 `search_*` 会把命中的**全部**行都解析成 dict 塞进句柄，与 `limit`
# 无关 —— 实测 IL2CPP `search_classes('')` 里这一步占 45%（54.8 ms / 121.7 ms），
# 宽查询下纯属白干：调用方通常只要前 20 条。
#
# 现在句柄只登记「怎么取」（原始行 + 一个解析函数），翻页时才解析这一页。
# 注意：**不是**「把解析结果缓存起来」—— 那没省掉解析，只是挪了位置。


class _RowSource:
    """句柄背后的结果来源：数据库行 + 解析函数（P1-3 的主力）。

    行本身是廉价的（`sqlite3.Row` 只是个元组），昂贵的是把它解析成 dict：
    要查类型名、算字段类型置信度、解析方法返回值……所以只解析取到的那一页。

    契约就三条：`len()` 给条数，`slice()` 给一页，`all()` 全量物化
    （**只在** `pattern` 跨结果过滤时才会被调用 —— 即「用到才付钱」）。
    """

    def __init__(self, rows: list[sqlite3.Row],
                 convert: Callable[[sqlite3.Row], dict[str, Any]]):
        self._rows = rows
        self._convert = convert

    def __len__(self) -> int:
        return len(self._rows)

    def slice(self, offset: int, limit: int) -> list[dict[str, Any]]:
        return [self._convert(r) for r in self._rows[offset : offset + limit]]

    def all(self) -> list[dict[str, Any]]:
        return [self._convert(r) for r in self._rows]


def list_source(items: list[dict[str, Any]]) -> _RowSource:
    """把**已经物化好**的 dict 列表包成句柄可用的来源（P2-4 的响应上限用）。

    `_RowSource` 的契约本来就是「`len` + `slice` + `all`」，与背后是
    `sqlite3.Row` 还是 dict 无关，所以这里只要一个恒等解析函数，
    不必再新增一个类。做成模块级函数是因为它有**第二个调用方**
    （`cap.py`），不该从那边 import 私有名。
    """
    return _RowSource(list(items), lambda it: it)


@dataclass
class QueryResult:
    """一次查询的结果句柄。

    大结果集不会一次性返回，而是留在这里供后续翻页/过滤。
    """

    handle: str
    kind: str
    source: _RowSource

    @property
    def total(self) -> int:
        """结果集条数（受句柄上限约束的那一份，与工具顶层的全局 total 不同）。"""
        return len(self.source)

    def page(self, offset: int, limit: int,
             pattern: str = "") -> dict[str, Any]:
        """取一页；给了 pattern 就在结果内部先按子串过滤。

        合并了原来的 page / search 两个方法（契约第 3 条：一个是另一个的
        特例就合成一个，用参数区分）。`pattern=""` 时不过滤，即纯翻页。

        注意两条路径的成本**不一样**，这是刻意的：
        - `pattern=""` 只解析这一页（P1-3 的收益就在这条路上）；
        - `pattern=...` 要看每一条的值才能过滤，必须全量物化 —— 即
          「用到才付钱」。别把它当成「免费的二次过滤」。
        """
        offset = max(0, offset)
        limit = max(1, min(limit, 2000))
        if pattern:
            needle = pattern.lower()
            hits = [
                it for it in self.source.all()
                if needle in " ".join(str(v) for v in it.values()).lower()
            ]
            chunk = hits[offset : offset + limit]
            total = len(hits)
        else:
            chunk = self.source.slice(offset, limit)
            total = len(self.source)
        return {
            "handle": self.handle,
            "kind": self.kind,
            "pattern": pattern,
            "offset": offset,
            "limit": limit,
            "returned": len(chunk),
            "total": total,
            "has_more": offset + len(chunk) < total,
            "items": chunk,
        }


class ResultStore:
    """查询结果句柄表（进程内，有上限，超出后淘汰最旧的）。

    每个实例领一个**进程内唯一的代次号**并写进句柄，见模块上方
    「句柄的命名与生命周期」。这一条是正确性要求，不是可读性偏好：
    没有它，切游戏后旧句柄会静默读到另一个游戏的数据。
    """

    #: 代次号：进程内每新建一个句柄表就 +1，保证句柄字符串跨索引不重名。
    _EPOCH = itertools.count(1)

    def __init__(self, owner: str = "", max_entries: int = 32):
        self._max = max_entries
        self._items: dict[str, QueryResult] = {}
        self._counter = 0
        self._epoch = next(self._EPOCH)
        # owner 只影响可读性；**唯一性由代次号保证**（同一个引擎的两个游戏
        # owner 相同，只靠引擎名区分不开）。
        self._prefix = f"{owner or 'idx'}-g{self._epoch}-"

    @property
    def prefix(self) -> str:
        """句柄前缀（`{引擎}-g{代次}-`），仅供说明与验证脚本使用。"""
        return self._prefix

    def put(self, kind: str, source: _RowSource) -> QueryResult:
        """登记一份结果来源，返回句柄（**不物化**，见模块上方说明）。"""
        self._counter += 1
        handle = f"{self._prefix}{kind}-{self._counter}"
        qr = QueryResult(handle=handle, kind=kind, source=source)
        self._items[handle] = qr

        # 淘汰最旧的
        while len(self._items) > self._max:
            oldest = next(iter(self._items))
            del self._items[oldest]

        return qr

    def get(self, handle: str) -> QueryResult | None:
        return self._items.get(handle)

    def handles(self) -> list[str]:
        return list(self._items)

    def clear(self) -> None:
        """作废全部句柄（切游戏 / 关会话时用，见 `state.close_index`）。

        代次号**不重置** —— 这正是要点：清空后新句柄不会与旧的同名，
        于是「已经作废的句柄」永远是查不到的，不会静默命中新数据。
        """
        self._items.clear()


# ---------------------------------------------------------------- 索引主体


class Index:
    """IL2CPP 元数据索引。

    典型用法：
        with Index.open(metadata_path) as idx:
            idx.search_classes("Player")
    """

    def __init__(self, meta: Any, db_path: Path):
        self._attach(meta, db_path)
        # 这个索引是「复用既有缓存」还是「刚刚全量重建」——
        # 只有 open() 知道，所以由它赋值，默认按「新建」算。
        # 之前 index_info 里硬编码 True，导致重建一次（几秒到几十秒）
        # 也会报 cache_hit=true，Agent 据此以为走了快路径，无法察觉异常。
        self.cache_hit = False
        self._build()

    def _attach(self, meta: Any, db_path: Path,
                metadata_path: str | Path | None = None,
                engine: str = "") -> None:
        """设置一个索引实例的**全部**共享字段（不解构建）。

        单独抽出来是因为 `open()` 的「命中缓存」快路径走 `cls.__new__(cls)`，
        以前它在原地重复了一遍字段赋值 —— 于是任何人往 `__init__` 里加字段，
        缓存命中路径都会静默缺这个字段，而且**只在命中缓存时**才犯病。
        这个缺陷真实发生过：新增 `_child_map` 后，重建索引正常，复用缓存
        却报 `AttributeError: 'Index' object has no attribute '_child_map'`。

        现在字段只在这里列一次，两条路径都走它，不可能再漂移。

        **meta 允许为 None**，表示「缓存命中、尚未解析」（见 `meta` 属性）。
        这种惰性不是可有可无的优化：IL2CPP 20,072 类型的全量解析实测约
        480 ms，而缓存命中路径本来只需要读 8 字节的引擎嗅探。
        """
        # 注意：这里写的是 `_meta`，不是 `meta` —— 对外的 `meta` 是只读属性，
        # 直接赋值会 `AttributeError`。外部（含验证脚本）照旧用 `idx.meta`。
        self._meta = meta
        self._meta_path = Path(metadata_path) if metadata_path else None
        # 引擎名必须能在「没解析」时也知道，否则拼不出 db_path、也答不了能力问题。
        if engine:
            self.engine = engine
        elif meta is not None:
            self.engine = adapt.engine_of(meta)
        else:
            self.engine = ""
        self._closed = False
        self.db_path = db_path
        # 建库用的那条连接（只在**创建它的线程**里用，见 `conn` 属性）。
        self._primary_conn = sqlite3.connect(str(db_path))
        self._primary_conn.row_factory = sqlite3.Row
        #: 本索引创建时所在的线程 id —— `conn` 靠它判断能不能复用主连接。
        self._owner_tid = threading.get_ident()
        #: 每条线程各自的连接（`threading.local`），以及一份全量清单用于 close。
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = [self._primary_conn]
        self._conn_lock = threading.Lock()
        #: 惰性解析元数据时的锁（见 `meta` 属性）。
        self._meta_lock = threading.Lock()
        self.results = ResultStore(owner=self.engine)
        # 「父类名 -> 直接子类」映射的惰性缓存（见 _direct_children）。
        # 构建一次约 6 ms（IL2CPP 20,072 类型），元数据本身是只读的，
        # 所以不需要失效逻辑。
        self._child_map: dict[str, list[Any]] | None = None
        # 打开索引时顺带做的缓存清理结果（P3-2），由 `open()` 赋值。
        # 默认 None = 「没清理过」（例如直接 `Index(meta, path)` 建的索引）——
        # 不用空 dict 冒充「清理了但什么都没删」，两者含义不同。
        self.cache_pruned: dict[str, Any] | None = None

    @property
    def meta(self) -> Any:
        """元数据解析器，**按需解析**。

        缓存命中时 `open()` 不做任何解析就直接返回，所以 `_meta` 是 None；
        第一次访问本属性才真正载入解析器。这样「只想查索引」的调用
        （`search_classes` / `index_info` 等）**完全不必**把整份元数据
        读进内存。

        需要全量类型的路径（`_direct_children`）照旧通过本属性取，
        即「用到才付钱」，行为不变。

        **加锁**：工具现在会被 offload 到线程池执行（见 `server.py` 的
        `_offload_sync_tools`），并发访问时两个线程可能同时走到
        `if self._meta is None`，各自解析一遍 —— 480 ms 的解析做两遍是浪费，
        更糟的是 `_meta` 会被后写的覆盖、先解析那份泄漏。锁只保护
        「解析 + 赋值」这一小段，解析本身在锁内（本来也只能解析一次）。
        """
        if self._meta is None:
            with self._meta_lock:
                # 双重检查：等锁期间别的线程可能已经解析好了
                if self._meta is None:
                    if self._closed:
                        raise MetadataError(
                            "索引已关闭，元数据不再可用（不能再惰性解析）"
                        )
                    if self._meta_path is None:
                        raise MetadataError("索引未记录元数据路径，无法惰性解析")
                    self._meta = self._load_meta(self._meta_path, self.engine)
        return self._meta

    @property
    def conn(self) -> sqlite3.Connection:
        """当前线程可用的数据库连接。

        **为什么不是一条共享连接**（这是缺陷 #1 的配套修复）：
        本机 `sqlite3.threadsafety == 1`，实测同一条连接对象跨线程使用会直接抛

            ProgrammingError: SQLite objects created in a thread can only be
            used in that same thread.

        而修复 #1 把工具 offload 到线程池之后，同一个 `Index` 必然会被
        **不同线程**访问（线程池的 worker 是复用的，但绝不保证与创建者同一条）。
        所以这里按线程各持一条连接：创建者用主连接，其它线程第一次用时新建。

        连接是**只读**查询用的（建库只在 `_build()` 里、且在创建线程内完成），
        所以多开几条没有写冲突问题；每条都用 `check_same_thread=False` 只是为了
        让 `close()` 能在任意线程统一关掉它们。
        """
        if threading.get_ident() == self._owner_tid:
            return self._primary_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
            with self._conn_lock:
                self._conns.append(conn)
        return conn

    # ------------------------------------------------------ 生命周期

    @staticmethod
    def _resolve_engine(metadata_path: Path, engine: str = "") -> str:
        """定出引擎名，**不解析元数据**。

        显式传入的 engine 优先（不认识就报错，不静默回退）；
        否则只读文件头 8 字节嗅探。

        为什么单独抽出来：`open()` 需要引擎名来拼缓存文件路径，
        而这件事**只需要文件名**。以前它是先 `_load_meta` 全量解析、
        再 `adapt.engine_of` 反推引擎名 —— 为了一个字符串付了 480 ms。
        实测嗅探结果与 `adapt.engine_of` 在两个真实样本上完全一致。
        """
        want = (engine or "auto").strip().lower()
        if want in ("auto", "", "unknown"):
            return Index._sniff_engine(metadata_path)
        if want in ("il2cpp", "mono"):
            return want
        # 显式指定了不认识的引擎：直接报错，不要静默回退到某个解析器。
        # 静默回退正是「Agent 拿到貌似成功但内容错乱的结果」的根源。
        raise MetadataError(
            f"不支持的引擎类型：{engine!r}（可用：il2cpp / mono / auto）"
        )

    @staticmethod
    def _load_meta(metadata_path: Path, engine: str = "") -> Any:
        """按引擎载入元数据解析器。

        engine 为空或 auto 时**自动嗅探**：先看 magic（IL2CPP 的
        global-metadata.dat 以 0xFAB11BAF 开头），不是 IL2CPP 就试 Mono。
        显式传入 engine 可以跳过嗅探、避免误判。
        """
        want = Index._resolve_engine(metadata_path, engine)
        if want == "il2cpp":
            return Metadata(metadata_path)
        return MonoMetadata(metadata_path)

    @staticmethod
    def _sniff_engine(metadata_path: Path) -> str:
        """读文件头判断引擎。"""
        try:
            with open(metadata_path, "rb") as fh:
                head = fh.read(8)
        except OSError:
            return "il2cpp"
        # IL2CPP global-metadata.dat 的 magic 是 0xFAB11BAF（小端）
        if len(head) >= 4 and int.from_bytes(head[:4], "little") == METADATA_MAGIC:
            return "il2cpp"
        # .NET 程序集以 "MZ" 开头（PE 文件）
        if head[:2] == b"MZ":
            return "mono"
        # 兜底：按原行为当 IL2CPP 处理（会给出明确报错而非静默错数据）
        return "il2cpp"

    @classmethod
    def open(
        cls,
        metadata_path: str | Path,
        cache_dir: str | Path | None = None,
        force_rebuild: bool = False,
        engine: str = "",
    ) -> "Index":
        """打开（必要时构建）索引。

        缓存键 = 元数据文件 sha256，因此换游戏/换版本会自动重建，不会串数据。
        engine 为空时自动嗅探（IL2CPP / Mono）。

        **缓存命中时完全不解析元数据**：只用 8 字节嗅探定出引擎名来拼
        db_path，真正需要元数据时再由 `meta` 属性按需解析。
        以前这里是无条件 `_load_meta` 全量解析，命中也白付 480 ms（IL2CPP）。
        """
        metadata_path = Path(metadata_path)
        # 只定引擎名，不解析。（显式 engine 非法时这里就会报错，与旧行为一致）
        eng = cls._resolve_engine(metadata_path, engine)

        cache = Path(cache_dir) if cache_dir else default_cache_dir()
        cache.mkdir(parents=True, exist_ok=True)

        digest = _file_sha256(metadata_path)
        # 文件名带引擎前缀：同一个文件被两种解析器读出的索引不共用
        db_path = cache / f"{eng}-{digest[:16]}.sqlite"

        if db_path.is_file() and not force_rebuild and cls._is_valid(db_path):
            progress(f"复用索引缓存 {db_path.name}")
            idx = cls.__new__(cls)
            # 走与 __init__ 相同的字段初始化，杜绝「只在命中缓存时缺字段」
            # meta=None 是**刻意**的：命中缓存就不该解析，见 `meta` 属性。
            idx._attach(None, db_path,
                        metadata_path=metadata_path, engine=eng)
            idx.cache_hit = True          # ← 真的复用了缓存
            # 「命中」就是「用过」：不刷新 mtime，prune_cache 的 LRU 会把这个
            # 天天在用的缓存当成最久没用的那个。
            _touch(db_path)
            idx.cache_pruned = prune_cache(cache, protect=[db_path.name])
            return idx

        if db_path.is_file() and force_rebuild:
            db_path.unlink()

        # 只有确定要重建时才解析元数据。
        meta = cls._load_meta(metadata_path, engine)
        progress(f"构建索引 {db_path.name}（{adapt.engine_label(meta)}）...")
        idx = cls(meta, db_path)          # __init__ 里 cache_hit=False
        idx.cache_pruned = prune_cache(cache, protect=[db_path.name])
        return idx

    @staticmethod
    def _is_valid(db_path: Path) -> bool:
        """校验缓存文件完整（schema 版本匹配且表非空）。"""
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                if row is None or int(row[0]) != SCHEMA_VERSION:
                    return False
                n = conn.execute("SELECT COUNT(*) FROM types").fetchone()[0]
                return n > 0
            finally:
                conn.close()
        except sqlite3.Error:
            return False

    def close(self) -> None:
        """关闭**所有**连接与元数据。

        为什么是所有：`conn` 属性会为每条用过的线程各开一条连接
        （见那里的说明）。只关主连接的话，线程池 worker 上那些连接会一直
        占着 sqlite 文件 —— 在 Windows 上表现为「删不掉被占用的缓存文件」
        （`force_rebuild` 正需要先删掉它）。

        元数据可能是**从未解析**的（缓存命中且没用到）—— 这正是我们想要的：
        关闭时也不该为了「关掉它」而先花 480 ms 把它解析起来。
        """
        with self._conn_lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.close()
            except Exception as exc:      # noqa: BLE001 - 关不掉不该挡住其余清理
                warn(f"关闭索引连接失败：{exc}")
        self._closed = True
        meta, self._meta = self._meta, None
        if meta is not None:
            meta.close()

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<Index {self.db_path.name}>"

    # ------------------------------------------------------ 建表

    def _build(self) -> None:
        cur = self.conn.cursor()

        cur.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS types (
                idx            INTEGER PRIMARY KEY,
                name           TEXT NOT NULL,
                namespace      TEXT NOT NULL,
                full_name      TEXT NOT NULL,
                nested_name    TEXT NOT NULL,
                declaring_il2cpp INTEGER,
                parent_il2cpp  INTEGER,
                flags          INTEGER,
                token          INTEGER,
                method_count   INTEGER,
                field_count    INTEGER,
                is_enum        INTEGER,
                is_value_type  INTEGER,
                image          TEXT
            );

            CREATE TABLE IF NOT EXISTS fields (
                idx        INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                type_index INTEGER,
                owner      INTEGER
            );

            CREATE TABLE IF NOT EXISTS methods (
                idx             INTEGER PRIMARY KEY,
                name            TEXT NOT NULL,
                owner           INTEGER,
                return_type     INTEGER,
                parameter_start INTEGER,
                parameter_count INTEGER,
                flags           INTEGER,
                token           INTEGER
            );

            CREATE TABLE IF NOT EXISTS parameters (
                idx        INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                type_index INTEGER
            );

            CREATE TABLE IF NOT EXISTS images (
                idx        INTEGER PRIMARY KEY,
                name       TEXT,
                type_start INTEGER,
                type_count INTEGER
            );
            """
        )

        m = self.meta
        eng = adapt.engine_of(m)

        # ---- meta
        cur.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("metadata_file", str(m.path)),
                ("metadata_version", str(m.version)),
                ("engine", eng),
                ("engine_label", adapt.engine_label(m)),
                ("type_count", str(len(m.types))),
                ("method_count", str(len(m.methods))),
                ("field_count", str(len(m.fields))),
            ],
        )

        # ---- types
        # 注意 declaring/parent 两个引用列的语义随引擎不同（见 adapt 模块），
        # 它们只用于「有没有外层/有没有基类」的判断，不参与进一步索引。
        enum_flags = []
        for t in m.types:
            enum_flags.append(
                (
                    t.index, t.name, t.namespace, t.full_name,
                    getattr(t, "nested_name", "") or t.full_name,
                    adapt.declaring_ref(m, t) or 0,
                    adapt.parent_ref(m, t) or 0,
                    t.flags, t.token, t.method_count, t.field_count,
                    1 if m.is_enum(t) else 0,
                    1 if m.is_value_type(t) else 0,
                    adapt.image_of_type(m, t),
                )
            )
        cur.executemany(
            "INSERT INTO types VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", enum_flags
        )

        # ---- fields（附带 owner，省掉运行时的线性反查）
        field_rows = []
        for t in m.types:
            if t.field_count <= 0:
                continue
            for f in m.fields_of(t):
                field_rows.append(
                    (f.index, f.name, adapt.field_type_key(m, f), t.index)
                )
        cur.executemany("INSERT INTO fields VALUES (?,?,?,?)", field_rows)

        # ---- methods（owner 取 declaring_type 的静态归属）
        method_rows = []
        for t in m.types:
            if t.method_count <= 0:
                continue
            for mm in m.methods_of(t):
                method_rows.append(
                    (mm.index, mm.name, t.index, adapt.method_return_key(m, mm),
                     mm.parameter_start, mm.parameter_count, mm.flags, mm.token)
                )
        cur.executemany("INSERT INTO methods VALUES (?,?,?,?,?,?,?,?)", method_rows)

        # ---- parameters
        # Mono 的 Param 表不含类型（类型只在方法签名里），故 type_index 为 None
        param_rows = [
            (p.index, p.name, adapt.param_type_key(m, p)) for p in m.parameters
        ]
        cur.executemany("INSERT INTO parameters VALUES (?,?,?)", param_rows)

        # ---- images（Mono 的 ImageDef 没有 type_start/type_count，做兜底）
        image_rows = []
        for img in m.images:
            image_rows.append(
                (
                    img.index,
                    getattr(img, "name", "") or "",
                    getattr(img, "type_start", None),
                    getattr(img, "type_count", None),
                )
            )
        cur.executemany("INSERT INTO images VALUES (?,?,?,?)", image_rows)

        # ---- 索引
        cur.executescript(
            """
            CREATE INDEX IF NOT EXISTS ix_types_fullname ON types(full_name);
            CREATE INDEX IF NOT EXISTS ix_types_nested   ON types(nested_name);
            CREATE INDEX IF NOT EXISTS ix_types_name     ON types(name);
            CREATE INDEX IF NOT EXISTS ix_types_enum     ON types(is_enum);
            CREATE INDEX IF NOT EXISTS ix_types_image    ON types(image);
            CREATE INDEX IF NOT EXISTS ix_fields_name    ON fields(name);
            CREATE INDEX IF NOT EXISTS ix_fields_owner   ON fields(owner);
            CREATE INDEX IF NOT EXISTS ix_methods_name   ON methods(name);
            CREATE INDEX IF NOT EXISTS ix_methods_owner  ON methods(owner);
            """
        )

        self.conn.commit()
        progress(
            f"索引构建完成：类型 {len(enum_flags):,}、字段 {len(field_rows):,}、"
            f"方法 {len(method_rows):,}、参数 {len(param_rows):,}"
        )

    # ------------------------------------------------------ 查询：类型

    def index_info(self) -> dict[str, Any]:
        """索引概况。

        `cache_hit` 如实反映「这次是复用缓存还是真重建」：
        false 意味着刚刚花了时间做全量解析，是排查「为什么慢」的第一手线索。
        """
        cur = self.conn.cursor()
        info = dict(cur.execute("SELECT key, value FROM meta").fetchall())
        # meta 表里存的是文本，这里把计数字段转成 int，
        # 免得 Agent 拿到 "20072" 这种字符串还要自己转。
        for key in ("schema_version", "metadata_version",
                    "type_count", "method_count", "field_count"):
            if key in info:
                try:
                    info[key] = int(info[key])
                except (TypeError, ValueError):
                    pass
        info["database"] = str(self.db_path)
        info["database_size"] = self.db_path.stat().st_size if self.db_path.is_file() else 0
        info["enum_count"] = cur.execute(
            "SELECT COUNT(*) FROM types WHERE is_enum=1"
        ).fetchone()[0]
        info["image_count"] = cur.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        info["cache_hit"] = bool(getattr(self, "cache_hit", False))
        # 缓存占用（P3-2）：缓存目录会随「换游戏 / 游戏更新」一直长大，
        # 报出来才看得见。这是**真数**（现数现算），不是配置里的上限。
        info["cache"] = cache_stats(self.db_path.parent)
        # 只在**真的删了东西**时才有这个字段：没删就不给，
        # 免得客户端把「空列表」读成「缓存是干净的」这种恒定噪声。
        pruned = getattr(self, "cache_pruned", None) or {}
        if pruned.get("deleted"):
            limit = pruned.get("max_total_bytes")
            info["cache_evicted"] = {
                "files": list(pruned["deleted"]),
                "freed_bytes": pruned.get("freed_bytes", 0),
                # 上限取**这一次实际用的**那个值，不写模块默认常量：
                # 调用方可以传别的上限，写默认值就成了「报告说谎」。
                "limit_bytes": limit,
                "reason": f"缓存总大小超过 {limit} 字节上限，"
                          f"按最久未用优先清理（保留最近 {pruned.get('min_keep')} 个"
                          f"与 {int(pruned.get('min_age_seconds', 0)) // 86400} 天内用过的）",
            }
        # 把「这个引擎在元数据层面能提供什么」一并透给 Agent，
        # 免得它把「IL2CPP 本来就没有字段 flags」误判成工具出错。
        #
        # 用 capabilities_of(引擎名) 而不是 capabilities(self.meta)：
        # 这个问题的答案**只取决于引擎名**，用 self.meta 会把整份元数据
        # 解析起来（IL2CPP 约 480 ms）—— 「查个概况」不该付这个钱。
        info["capabilities"] = adapt.capabilities_of(self.engine)
        return info

    # ------------------------------------------------------ 跨引擎解析收口
    #
    # 下面三个方法把「同一个语义在不同引擎下的不同解析方式」收在一处。
    # 上层查询代码只调它们，不再直接碰 meta.resolve_il2cpp_type 之类的
    # 引擎专有接口。
    #
    # 背景：IL2CPP 的 returnType / paramTypeIndex / parentIndex 都是
    # Il2CppType 索引，用同一个 resolve 就行；Mono 的这三者分别是
    # 签名 blob、签名内顺序、TypeDefOrRef 编码值，需要分开处理。

    def _resolve_param(self, method_idx: int, param_row: sqlite3.Row
                       ) -> str | None:
        """解析参数类型。

        IL2CPP 可以逐参数用 typeIndex 解；
        Mono 的 Param 表没有类型，必须从**所属方法**的签名里按顺序取。

        注意 `m = self.meta` 这行**必须在 try 之外**：`self.meta` 是惰性属性，
        第一次访问会真的去载入/解析元数据，而那是可能失败的
        （文件被截断、版本不认识、内存不够）。失败必须**抛出去** ——
        `server.py` 每个工具都包了 `try/except → ok:false`，所以抛出去
        客户端会看到诚实的错误。若把它放进 try 里，元数据载入失败就会被
        降级成「这个参数没有类型」，整份结果照旧 `ok: true`。
        这正是本项目最忌讳的「假成功」，而且**实测发生过**：
        验证脚本里故意让 `_load_meta` 抛错，`search_classes` 曾经返回
        `ok: true` + 200 行 `parent: null`（见 `devtools/verify_lazy_meta.py` §5）。

        try 只负责「这一条参数解不出来」（下标越界等）这种**预期内**的降级，
        与 `adapt.py` 里「解析不出来就给 None、不猜」的政策一致。
        """
        m = self.meta
        if adapt.engine_of(m) == adapt.ENGINE_IL2CPP:
            return adapt.resolve_method_return(m, param_row["type_index"])
        # Mono：用方法签名里的参数类型列表按 sequence 取
        try:
            types = adapt.resolve_param_types(m, m.methods[method_idx])
            idx = param_row["sequence"] - 1   # sequence 是 1 基
            if 0 <= idx < len(types):
                return types[idx]
        except Exception:
            pass
        return None

    def _resolve_parent(self, type_idx: int) -> str | None:
        """解析类型的基类名。

        `m = self.meta` 同样必须在 try 之外（理由见 `_resolve_param`）：
        元数据载入失败**不许**被降级成「这个类没有基类」。
        """
        m = self.meta
        try:
            name = adapt.parent_name(m, m.types[type_idx])
        except Exception:
            return None
        return name or None

    def _row_to_type(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "index": row["idx"],
            "name": row["name"],
            "namespace": row["namespace"],
            "full_name": row["full_name"],
            # 嵌套感知全名（Outer.Inner）；嵌套类型用它与外部工具对齐
            "nested_name": row["nested_name"],
            "parent": self._resolve_parent(row["idx"]),
            "image": row["image"],
            "is_enum": bool(row["is_enum"]),
            "is_value_type": bool(row["is_value_type"]),
            "field_count": row["field_count"],
            "method_count": row["method_count"],
            "token": f"0x{row['token']:08X}",
        }

    def search_classes(
        self,
        pattern: str = "",
        offset: int = 0,
        limit: int = 50,
        subtype_of: str = "",
        assembly: str = "",
        only_enums: bool = False,
        exclude_compiler_generated: bool = True,
    ) -> dict[str, Any]:
        """搜索类/结构/枚举 —— Mod 开发的入口查询。

        pattern    子串（大小写不敏感），可空表示不过滤
        subtype_of 只保留继承自该类的类型（如 UnityEngine.MonoBehaviour）
        assembly   按 dll 名过滤（子串）
        only_enums 只要枚举（找游戏里的常量表特别有用）
        """
        where: list[str] = []
        args: list[Any] = []

        if pattern:
            # 同时匹配嵌套全名与原始全名，避免嵌套类型搜不到
            where.append("(nested_name LIKE ? OR full_name LIKE ?)")
            args.extend([f"%{pattern}%", f"%{pattern}%"])
        if assembly:
            where.append("image LIKE ?")
            args.append(f"%{assembly}%")
        if only_enums:
            where.append("is_enum = 1")
        if exclude_compiler_generated:
            where.append("name NOT LIKE '<%' AND name NOT LIKE '%<>%'")

        sql = "SELECT * FROM types"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY nested_name"

        rows = list(self.conn.execute(sql, args))

        # subtype_of 需要走继承链，无法纯 SQL 表达，故在 Python 侧过滤。
        # P1-4：**反过来查** —— 先在 meta 侧一次算出「后代下标集合」
        # （IL2CPP 用惰性邻接表 + BFS，Mono 沿用逐条判定），再与行取交集。
        # 原来对**每一行**调 is_subclass_of（每次都走一遍基类链）是
        # O(行数 × 链长)，IL2CPP 宽查询实测净增 +20~33 ms。
        if subtype_of:
            keep = self.meta.derived_set(subtype_of)
            rows = [r for r in rows if r["idx"] in keep]

        total = len(rows)
        limit = max(1, min(limit, 2000))
        offset = max(0, offset)

        # P1-3：句柄登记「原始行 + 解析函数」，不提前把 2 万行全解析成 dict。
        # 这里原来对全部 rows 调一次 _row_to_type（只为了塞进句柄），
        # 同时又对 page 调一次 —— 前者在宽查询里占 45%。
        source = _RowSource(rows, self._row_to_type)
        qr = self.results.put("classes", source)
        items = source.slice(offset, limit)

        return {
            "handle": qr.handle,
            "total": total,
            "offset": offset,
            "limit": limit,
            "returned": len(items),
            "has_more": offset + len(items) < total,
            "items": items,
        }

    def get_type(self, full_name: str) -> dict[str, Any] | None:
        """按完整名精确取类型（支持嵌套全名与原始全名）。

        与 `class_detail` 走同一套唯一性解析：拿不准就返回 `None`，
        **不**在多个同名类里随便挑一个（原来的 `LIMIT 1` 正是这么错的）。

        注：当前仓库内没有调用方（实际入口是 `class_detail`），
        保留是因为它是「按名取类型」的公开小工具，将来可能被复用；
        若确认长期无人使用，可连同这段注释一起删除。
        """
        ids, err = self._match_type_ids(full_name)
        if err or not ids:
            return None
        row = self.conn.execute(
            "SELECT * FROM types WHERE idx = ? LIMIT 1", (ids[0],)).fetchone()
        return self._row_to_type(row) if row else None

    # ------------------------------------------------------ 查询：成员
    #
    # 这里曾有一个 `_member_row(row, kind, type_name)`：把 sqlite 行组装成
    # 成员 dict。P3 复核时全仓库只有定义本身一处引用（真正在用的组装逻辑
    # 分别在 `_type_members` / `class_detail` 里就地完成），已删除。

    # 句柄里最多留多少条。超过这个数就不再往句柄里堆，但 `total` 仍然是**真实总数**
    # （见下面 _paged 的说明）——「句柄不全」必须如实标注，不能悄悄截断。
    _HANDLE_CAP = 20000

    def _framework_hint(self) -> str:
        """静态索引查不到框架类型时，把「为什么」直接告诉调用方。

        为什么需要：Mono 的静态索引默认只收游戏自己的程序集
        （Assembly-CSharp.dll），`UnityEngine.Component` 这类框架类型
        **必然**查不到 —— 实测 4 个 Mono 样本上
        `class_detail("UnityEngine.Component")` 全部返回「未找到类」。
        只说「用 search_classes 搜短名」会把人引向一条走不通的路
        （搜短名也搜不到，因为它压根没被索引），
        而正确答案（改用运行时探针）就在手边。
        这句话由 `adapt.py` 的能力位驱动，不在这里硬编码引擎名。
        """
        cap = adapt.capabilities_of(self.engine) if self.engine else {}
        if cap.get("static_index_covers_frameworks") is False:
            return ("；注意本引擎的静态索引**只覆盖游戏自己的程序集**，"
                    "UnityEngine.* 等框架类型不在其中 —— "
                    "这类类型请用 runtime_class_info 在运行时查（需先注入）")
        return ""

    def _match_type_ids(
        self, name: str, assembly: str = "", label: str = "owner",
        _probe: bool = True,
    ) -> tuple[list[int], dict[str, Any] | None]:
        """把类名解析成**唯一**一个类型，必要时按 assembly 消歧。

        返回 `(类型 idx 列表, 错误)`；成功时列表恰好一个元素。

        为什么必须唯一（历史教训）：早期实现是
        `SELECT idx FROM types WHERE nested_name=? OR full_name=? OR name=?`，
        然后把**所有**命中的类型当同一个类来处理：

            输入 "UserData" → 命中 2 个类型
                UserData                              [Unity.InputSystem.dll]
                TeamTapas.ProjectR.UserDatas.UserData [TeamTapas.ProjectR.dll]

        工具照样返回 `ok: true`，把 Unity 的字段和游戏自己的字段**混在一起**。
        Agent 以为拿到的是「UserData 这个类的字段」，据此算偏移必然错，
        而且错误要到运行时才暴露。所以命中多个时**拒绝并给出候选**，
        让调用方补 `assembly` 消歧，而不是替它猜。

        为什么必须把「全名」与「短名」两种匹配**合并**后再判歧义：
        没有命名空间的类型，其 `full_name` **就等于**短名
        （实测 `UserData` 的 `full_name` 就是 `"UserData"`）。
        若先单独试全名匹配，会「恰好命中一个」而看不出歧义，
        于是静默选中了 Unity 那个 —— 比原来的 bug 更隐蔽。
        合并后：全名匹配 {Unity} ∪ 短名匹配 {Unity, 团队那个} = 2 个 → 正确判为歧义。

        碰撞规模是实测过的（不是拍脑袋加限制）：
        IL2CPP 样本 20,072 个类型里，会被判为歧义的名字只有 585 个（3%）；
        Mono 样本 1,387 个类型里是 0 个。也就是说「拒绝」几乎不会误伤，
        而它拦掉的正是那种「看起来成功、偏移全错」的静默错误。
        """
        def _ids(where: str, *args: Any) -> list[int]:
            return [r["idx"] for r in self.conn.execute(
                f"SELECT DISTINCT idx FROM types WHERE {where}", args)]

        # 全名/嵌套全名匹配 + 短名匹配，取并集（保持稳定顺序）
        cand = _ids("nested_name = ? OR full_name = ?", name, name)
        for i in _ids("name = ?", name):
            if i not in cand:
                cand.append(i)

        # assembly 过滤必须在判歧义**之前**做：它是调用方给的消歧依据。
        # 注意不能因为 len==1 就跳过 —— 那样「指定了错误的 assembly」
        # 会静默返回那个不属于该程序集的类（实测：assembly="TeamTapas"
        # 却返回了 Unity 的 UserData）。
        if assembly and cand:
            ph = ",".join("?" * len(cand))
            keep = {r["idx"] for r in self.conn.execute(
                f"SELECT idx FROM types WHERE idx IN ({ph}) AND image LIKE ?",
                (*cand, f"%{assembly}%"))}
            cand = [i for i in cand if i in keep]

        if not cand:
            if assembly:
                return [], {"error": f"未找到类：{name}（在程序集 "
                                     f"{assembly!r} 里不存在，请检查拼写或去掉 assembly）",
                            "total": 0, "items": []}
            return [], {"error": f"未找到类：{name}"
                                 f"（用 search_classes 搜短名以确认完整类名）"
                                 f"{self._framework_hint()}",
                        "total": 0, "items": []}

        if len(cand) > 1:
            # _probe=False 用于「给自己找消歧建议」的内部验证调用，
            # 避免 A 歧义 → 探测 B → B 也歧义 → 再探测 … 的递归。
            return [], self._type_ambiguous(name, cand, assembly, label,
                                            verify=_probe)
        return cand, None

    def _resolve_owner(
        self, owner: str, assembly: str = ""
    ) -> tuple[list[int], dict[str, Any] | None]:
        """`search_fields` / `search_methods` 的 owner 解析（见 `_match_type_ids`）。"""
        return self._match_type_ids(owner, assembly, label="owner")

    def _resolve_type_row(
        self, name: str, assembly: str = ""
    ) -> tuple[sqlite3.Row | None, dict[str, Any] | None]:
        """按名字解析出**唯一**一行的 `types` 记录（供 class_detail / class_hierarchy 用）。

        与 `search_classes` 不同，这几个工具返回的是「这个类的全部内容」，
        拿错类不是少几条数据，而是整份字段/方法表都是别人的 ——
        所以这里同样必须拒绝歧义，而不是 `LIMIT 1` 随便选一个。

        历史教训：`class_detail`/`class_hierarchy` 曾用
        `... LIMIT 1` + 退化到 `name = ? LIMIT 1`，
        实测 `class_detail("UserData")` 静默返回了 Unity.InputSystem.dll
        那个 UserData（11 个字段），而调用方多半想要游戏自己的那个
        （23 个字段），且返回里没有任何歧义提示。这与
        `search_fields` 的 owner 合并是同一类缺陷，只是表现成「选错」而非「混合」。
        """
        ids, err = self._match_type_ids(name, assembly, label="full_name")
        if err:
            # 索引层统一用 found=False 表达「没查到」，server 的 _index_query
            # 会把它转成 ok=False 并把 candidates/hint 一并透出。
            out: dict[str, Any] = {"found": False,
                                   "reason": err.get("error", f"未找到类：{name}")}
            for k, v in err.items():
                if k not in ("error", "total", "items"):
                    out[k] = v
            return None, out
        row = self.conn.execute(
            "SELECT * FROM types WHERE idx = ? LIMIT 1", (ids[0],)).fetchone()
        if row is None:  # 理论上不可能：idx 刚从同一张表取出来
            return None, {"found": False, "reason": f"未找到类：{name}"}
        return row, None

    # 拒绝时最多列多少个候选。实测最坏情形（短名 "<>c"）有 456 个候选、
    # 序列化后 88 KB —— 直接把全部塞进错误返回会白白吃掉 Agent 的上下文，
    # 而它需要的只是「够看出该补什么限定」的前若干条。
    _CAND_CAP = 30

    def _type_ambiguous(
        self, name: str, ids: list[int], assembly: str, label: str = "owner",
        verify: bool = True,
    ) -> dict[str, Any]:
        """类名命中多个类型：拒绝，并把候选摆出来（参照 hook_method 的做法）。

        拒绝必须**可操作**，否则等于把难题丢回给调用方。这里踩过三个坑，
        都是实测出来的，不是推测：

        1) hint 曾建议 `full_name="State"` —— 而这正是调用方**刚刚传进来**
           的那个歧义串，照做只会再报一次同样的错。实测 131 个候选的
           `State` 全部回传 `full_name="State"` 仍然歧义。
           真正能唯一定位的是 `nested_name`（限定名，如
           `TeamTapas.ProjectR.UI2.RUI_FloatingUI.State`），实测 131/131、
           54/54、31/31 都唯一，回传即可命中。
        2) hint 曾无条件建议 `assembly=` —— 但当所有候选都在**同一个映像**里时
           这毫无用处（实测 `UxmlTraits` 的 54 个候选全在
           UnityEngine.UIElementsModule.dll）。那种情况下必须说清
           「assembly 帮不上忙」，否则调用方会反复试。
        3) 候选列表曾无上限（456 个 / 88 KB）。

        为了不给出「看起来像建议、实际没用」的 hint，这里**实际验证**过：
        按顺序拿候选的 `nested_name` 回灌解析器，取第一个真能唯一定位的
        作为建议值。宁可多花一两次索引查询，也不给空头支票。
        """
        cands = []
        for r in self.conn.execute(
            f"SELECT idx, nested_name, full_name, name, image, field_count, "
            f"method_count FROM types WHERE idx IN ({','.join('?' * len(ids))})",
            tuple(ids),
        ):
            cands.append({
                "index": r["idx"],
                "full_name": r["full_name"],
                "nested_name": r["nested_name"],
                "name": r["name"],
                "image": r["image"],
                "field_count": r["field_count"],
                "method_count": r["method_count"],
            })
        cands.sort(key=lambda c: (c["image"] or "", c["nested_name"] or ""))
        total = len(cands)
        shown = cands[: self._CAND_CAP]

        # assembly 是否真能消歧：只有当候选分散在**多个**映像里才有用
        images = {c["image"] for c in cands}
        asm_useful = len(images) > 1

        # 找一个**验证过**能唯一定位的 nested_name 当建议（通常第 1 个就成）。
        # nested_name 实测在绝大多数情况下唯一（IL2CPP 20,072 类型里只有 89 个
        # 重复），但既然它**可能**不唯一，就不能想当然 —— 这里真的回灌解析器
        # 验一遍，取第一个真能唯一定位的。verify=False 时跳过（防递归）。
        usable = ""
        if verify:
            for c in cands[:5]:
                nn = c.get("nested_name") or ""
                if not nn:
                    continue
                resolved, err2 = self._match_type_ids(nn, _probe=False)
                if not err2 and len(resolved) == 1:
                    usable = nn
                    break

        # 成因描述必须与事实一致：曾无条件写「多半是不同程序集里有同名类」，
        # 但实测 `UxmlTraits` 的 54 个候选全在同一个映像里（都是
        # UnityEngine.UIElements 的嵌套类），那句话是错的 —— 而错误的成因
        # 会把调用方引向错误的排查方向（去试 assembly=，越试越迷惑）。
        if len(images) > 1:
            why = (f"（分散在 {len(images)} 个程序集里）" if not assembly
                   else "(已按 assembly 过滤，但仍不唯一)")
        else:
            why = (f"（都在同一个程序集 {sorted(images)[0]!r} 里，"
                   f"是嵌套/同名类型）")
        if usable:
            # nested_name 能唯一定位，那就直接给出可照抄的调用
            how = f'{label}="{usable}" 可直接定位到其中一个。'
            if not asm_useful:
                # 顺手掐掉一个可预见的弯路：调用方看到「同名类」很容易先试
                # assembly=，但这里候选全在同一映像，试了只会再失败一次。
                how += (f'（这些候选都在同一个程序集 '
                        f'{sorted(images)[0]!r} 里，assembly 参数帮不上忙）')
        elif asm_useful:
            how = (f'候选分散在 {len(images)} 个映像里，'
                   f'可用 {label}="<完整类名>" 或 {label}="{name}", '
                   f'assembly="<下列某一程序集>" 消歧。')
        else:
            # 同映像内同名：assembly 完全帮不上忙，必须说明，否则调用方白试
            how = (f'注意：这些候选都在同一个程序集 {sorted(images)[0]!r} 里，'
                   f'assembly 参数**帮不上忙**，请改用上面候选的 nested_name。')
        out: dict[str, Any] = {
            "error": f"{label}={name!r} 命中 {total} 个类{why}，"
                     f"无法确定你要哪一个。{how}",
            "total": 0,
            "items": [],
            "candidates": shown,
            "candidate_count": total,
            "assembly_helps": asm_useful,
        }
        if total > len(shown):
            out["candidates_truncated"] = True
            out["candidates_note"] = (
                f"候选共 {total} 个，此处只列前 {len(shown)} 个"
                f"（避免错误返回体积过大）；用 search_classes(pattern=..., "
                f"subtype_of=...) 可自行缩小范围。"
            )
        if usable:
            out["hint"] = f'{label}="{usable}"'
        return out

    def _paged(
        self, kind: str, total: int, source: _RowSource,
        offset: int, limit: int, handle_truncated: bool, **extra: Any
    ) -> dict[str, Any]:
        """组装统一的分页返回（`total` 是真值，`has_more` 如实标注）。

        关键：`total` 由调用方用 `COUNT(*)` 算出来，**不是** `len(items)`。
        早期实现在 SQL 里写死 `LIMIT 20000` 再令 `total = len(items)`，
        于是「'a' 命中 49286 个字段」被报成 20000（实测），
        而且是**静默**的：Agent 会以为全局只有 2 万个匹配，据此判断覆盖率。

        P1-3：入参从 `items` 改为 `source` —— 只解析这一页，不再把整个
        结果集提前物化（见 `_RowSource`）。
        """
        limit = max(1, min(limit, 2000))
        offset = max(0, offset)
        page = source.slice(offset, limit)
        out = {
            "handle": self.results.put(kind, source).handle,
            "total": total,
            "offset": offset,
            "limit": limit,
            "returned": len(page),
            "has_more": offset + len(page) < total,
            "items": page,
        }
        if handle_truncated:
            out["handle_truncated"] = True
            out["handle_note"] = (
                f"匹配数 {total} 超过句柄上限 {self._HANDLE_CAP}，"
                f"句柄只保留了前 {len(source)} 条；翻页请用本工具的 offset/limit，"
                f"不要依赖 read_query_result 拿到全集。"
            )
        out.update(extra)
        return out

    def search_fields(
        self, pattern: str, offset: int = 0, limit: int = 50, owner: str = "",
        assembly: str = "",
    ) -> dict[str, Any]:
        """按字段名搜索（大小写不敏感）。

        owner 非空时只保留属于**某一个**类的字段，该类由 owner 唯一确定：
        先按完整名/嵌套全名匹配，再退到短名；命中多个则拒绝并给候选
        （可用 assembly 消歧）。很多字段名（如 value、data、count）在几万个
        类里重复，不给 owner 会淹没在噪声里。
        """
        where = "name LIKE ?"
        params: list[Any] = [f"%{pattern}%"]
        if owner:
            owners, bad = self._resolve_owner(owner, assembly)
            if bad:
                return bad
            where += f" AND owner IN ({','.join('?' * len(owners))})"
            params.extend(owners)

        # total 走 COUNT(*) 拿真值；items 才受句柄上限约束
        total = self.conn.execute(
            f"SELECT COUNT(*) FROM fields WHERE {where}", params).fetchone()[0]
        rows = list(self.conn.execute(
            f"SELECT * FROM fields WHERE {where} ORDER BY name LIMIT ?",
            (*params, self._HANDLE_CAP)))

        # P1-3：只登记来源，不在这里把最多 2 万条全解析成 dict。
        def _convert_field(r: sqlite3.Row) -> dict[str, Any]:
            # 字段的类型引用与其它成员不是同一套索引，必须走字段专用解析
            dt = adapt.resolve_field(self.meta, r["type_index"])
            return {
                "index": r["idx"],
                "name": r["name"],
                "owner": (
                    self.meta.types[r["owner"]].nested_name
                    if r["owner"] is not None
                    else None
                ),
                "field_type": dt.get("name"),
                "type_confidence": dt.get("confidence"),
                "type_index": r["type_index"],
            }

        return self._paged("fields", total, _RowSource(rows, _convert_field),
                           offset, limit, total > len(rows))

    def search_methods(
        self, pattern: str, offset: int = 0, limit: int = 50, owner: str = "",
        assembly: str = "",
    ) -> dict[str, Any]:
        """按方法名搜索（大小写不敏感）。

        owner 非空时只保留属于**某一个**类的方法，语义与 search_fields
        完全一致（含「命中多个就拒绝」）。如搜 Update 时限定 Player。
        """
        where = "name LIKE ?"
        params: list[Any] = [f"%{pattern}%"]
        if owner:
            owners, bad = self._resolve_owner(owner, assembly)
            if bad:
                return bad
            where += f" AND owner IN ({','.join('?' * len(owners))})"
            params.extend(owners)

        total = self.conn.execute(
            f"SELECT COUNT(*) FROM methods WHERE {where}", params).fetchone()[0]
        rows = list(self.conn.execute(
            f"SELECT * FROM methods WHERE {where} ORDER BY name LIMIT ?",
            (*params, self._HANDLE_CAP)))

        def _convert_method(r: sqlite3.Row) -> dict[str, Any]:
            return {
                "index": r["idx"],
                "name": r["name"],
                "owner": self.meta.types[r["owner"]].full_name if r["owner"] is not None else None,
                "return_type": adapt.resolve_method_return(self.meta,
                                                           r["return_type"]),
                "parameter_count": r["parameter_count"],
                "flags": r["flags"],
                "token": f"0x{r['token']:08X}",
            }

        # P1-3：同上，句柄惰性化（方法返回值的解析比类型行更贵）
        return self._paged("methods", total, _RowSource(rows, _convert_method),
                           offset, limit, total > len(rows))

    def class_detail(self, full_name: str, include_inherited: bool = False,
                     assembly: str = "") -> dict[str, Any]:
        """取一个类的完整信息：字段（含静态判定）、方法、基类链、接口、子类数。

        这是「搞清楚一个类长什么样」的主力工具，也是后续 Hook 的输入。
        `assembly` 用于同名类消歧（见 `_match_type_ids`）。
        """
        # 必须唯一解析：返回的是「这个类的全部内容」，选错类等于整份字段表
        # 都是别人的，且返回里看不出异常（实测 UserData 会静默落到 Unity 那个）。
        row, err = self._resolve_type_row(full_name, assembly)
        if err:
            return err
        assert row is not None

        t = self.meta.types[row["idx"]]
        m = self.meta

        fields = [
            {
                "index": f.index,
                "name": f.name,
                "type_index": adapt.field_type_key(m, f),
                # 字段走专用解析规则；type_confidence 区分「直接命中」与
                # 「派生槽位（数组/泛型实例，只给到元素类型名）」
                "type": dt.get("name"),
                "type_confidence": dt.get("confidence"),
                # delta 只有 IL2CPP 有（索引轨道偏移量）；Mono 是精确签名，无此概念
                "type_delta": dt.get("delta"),
                "declared_by": t.nested_name,
            }
            for f in m.fields_of(t)
            for dt in (adapt.resolve_field(m, adapt.field_type_key(m, f)),)
        ]

        methods = [
            {
                "index": x.index,
                "name": x.name,
                "token": x.runtime_token,
                "return_type": adapt.resolve_method_return(
                    m, adapt.method_return_key(m, x)),
                "parameter_count": x.parameter_count,
                "parameters": [
                    {"name": p.name, "type": pt}
                    for p, pt in zip(
                        m.parameters_of(x),
                        adapt.resolve_param_types(m, x) or
                        [None] * len(m.parameters_of(x)),
                    )
                ],
                "is_static": x.is_static,
                "is_virtual": x.is_virtual,
                "is_constructor": x.is_constructor,
                "flags": f"0x{x.flags:04X}",
            }
            for x in m.methods_of(t)
        ]

        chain = [c.nested_name for c in m.base_chain(t)]

        if include_inherited:
            for anc in m.base_chain(t):
                for f in m.fields_of(anc):
                    dt = adapt.resolve_field(m, adapt.field_type_key(m, f))
                    fields.append(
                        {
                            "index": f.index,
                            "name": f.name,
                            "type_index": adapt.field_type_key(m, f),
                            "type": dt.get("name"),
                            "type_confidence": dt.get("confidence"),
                            "declared_by": anc.nested_name,
                        }
                    )
                for x in m.methods_of(anc):
                    methods.append(
                        {
                            "index": x.index,
                            "name": x.name,
                            "token": x.runtime_token,
                            "return_type": adapt.resolve_method_return(
                                m, adapt.method_return_key(m, x)),
                            # 继承来的方法同样要给参数表：class_detail 的
                            # method_name 过滤（原 get_method_source 的职责）
                            # 会同时命中自身与继承的方法，字段形状必须一致，
                            # 否则 Agent 按 parameter_count 选重载时会拿到 None。
                            "parameter_count": x.parameter_count,
                            "parameters": [
                                {"name": p.name, "type": pt}
                                for p, pt in zip(
                                    m.parameters_of(x),
                                    adapt.resolve_param_types(m, x) or
                                    [None] * len(m.parameters_of(x)),
                                )
                            ],
                            "flags": f"0x{x.flags:04X}",
                            "is_static": x.is_static,
                            "is_virtual": x.is_virtual,
                            "is_constructor": x.is_constructor,
                            "declared_by": anc.nested_name,
                        }
                    )

        # 全量取回，这样 derived_count 才是真值（不是被 200 截断后的数）。
        # 曾经写死 limit=200，于是 UnityEngine.MonoBehaviour 报 derived_count=200，
        # 实际有 1548 个 —— 与 find_derived_classes 的 total 是同一类谎报。
        derived = m.find_derived(t.full_name, limit=1_000_000)
        derived_cap = 50

        return {
            "found": True,
            "index": t.index,
            "full_name": t.full_name,
            "nested_name": t.nested_name,
            "namespace": t.namespace,
            "name": t.name,
            "image": m.image_of_type(t),
            "token": f"0x{t.token:08X}",
            "flags": f"0x{t.flags:08X}",
            "visibility": t.visibility,
            "is_enum": m.is_enum(t),
            "is_value_type": m.is_value_type(t),
            "is_interface": t.is_interface,
            "is_abstract": t.is_abstract,
            "is_sealed": t.is_sealed,
            "parent": self._resolve_parent(t.index),
            "base_chain": chain,
            "field_count": len(fields),
            "method_count": len(methods),
            "fields": fields,
            "methods": methods,
            "derived_count": len(derived),
            "derived": [d.nested_name for d in derived[:derived_cap]],
            # 如实标注列表被截断，避免 Agent 把 50 条当成全部
            "derived_returned": min(len(derived), derived_cap),
            "derived_has_more": len(derived) > derived_cap,
            "include_inherited": include_inherited,
        }

    # ------------------------------------------------------ 查询：层级 / 依赖

    # 每层最多展开多少个直接子类；超出的部分如实计入 *_has_more。
    _CHILD_CAP = 500

    def _direct_children(self) -> dict[str, list[Any]]:
        """父类名 -> **直接**子类列表（惰性构建一次，之后复用）。

        为什么需要它：`meta.find_derived()` 是**传递式**查询（返回全部后代，
        含间接）。`class_hierarchy` 早期直接拿它当「直接子类」用，于是
        每个中间层的 `depth` 与 `parent` 都是编造的 —— 实测
        `class_hierarchy("UnityEngine.MonoBehaviour", direction="down", depth=1)`：
        真值直接子类 428 个，工具却报了 500 个 `depth=1`，其中 **319 个不是
        直接子类**（`parent` 被写成 MonoBehaviour），同时 **247 个真实直接
        子类根本没出现**。

        这比「少给几条」严重得多：Agent 用 down 的核心目的就是逐层摸清继承树，
        错误的 `parent` 会让它把不存在的父子关系写进 Hook 方案。

        键用 `adapt.parent_name` 的返回值（与 `TypeDef.nested_name` 同形），
        这样跨引擎都是一套逻辑 —— 引擎差异只出现在 `adapt` 里（见 AGENTS.md §3）。
        构建成本实测：IL2CPP 20,072 类型 6 ms，Mono 1,387 类型 1 ms，
        所以惰性构建一次即可，不做增量失效。
        """
        if self._child_map is not None:
            return self._child_map
        kids: dict[str, list[Any]] = {}
        for t in self.meta.types:
            pn = adapt.parent_name(self.meta, t)
            if pn:
                kids.setdefault(pn, []).append(t)
        self._child_map = kids
        return kids

    def class_hierarchy(self, full_name: str, direction: str = "both", depth: int = 6,
                       assembly: str = "") -> dict[str, Any]:
        """类的继承层级树。

        direction: up（基类）/ down（子类）/ both

        `down` 是**逐层**的：每个条目给的是它所属层的 `depth`，`parent` 是它
        在**上一层**的真实父类。层与层之间靠「直接子类」推进，不走传递式查询。
        """
        m = self.meta
        # 与 class_detail 同一套解析：继承树挂到别的同名类上同样是无提示的错误。
        _row, err = self._resolve_type_row(full_name, assembly)
        if err:
            return err
        t = m.types[_row["idx"]]

        result: dict[str, Any] = {"found": True, "root": t.nested_name}

        if direction in ("up", "both"):
            result["up"] = [c.nested_name for c in m.base_chain(t, limit=depth)]

        if direction in ("down", "both"):
            kids = self._direct_children()
            tree: list[dict[str, Any]] = []
            frontier = [(t, 0)]
            seen = {t.index}
            truncated_levels: set[int] = set()
            while frontier:
                cur, lvl = frontier.pop(0)
                if lvl >= depth:
                    continue
                children = kids.get(cur.nested_name, [])
                # 超出上限时如实标注，而不是让 Agent 以为这层就这么多
                if len(children) > self._CHILD_CAP:
                    truncated_levels.add(lvl + 1)
                for d in children[: self._CHILD_CAP]:
                    if d.index in seen:
                        continue
                    seen.add(d.index)
                    tree.append({"name": d.nested_name, "depth": lvl + 1,
                                 "parent": cur.nested_name})
                    frontier.append((d, lvl + 1))
            result["down"] = tree
            result["down_count"] = len(tree)
            result["depth"] = depth
            # 是否有任何一层被 _CHILD_CAP 截断 —— 没有这个字段，Agent 无法
            # 察觉「500 条」其实是上限而不是全部（实测 500 正是这么来的）。
            # 注意：depth 是一开始就指定的参数，按它收尾不算「截断」；
            # 这里只如实反映 _CHILD_CAP 造成的丢失。
            result["down_has_more"] = bool(truncated_levels)
            result["down_is_direct_only"] = True
            if truncated_levels:
                result["down_truncated_levels"] = sorted(truncated_levels)
                result["note"] = (
                    f"第 {sorted(truncated_levels)} 层的子类数超过上限 "
                    f"{self._CHILD_CAP}，已截断；用 find_derived_classes 拿全量。"
                )

        return result

    def find_derived_classes(self, base_full_name: str, limit: int = 200,
                             assembly: str = "") -> dict[str, Any]:
        """列出所有继承自某类的类型（找 MonoBehaviour 脚本的必备工具）。

        判定用**传递式**继承（含间接继承），因此
        `find_derived_classes("UnityEngine.MonoBehaviour")` 能一次拿到
        所有脚本组件 —— 这正是它取代旧 `find_monobehaviours` 的原因：
        那个工具只是本方法加了一个「只看游戏程序集」的粗糙过滤器，
        而 `assembly` 参数能把这件事做得更明确、更可控。

        Args:
            base_full_name: 基类完整名。
            limit:          最多返回多少条。
            assembly:       只看该程序集（子串匹配，如 "Assembly-CSharp"）。
                            留空则不限。找游戏自己的 MonoBehaviour 脚本时
                            传 "Assembly-CSharp" 比按前缀猜引擎程序集更可靠。

        注意：基类**不必**出现在被索引的程序集里。Mono 游戏只索引
        Assembly-CSharp.dll，而 `UnityEngine.MonoBehaviour` 定义在
        UnityEngine.CoreModule.dll —— 这时 `type_by_name` 找不到基类，
        但 `find_derived` 依然能正确工作（它扫的是派生类的基类引用）。
        所以这里**不能**因为找不到基类就报错。

        返回的 `total` **必须**是过滤后的**真实总数**，不是被 limit 截断后的条数：
        实测 Archon Soul 的 MonoBehaviour 派生类有 644 个，若按截断数返回
        `total: 200`，Agent 会以为「这个游戏只有 200 个脚本」而漏掉三分之二。
        所以这里全量算出结果集，再用 `has_more` 如实标注是否被截断。
        全量扫描很便宜（实测 IL2CPP 20,072 类型 17 ms / Mono 1,387 类型 2 ms），
        不值得为此牺牲正确性。
        """
        m = self.meta
        base = m.type_by_name(base_full_name)

        # 全量取回，再按 assembly 过滤 —— 这样 total 才是真值。
        # 之前先用 limit*20 截断再过滤，导致 total 既不是真值、也不稳定。
        derived = m.find_derived(base_full_name, limit=1_000_000)

        filtered = []
        for d in derived:
            img = m.image_of_type(d)
            if assembly and assembly.lower() not in img.lower():
                continue
            filtered.append({
                "index": d.index,
                "full_name": d.nested_name,
                "image": img,
                "field_count": d.field_count,
                "method_count": d.method_count,
            })

        total = len(filtered)
        items = filtered[:limit]
        truncated = total > limit

        if base is None and total == 0:
            # 既找不到基类、也没有任何派生类 —— 这才是真的查不到
            return {"found": False, "query": base_full_name,
                    "total": 0, "returned": 0, "items": []}

        out: dict[str, Any] = {
            "found": True,
            "base": base.nested_name if base is not None else base_full_name,
            "total": total,
            "returned": len(items),
            "has_more": truncated,
            "items": items,
        }
        if truncated:
            out["note"] = (
                f"匹配 {total} 个，本次只返回前 {len(items)} 个（limit={limit}）。"
                f"调大 limit，或加 assembly= 缩小范围。"
            )
        if assembly:
            out["assembly_filter"] = assembly
        if base is None:
            # 如实说明：基类不在当前索引范围内，但派生类是通过基类名字符串
            # 比对出来的，结果依然有效。
            # 注意：不能直接赋值覆盖 —— 可能上面已有「被截断」的提示，
            # 两条信息都要保留（否则截断警告会被悄悄吞掉）。
            extra = (
                f"基类 {base_full_name} 不在当前索引的程序集内（例如它属于 "
                f"UnityEngine/System 等框架程序集），派生类按基类名比对得出。"
            )
            out["note"] = f"{out['note']} {extra}" if out.get("note") else extra
        return out

    def analyze_dependency(self, full_name: str,
                           assembly: str = "") -> dict[str, Any]:
        """静态依赖概览：这个类引用了哪些类型、被谁引用。

        注意：**这是基于元数据签名的近似分析**，不是精确的调用图。
        IL2CPP 不保留托管 IL，精确调用图需要反汇编原生代码。
        本工具给出的是「字段/方法签名里出现了哪些类型」，用于快速摸清耦合关系。
        """
        m = self.meta
        # 同 class_detail：依赖清单必须属于**请求的那个类**，不能选错同名类。
        _row, err = self._resolve_type_row(full_name, assembly)
        if err:
            return err
        t = m.types[_row["idx"]]

        referenced: dict[str, int] = {}

        def bump(nm: str | None) -> None:
            if nm and nm != t.nested_name:
                referenced[nm] = referenced.get(nm, 0) + 1

        for f in m.fields_of(t):
            # 字段走专用解析规则
            bump(adapt.resolve_field(m, adapt.field_type_key(m, f)).get("name"))
        for x in m.methods_of(t):
            bump(adapt.resolve_method_return(m, adapt.method_return_key(m, x)))
            for pt in adapt.resolve_param_types(m, x):
                bump(pt)
        bump(adapt.parent_name(m, t))

        ordered = sorted(
            ({"type": k, "count": v} for k, v in referenced.items()),
            key=lambda x: -x["count"],
        )
        _REF_CAP = 100
        return {
            "found": True,
            "class": t.nested_name,
            "parent": self._resolve_parent(t.index),
            "referenced_types": ordered[:_REF_CAP],
            "referenced_count": len(ordered),
            # 与 class_detail 的 derived_has_more 保持一致：截断了就如实说，
            # 否则 Agent 会把「前 100 个」当成全部引用。实测本样本最大引用数
            # 只有 75，触不到上限 —— 但样本触不到不等于逻辑可以省。
            "referenced_returned": min(len(ordered), _REF_CAP),
            "referenced_has_more": len(ordered) > _REF_CAP,
            "note": "基于字段/方法签名的近似依赖，非精确调用图",
        }

    # ------------------------------------------------------ 结果句柄

    def read_query_result(self, handle: str, offset: int = 0, limit: int = 50,
                          pattern: str = "") -> dict[str, Any]:
        """翻页读取之前某次查询的完整结果；给了 pattern 就在结果内部再过滤。

        这一个方法取代了原先的 read_query_result + search_query_result 两个工具
        —— 契约第 3 条：`search_query_result` 就是 `read_query_result(pattern="")`
        的特例，分开留两个只会让 Agent 猜该用哪个。
        """
        qr = self.results.get(handle)
        if qr is None:
            return {
                "found": False,
                "handle": handle,
                "available_handles": self.results.handles(),
            }
        out = qr.page(offset, limit, pattern=pattern)
        out["found"] = True
        return out
