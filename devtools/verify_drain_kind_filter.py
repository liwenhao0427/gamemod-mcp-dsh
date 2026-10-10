#!/usr/bin/env python3
"""验收：`drain_events(kind=...)` 必须过滤**整条队列**，不是只看队首。

被修复的缺陷（实测，不是推测）
============================================================
`RuntimeSession.drain_events` 的过滤写法是：

    if kind and ev.get("type") != kind:
        self._push_event(ev)     # 放回
        break                    # ← 一看队首不匹配就退出

于是**只要队首那一条不是要的类型，后面排着多少条匹配事件都取不到**。

在真实游戏上的表现（Pass The Fear / IL2CPP，`get_frameCount` + `get_gameObject`
两个 Hook 各触发 50 次）：

    drain_events(kind="hook")          -> returned=0,  pending_approx=201
    drain_events()                     -> 立刻取到一堆 type:"hook" 的事件

**诱因是注入后队首恒有一条 `notice`（probe_ready）** —— 探针一就绪就发，
永远排在队列最前面，正好把整条队列堵死。

为什么这是「假成功」而不是「小毛病」
============================================================
返回值是 `ok: True` + `events: []`。调用方（Agent）会得出
「期间没有任何 Hook 触发」的结论，然后去排查 **Hook 落点**
（改 token、换方法、怀疑重载挂错）—— 而落点其实完全正确。
这正是 AGENTS.md 最忌讳的「说了话但不干活 / 静默给错信息」。

还要验的边界
============================================================
1. 队首不匹配时，后面的匹配事件**必须能取到**（核心回归）；
2. 队首匹配时行为不变；
3. **不匹配的事件不许被吞掉**：过滤取 hook 之后，notice 还得留在队列里
   （早期「修法」如果直接把不匹配的丢掉，就是另一种数据丢失）；
4. 超过 `limit` 的匹配事件也不许丢，要留在队列里下次能取；
5. 相对顺序必须保持；
6. `kind=""`（不过滤）行为完全不变；
7. `pending_approx` 要与实际剩余量对得上。

非空转反证
============================================================
把 `drain_events` 换回修复前的写法（只看队首），第 1 条断言必须**报红**。
否则说明这套断言根本没在测那个缺陷。

    python devtools/verify_drain_kind_filter.py
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import io

_sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8",
                               errors="replace")

import samples  # noqa: E402
samples.add_src_to_path()

from gamemod_mcp.runtime.session import RuntimeSession  # noqa: E402

PASS, FAIL = [], []


def check(label: str, cond: bool, detail: str = "") -> None:
    tag = "✅" if cond else "❌"
    print(f"  {tag} {label}" + (f" — {detail}" if detail else ""))
    (PASS if cond else FAIL).append(label)


def show(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def make_session() -> RuntimeSession:
    """造一个**不碰 Frida** 的会话对象：只需要事件队列那部分。

    为什么能这么造：`drain_events` 只依赖 `self._events` / `self._dropped`，
    与 Frida 完全无关。用桩对象就能确定性地复现队首/顺序这些边界，
    不需要启动游戏、也不需要管理员权限。
    """
    s = RuntimeSession.__new__(RuntimeSession)
    import queue
    s._events = queue.Queue(maxsize=20000)
    s._dropped = 0
    return s


def fill(sess, events) -> None:
    for ev in events:
        sess._events.put_nowait(ev)


def hook(i: int) -> dict:
    return {"type": "hook", "id": f"h{i}", "phase": "enter", "seq": i}


def notice() -> dict:
    return {"type": "notice", "payload": {"type": "probe_ready"}}


# ---------------------------------------------------------------- 1. 核心回归

def part_core() -> None:
    show("1. 【缺陷核心】队首不匹配时，后面的匹配事件必须能取到")

    # 真实场景：探针一就绪就发的 notice 排在队首，后面才是 Hook 事件
    sess = make_session()
    fill(sess, [notice(), hook(1), hook(2), hook(3)])
    r = sess.drain_events(limit=10, kind="hook")

    print(f"     队列 = [notice, hook1, hook2, hook3]")
    print(f"     drain_events(kind='hook') -> returned={r['returned']} "
          f"pending={r['pending_approx']}")
    check("队首是 notice 时仍能取到 hook 事件（修复前返回 0 条）",
          r["returned"] == 3,
          f"returned={r['returned']}")
    check("取到的确实都是 hook 类型",
          all(e.get("type") == "hook" for e in r["events"]),
          f"types={[e.get('type') for e in r['events']]}")
    check("hook 事件的顺序与入队顺序一致",
          [e["seq"] for e in r["events"]] == [1, 2, 3],
          f"seq={[e['seq'] for e in r['events']]}")

    # 不匹配的事件**不许被吞掉**：这是本条验收最容易被「修坏」的地方 ——
    # 把不匹配的直接丢弃也能让上面三条变绿，但那是数据丢失。
    rest = sess.drain_events(limit=10)
    check("不匹配的 notice 没有被吞掉，仍在队列里",
          rest["returned"] == 1 and rest["events"][0]["type"] == "notice",
          f"剩余={[e.get('type') for e in rest['events']]}")

    # 多个不匹配项夹在中间：整条队列都要扫到，不能遇到第一个不匹配就停
    sess = make_session()
    fill(sess, [notice(), hook(1), notice(), hook(2), notice()])
    r2 = sess.drain_events(limit=10, kind="hook")
    print(f"     队列 = [notice, hook1, notice, hook2, notice] -> "
          f"returned={r2['returned']}")
    check("不匹配项夹在中间时也能取到后面的匹配事件",
          [e["seq"] for e in r2["events"]] == [1, 2],
          f"seq={[e.get('seq') for e in r2['events']]}")
    left = sess.drain_events(limit=10)
    check("夹在中间的不匹配项全部保留",
          left["returned"] == 3
          and all(e["type"] == "notice" for e in left["events"]),
          f"剩余 {left['returned']} 条")


# ---------------------------------------------------------------- 2. 边界

def part_edges() -> None:
    show("2. 边界：limit / 顺序 / 空队列 / 不过滤行为不变")

    # 队首就匹配（修复前也能过，用来确认没有改坏原行为）
    sess = make_session()
    fill(sess, [hook(1), hook(2), notice()])
    r = sess.drain_events(limit=10, kind="hook")
    check("队首匹配时行为不变",
          [e["seq"] for e in r["events"]] == [1, 2],
          f"seq={[e.get('seq') for e in r['events']]}")

    # limit 必须生效，且**超出的匹配事件不许丢**
    sess = make_session()
    fill(sess, [hook(i) for i in range(1, 11)])
    r = sess.drain_events(limit=3, kind="hook")
    check("limit 生效（只要 3 条）", r["returned"] == 3,
          f"returned={r['returned']}")
    r2 = sess.drain_events(limit=100, kind="hook")
    check("被 limit 挡下的匹配事件留在队列里，下次能取到（不丢数据）",
          [e["seq"] for e in r2["events"]] == [4, 5, 6, 7, 8, 9, 10],
          f"第二次 seq={[e.get('seq') for e in r2['events']]}")

    # 相对顺序：hook 与 notice 交错时，剩下的 notice 顺序不能乱
    sess = make_session()
    fill(sess, [hook(1), notice(), hook(2), notice(), hook(3)])
    sess.drain_events(limit=10, kind="hook")
    left = sess.drain_events(limit=10)
    check("剩余事件的相对顺序保持不变",
          left["returned"] == 2
          and all(e["type"] == "notice" for e in left["events"]),
          f"剩余={left['returned']} 条")

    # 空队列不该抛
    sess = make_session()
    r = sess.drain_events(limit=10, kind="hook")
    check("空队列时返回空列表而不是报错",
          r["ok"] is True and r["returned"] == 0,
          f"ok={r['ok']} returned={r['returned']}")

    # 全都不匹配：一条都不该返回，也不该把队列清空
    sess = make_session()
    fill(sess, [notice(), notice()])
    r = sess.drain_events(limit=10, kind="hook")
    check("全不匹配时返回 0 条且不改动队列",
          r["returned"] == 0 and r["pending_approx"] == 2,
          f"returned={r['returned']} pending={r['pending_approx']}")

    # kind="" 必须与修复前**完全**一致：按 limit 取，且不丢未取到的
    sess = make_session()
    fill(sess, [notice(), hook(1), notice()])
    r = sess.drain_events(limit=2)
    check("kind='' 时按 limit 顺序取（行为不变）",
          [e["type"] for e in r["events"]] == ["notice", "hook"],
          f"types={[e.get('type') for e in r['events']]}")
    check("kind='' 时未取到的留在队列里",
          sess.drain_events(limit=10)["returned"] == 1,
          "剩 1 条 notice")

    # pending_approx 要与实际剩余量对得上（它是客户端判断「还有没有」的依据）
    sess = make_session()
    fill(sess, [notice(), hook(1), hook(2)])
    r = sess.drain_events(limit=1, kind="hook")
    check("pending_approx 与实际剩余量一致",
          r["pending_approx"] == 2,
          f"pending_approx={r['pending_approx']}（期望 2：1 条 notice + 1 条超限 hook）")


# ---------------------------------------------------------------- 3. 反证

def part_counterproof() -> None:
    show("3. 非空转反证：换回旧写法必须报红")

    class OldSession(RuntimeSession):
        """修复前的实现：只看队首，不匹配就放回并 break。"""

        def drain_events(self, limit: int = 200, kind: str = "") -> dict:
            out = []
            while len(out) < limit:
                try:
                    ev = self._events.get_nowait()
                except Exception:
                    break
                if kind and ev.get("type") != kind:
                    self._push_event(ev)
                    break
                out.append(ev)
            return {"ok": True, "events": out, "returned": len(out),
                    "pending_approx": self._events.qsize(),
                    "dropped": self._dropped}

    sess = OldSession.__new__(OldSession)
    import queue
    sess._events = queue.Queue(maxsize=20000)
    sess._dropped = 0
    fill(sess, [notice(), hook(1), hook(2), hook(3)])
    r_old = sess.drain_events(limit=10, kind="hook")

    print(f"     旧写法 drain_events(kind='hook') -> "
          f"returned={r_old['returned']} pending={r_old['pending_approx']}")
    check("反证：旧写法确实返回 0 条（说明上面第 1 节不是空转）",
          r_old["returned"] == 0,
          f"returned={r_old['returned']}（旧写法被队首 notice 挡住）")

    # 同一份数据、同一条断言，新写法必须通过 —— 这才是真正在测那个改动
    sess2 = make_session()
    fill(sess2, [notice(), hook(1), hook(2), hook(3)])
    r_new = sess2.drain_events(limit=10, kind="hook")
    check("（对照）同一份数据新写法返回 3 条",
          r_new["returned"] == 3,
          f"returned={r_new['returned']}")


def main() -> int:
    print("验收：drain_events 的 kind 过滤必须作用于整条队列")
    part_core()
    part_edges()
    part_counterproof()

    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    _sys.exit(main())
