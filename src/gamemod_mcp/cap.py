"""响应体积上限（P2-4）：任何一次工具返回都不许把 Agent 的上下文炸掉。

## 为什么需要「统一上限」，而不是每个工具各自的 limit

契约第 5 条要求「不返回超大结果」，此前它靠**每个工具各自的** `limit` /
`has_more` 自觉遵守。实测（`devtools/verify_response_size.py`）表明这条防线
不成立 —— `limit` 的上限是 2000，Agent 完全可以合法地要 2000 条：

    search_classes(pattern="", limit=2000)      856 KB
    search_fields('a', limit=2000)              528 KB
    search_methods('a', limit=2000)             505 KB
    find_derived_classes(MonoBehaviour, 2000)   311 KB

而这些响应里 **92.8%~93.9% 的字节集中在同一个顶层列表**上（`items` / `down` /
`methods`）。所以这里把「不许超大」从**自觉**变成**机制**。

## 做法

超过 `CAP_BYTES` 时，把最大的那个顶层列表截到刚好放得下，完整列表登记成句柄，
响应里如实写明「只回了前 N 条、剩下的用哪个句柄怎么取」。

三个刻意的取舍：

1. **只截顶层列表，不做递归截断。** 实测超限字节全在顶层的那一个列表上；
   递归截断虽然更"彻底"，但会让「到底哪里被截了」变得不明显。
2. **已经有 `handle` 就不新造句柄。** `search_*` 的返回里本来就带一个指向
   **完整**结果集的句柄（`_paged` 给的），再给一个只会让 Agent 犹豫用哪个。
   此时续读的 `offset` 要**接着原来的 offset** 算，不能从 0 算（见 `_resume_at`）。
3. **测量宁可偏大，不许偏小。** 见 `measure()`：偏大只是少回几条，
   偏小就是真的超限了。
"""

from __future__ import annotations

import json
from typing import Any

#: 单次响应的线上文本上限。选 256 KB 的依据：实测最大 856 KB，
#: 取约 1/3 既能把最坏情况压下来，又不会让「要 200 条」这种正常查询被截。
#: 截断不丢数据 —— 完整列表照旧进句柄，用 `read_query_result` 取。
CAP_BYTES = 256 * 1024

#: 给「截断之后才加的那些字段」留的余量（`capped` / `cap_bytes` / `note`）。
#: 为什么必须留：`_fit` 量的试探载荷里**还没有**这几个字段，而 `note` 是中文
#: 说明（按 `ensure_ascii=True` 算，一个汉字 6 字节）。实测不留余量时，
#: `search_classes('')` 截完是 262,304 B —— **超了 160 B**。
#: 2 KB 远超任何一条说明的长度，代价只是少回几条。
RESERVE_BYTES = 2048


def measure(payload: Any) -> int:
    """估算 `payload` 在 MCP 线上文本里的字节数（**保守偏高**）。

    为什么不用 FastMCP 真正用的 `pydantic_core.to_json`：这里只要一个可靠的
    **上界**，用标准库就够，也不必依赖 `mcp` 的内部实现。
    `ensure_ascii=True` 让非 ASCII 走 `\\uXXXX`（比原样 UTF-8 更长），
    于是估出来只会偏大：偏大只是偶尔少回几条，偏小才是真的超限。

    偏大多少、以及线上真实值，由 `devtools/verify_response_size.py` 用
    「同一个 payload 分别过 `measure` 与真实 `FastMCP.call_tool`」实测钉住。
    """
    return len(
        json.dumps(payload, ensure_ascii=True, default=str,
                   indent=2, separators=(",", ": ")).encode("utf-8")
    )


def _resume_at(payload: dict, key: str, keep: int, reused: bool) -> int:
    """续读时该从第几条开始。

    **复用已有句柄时不能从 0 算**：`_paged` 的 `items` 是
    `offset .. offset+limit` 这一段，截断后要继续取的是
    `offset + keep` 之后的，而不是 `keep` 之后的。
    新登记的句柄装的就是这个列表本身，所以从 `keep` 算。
    """
    if not reused:
        return keep
    base = payload.get("offset")
    return (base if isinstance(base, int) else 0) + keep


def _fit(payload: dict, key: str, items: list, cur: int) -> int:
    """求出「最多保留几条」才能放进上限。

    平均条宽必须按**在上下文里的**宽度算。曾经拿「单独序列化这个列表」当分母，
    于是把「列表在 dict 里比独立序列化多缩进一层」的字节算进了"其它部分" ——
    实测 `search_classes('')` 因此少回约 20% 的条数（514 而不是 630）。
    改成「把列表清空再量一次」：这次测量很便宜（列表已空），而且恰好等于
    「除这个列表之外的全部字节」。

    起始值只是估算，最后仍用**真实测量**收敛（最多 6 轮）——
    末条特别宽时估算会偏大，不许只凭估算下结论。
    """
    total = len(items)
    if total <= 0:
        return 0
    budget = CAP_BYTES - RESERVE_BYTES
    base = dict(payload)
    base[key] = []
    other = measure(base)
    avg = (cur - other) / total
    if avg <= 0:
        return total
    keep = int(max(0, budget - other) / avg)
    keep = max(1, min(total, keep))
    for _ in range(6):
        probe = dict(payload)
        probe[key] = items[:keep]
        got = measure(probe)
        if got <= budget:
            return keep
        keep = max(1, int(keep * budget / got * 0.97))
    return keep


def _register(out: dict, key: str, items: list, store: Any) -> tuple[str | None, bool]:
    """给被截断的完整列表找一个句柄，返回 (句柄, 是否复用了已有句柄)。"""
    if key == "items" and out.get("handle"):
        return str(out["handle"]), True
    if store is None:
        return None, False
    # 延迟导入：cap 是协议层的小工具，不该在导入期就把索引层拉起来
    from .engine.index import list_source

    handle = store.put(key, list_source(items)).handle
    out["handle"] = handle
    return handle, False


def enforce(payload: Any, *, tool: str, store: Any = None) -> Any:
    """把超过 `CAP_BYTES` 的响应截断成「前 N 条 + 句柄 + 说明」。

    规则：**任何**返回都不许超过上限（成功与失败一视同仁），
    所以这里不按 `ok` 分流。没超限时原样返回（连字典都不复制）。
    """
    if not isinstance(payload, dict) or not payload:
        return payload
    if measure(payload) <= CAP_BYTES:
        return payload

    candidates = []
    for k, v in payload.items():
        if isinstance(v, list) and v:
            candidates.append((measure(v), k))
    candidates.sort(reverse=True)

    out = dict(payload)
    notes: list[str] = []
    capped = False
    for _, key in candidates:
        cur = measure(out)
        if cur <= CAP_BYTES:
            break
        items = out[key]
        keep = _fit(out, key, items, cur)
        if keep >= len(items):
            continue
        out[key] = items[:keep]
        capped = True
        handle, reused = _register(out, key, items, store)
        resume = _resume_at(out, key, keep, reused)

        # 说 has_more=false 却还有没给的数据，就是撒谎：截断后一律为 true
        for hk in ("has_more", f"{key}_has_more"):
            if hk in out:
                out[hk] = True
        for rk in ("returned", f"{key}_returned"):
            if rk in out:
                out[rk] = keep

        head = (f"{key} 共 {len(items)} 条，超过单次响应上限 "
                f"{CAP_BYTES // 1024} KB，只回了前 {keep} 条")
        if handle and reused:
            notes.append(f"{head}；用 read_query_result(handle={handle!r}, "
                         f"offset={resume}, limit=...) 继续取"
                         f"（该句柄里有全部 {out.get('total', len(items))} 条）")
        elif handle:
            notes.append(f"{head}；完整列表已存为句柄 {handle!r}，用 "
                         f"read_query_result({handle!r}, offset={resume}, "
                         f"limit=...) 取剩下的")
        else:
            notes.append(f"{head}；当前没有可用的句柄表，剩下的取不到 —— "
                         f"请缩小 limit 或用 pattern 过滤")

    if capped:
        out["capped"] = True
        out["cap_bytes"] = CAP_BYTES

    if measure(out) > CAP_BYTES:
        # 只可能是「体积不在列表里」（某个 dict / 超长字符串），
        # 那种情况不截，但必须如实说：宁可超限，也不许悄悄丢字段
        out["oversize"] = True
        out["cap_bytes"] = CAP_BYTES
        notes.append(f"截断顶层列表后仍超过 {CAP_BYTES // 1024} KB，"
                     f"超出部分不在列表里（工具 {tool}），请缩小查询范围")

    if notes:
        old = out.get("note")
        out["note"] = f"{old}；{'；'.join(notes)}" if old else "；".join(notes)
    return out
