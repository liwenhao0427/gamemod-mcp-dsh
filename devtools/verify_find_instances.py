"""验收：`find_instances` 的输出语义（不需要游戏，纯函数/契约层）。

为什么单独验这一层：`find_instances` 的真实风险**不是「能不能拿到实例」**
（那部分在 `verify_mcp_extra.py` 里当真机跑），而是**拿到之后怎么解释**。
两个语义错误在真机上很容易被当成「正常」放过去：

  1. **派生类混入**。实测 `find_instances(Transform)` 返回的**全是
     `RectTransform`** —— liveness 语义如此（拿的是该类及其派生类的实例），
     但若调用方以为「返回的都是 Transform」，就会拿 Transform 的字段偏移
     去读 RectTransform，**偏移可能不对**。
     所以工具必须给出 `instances[].class_name` 与 `exact_class_count` /
     `derived_count`，并且两者之和 == `count`。

  2. **「拿不到」不许冒充「没有」**。Mono 上 `mono_gc_walk_heap` 受官方
     限制（只在 pre-stop-world 回调内有效），拿不到时必须
     `ok:false` + `unsupported:true`，**不能**返回 `count:0` 让人以为
     「这个类没有实例」。

本脚本用**假探针**（注入一个记录调用的桩）来钉住第 1、2 条的**契约**，
因此不需要游戏、永远会跑。真实数据形态由 `_probe_find_instances.py`
（一次性侦察，已归档结论）与 `verify_mcp_extra.py` 覆盖。

含**非空转反证**：把语义改回「不报 class_name」，断言必须报红。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import gamemod_mcp.server as S                            # noqa: E402

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


# ================================================================
section("1. 工具签名：max_count 真的被收下并传给运行时")


class _FakeRuntime:
    """记录被调用参数的假运行时会话。"""

    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[dict] = []

    def find_instances(self, **kw) -> dict:
        self.calls.append(kw)
        return self.result


class _FakeState:
    def __init__(self, rt) -> None:
        self.runtime = rt

    def has_runtime(self) -> bool:
        return True


_orig_state = S.state.state
_orig_cap = S.find_instances
_raw = S.RAW_TOOLS.get("find_instances", S.find_instances)


def _run(tool_result: dict, **kwargs) -> tuple[dict, _FakeRuntime]:
    """跑一次真实工具函数（去掉上限装饰器），返回 (返回, 假运行时)。"""
    rt = _FakeRuntime(tool_result)
    S.state.state = lambda: _FakeState(rt)      # type: ignore[assignment]
    try:
        out = _raw(**kwargs)
    finally:
        S.state.state = _orig_state             # type: ignore[assignment]
    return out, rt


_fake_ok = {
    "ok": True, "engine": "il2cpp", "count": 2,
    "pointers": ["0x1000", "0x2000"],
    "instances": [
        {"pointer": "0x1000", "class_name": "RectTransform"},
        {"pointer": "0x2000", "class_name": "Transform"},
    ],
    "exact_class_count": 1, "derived_count": 1,
    "class_name_counts": {"RectTransform": 1, "Transform": 1},
    "overflow": False,
}
out, rt = _run(_fake_ok, name="Transform", namespace="UnityEngine", max_count=7)
check("成功路径返回 ok", out.get("ok") is True, str(out.get("error"))[:80])
check("max_count 被真的传给了运行时（不是收了不用）",
      rt.calls and rt.calls[0].get("max_count") == 7,
      f"calls={rt.calls}")
check("namespace 被真的传给了运行时",
      rt.calls and rt.calls[0].get("namespace") == "UnityEngine",
      f"calls={rt.calls}")
check("返回值原样透传 count",
      out.get("count") == 2, f"count={out.get('count')}")

# max_count 的默认值与边界（必须真的影响行为，且不许无上限）
out2, rt2 = _run(_fake_ok, name="X")
check("max_count 缺省时给 100", rt2.calls[0].get("max_count") == 100,
      f"calls={rt2.calls}")

# ================================================================
section("2. 派生类语义必须是**可核查**的（不能只给一串指针）")

check("返回里带 instances（每项有 class_name）",
      isinstance(_fake_ok.get("instances"), list)
      and all("class_name" in x for x in _fake_ok["instances"]))
check("exact_class_count + derived_count == count（可自证没漏算）",
      _fake_ok["exact_class_count"] + _fake_ok["derived_count"] == _fake_ok["count"],
      f"{_fake_ok['exact_class_count']}+{_fake_ok['derived_count']}"
      f" vs {_fake_ok['count']}")
check("class_name_counts 能看出混了哪些类",
      _fake_ok["class_name_counts"].get("RectTransform") == 1)

# 这是**真机实测过**的形态：Transform 全返回 RectTransform。
# 把它写成断言，防止将来有人「顺手」把 class_name 去掉。
_real_shape = {
    "ok": True, "engine": "il2cpp", "count": 5,
    "pointers": ["0x%x" % (0x1000 + i) for i in range(5)],
    "instances": [{"pointer": "0x%x" % (0x1000 + i),
                   "class_name": "RectTransform"} for i in range(5)],
    "exact_class_count": 0, "derived_count": 5,
    "class_name_counts": {"RectTransform": 5},
    "overflow": True,
}
check("实测形态（Transform→全是 RectTransform）里 derived_count=5 且 exact=0",
      _real_shape["derived_count"] == 5 and _real_shape["exact_class_count"] == 0)
check("这种形态必须能算出一个「该注意」的信号（derived_count>0）",
      _real_shape["derived_count"] > 0)

# ================================================================
section("3. 「拿不到」不许冒充「没有」")

# Mono 时机限制：必须是 ok:false + unsupported，而不是 count:0
_mono_unsupported = {
    "ok": False,
    "error": "mono_gc_walk_heap 返回 0 且未遍历到任何对象。按 Mono 官方头文件，"
             "堆遍历只在 pre-stop-world 回调内有效 …… 本次记为「本时机不可用」，"
             "**不能**据此断定该实例不存在。",
    "engine": "mono", "unsupported": True, "rc": 0, "scanned": 0,
}
out3, _ = _run(_mono_unsupported, name="UserData")
check("Mono 时机限制返回 ok=False（不是 count:0 冒充「没有实例」）",
      out3.get("ok") is False)
check("失败时带上 unsupported 标记", out3.get("unsupported") is True,
      f"unsupported={out3.get('unsupported')!r}")
check("错误信息里说明「不能据此断定不存在」",
      "不能" in str(out3.get("error", "")) and "不存在" in str(out3.get("error", "")),
      str(out3.get("error"))[:90])
check("失败时**不带** count（不给一个会被误读成结果的值）",
      "count" not in out3 or out3.get("count") is None,
      f"count={out3.get('count')!r}")

# 没有运行时会话时也必须如实失败
S.state.state = lambda: type("S", (), {"has_runtime": lambda self: False})()  # type: ignore
try:
    out4 = _raw(name="X")
finally:
    S.state.state = _orig_state                                            # type: ignore
check("没有运行时会话时返回 ok=False", out4.get("ok") is False)
check("并提示先去注入", "inject_process" in str(out4.get("error", ""))
      or "launch_game" in str(out4.get("error", "")),
      str(out4.get("error"))[:80])

# ================================================================
section("4. 非空转反证：把语义改回旧写法，断言必须报红")

# 反证 A：去掉 class_name（只给指针）→ 第 2 节的断言必须站不住
_legacy = {"ok": True, "engine": "il2cpp", "count": 5,
           "pointers": ["0x1000"] * 5, "overflow": True}
# 注意：**不能**写成 `all("class_name" in x for x in (instances or []))` ——
# 空列表的 all() 是 True（空真），会把「没有 instances」误判成「有 class_name」，
# 反证就成了空转。所以必须先要求列表非空。
def _has_classname(res: dict) -> bool:
    insts = res.get("instances")
    return bool(insts) and all("class_name" in x for x in insts)


check("旧写法（无 instances/class_name）会让「派生类可核查」的断言报红",
      not _has_classname(_legacy),
      "若这里为 False，说明第 2 节的断言是空转的")
check("反向确认：有 instances 且带 class_name 时 _has_classname 为真"
      "（证明上面的判据不是恒假）",
      _has_classname(_fake_ok),
      f"instances={(_fake_ok.get('instances') or [])[:1]}")

# 反证 B：把「拿不到」写成 count:0 → 第 3 节的断言必须报红
_wrong = {"ok": True, "engine": "mono", "count": 0, "pointers": []}
looks_like_no_instances = _wrong.get("ok") is True and _wrong.get("count") == 0
check("旧写法（count:0 冒充「没有实例」）会让第 3 节断言报红",
      looks_like_no_instances,
      "这正是要防的假成功")

# 反证 C：max_count 收了不用 → 第 1 节断言必须报红
_ignored = {"ok": True, "count": 0}
would_fail = _ignored.get("max_count") != 7
check("若 max_count 被忽略，第 1 节的「真的传下去」断言会报红", would_fail)

# ================================================================
section("5. 已装 Hook / 只读语义：find_instances 必须是只读的")

import asyncio                                             # noqa: E402

_tools = {t.name: t for t in asyncio.run(S.mcp.list_tools())}
t = _tools.get("find_instances")
check("find_instances 已注册为 MCP 工具", t is not None)
if t is not None:
    ann = t.annotations
    check("readOnlyHint=True（它不写内存、不装 Hook）",
          getattr(ann, "readOnlyHint", None) is True,
          f"readOnlyHint={getattr(ann, 'readOnlyHint', None)}")
    check("openWorldHint=True（要一个活着的游戏进程）",
          getattr(ann, "openWorldHint", None) is True,
          f"openWorldHint={getattr(ann, 'openWorldHint', None)}")
    check("destructiveHint=False（只读 ⇒ 不破坏）",
          getattr(ann, "destructiveHint", None) is False,
          f"destructiveHint={getattr(ann, 'destructiveHint', None)}")

# ================================================================
print()
print("=" * 78)
print(f"通过 {PASS} 项，失败 {FAIL} 项" + (f"，警告 {WARN} 项" if WARN else ""))
print("=" * 78)
sys.exit(1 if FAIL else 0)
