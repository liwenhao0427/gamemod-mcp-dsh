"""端到端验证：**通过 MCP 工具层**（而不是直接调内部 API）走完整流程。

为什么要这样测
--------------------------------------------------
前面几个验证脚本都是直接 import 内部模块，绕过了 server.py。
但交付物是「给通用 Agent 用的 MCP 服务器」，
真正要保证的是：Agent 通过 MCP 工具调用能拿到正确结果。

所以这里直接调用 server.py 里那些被 @mcp.tool 装饰的函数本体
（FastMCP 装饰器返回的仍是原函数，可直接调用），
验证「工具签名 → 返回值结构 → 数据正确性」整条链路。

覆盖的流程（与真实 Agent 用法一致）：
    1. get_diagnostics          环境自检
    2. inspect_game             识别引擎
    3. open_game                建索引
    4. search_classes           搜索
    5. class_detail             看字段/方法
    6. read_query_result        翻页
    7. launch_game              启动并注入
    8. runtime_info             一致性自检
    9. runtime_class_info       运行时字段偏移 vs 静态元数据
   10. find_hook_target         Hook 落点 + token 比对
   11. hook_method / drain_events / hook_status / unhook
   12. close_session           收尾
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
# 仓库路径由 samples.py 推导（不许硬编码工作区绝对路径）
# 用 os.path 推导，避免依赖后面才 import 的 pathlib
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import samples  # noqa: E402
samples.add_src_to_path()

from gamemod_mcp import server as S  # noqa: E402
from gamemod_mcp.engine import state  # noqa: E402

# 样本路径：根目录从 samples.py 取，派生路径用相对路径拼出来
_GAME = samples.find_il2cpp()
GAME_DIR = str(_GAME) if _GAME else None
GAME_EXE = str(_GAME / "Garden of Witches.exe") if _GAME else None

# 样本不可用时记为「无法验证」并跳过（跳过不算失败）
if not samples.require(GAME_DIR, "IL2CPP 样本 Garden of Witches"):
    sys.exit(0)
if not os.path.isfile(GAME_EXE):
    print(f"  ⚠️  无法验证：样本主程序不存在 —— {GAME_EXE}")
    sys.exit(0)

PASS, FAIL, WARN = [], [], []


def check(label: str, cond: bool, detail: str = "", warn_only: bool = False):
    tag = "✅" if cond else ("⚠️ " if warn_only else "❌")
    print(f"  {tag} {label}" + (f" — {detail}" if detail else ""))
    if cond:
        PASS.append(label)
    elif warn_only:
        WARN.append(label)
    else:
        FAIL.append(label)


def show(title: str):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ================================================================ 1. 诊断
show("1. get_diagnostics —— 环境自检")
d = S.get_diagnostics()
check("get_diagnostics 返回 ok", d.get("ok"), f"version={d.get('version')}")
print(f"     Frida: {d.get('frida', {}).get('installed')} "
      f"v{d.get('frida', {}).get('version')}")
check("IL2CPP 解析器就绪", d["engines"]["il2cpp"]["ready"])
check("Mono 解析器就绪", d["engines"]["mono"]["ready"],
      "mono_meta.py 是否存在", warn_only=True)
check("UE 解析器就绪", d["engines"]["ue"]["ready"],
      "ue.py 是否存在", warn_only=True)
# get_diagnostics 默认不再扫本机进程（耗时约 2.7s，且与 find_game_processes 重复），
# 所以这里改为：确认它**如实说明跳过了**，并验证专门的工具能列出进程。
check("get_diagnostics 如实标注跳过了进程扫描",
      "running_games_skipped" in d,
      "默认不扫本机进程（与 find_game_processes 职责分开）")

# ---- P3-1：这里曾有一个 `optional_packages`「能力探测」字段 --------------
# 它报的是 capstone / pefile / frida / mcp 装没装，但前两个本仓库**从来不 import**
# （PE 解析是 pe.py 手写的，也没有指令级分析）。报 `false` 会让 Agent 以为
# 「装个包就能解锁能力」，而那个能力根本不存在 —— 典型的「说了话但不干活」。
check("get_diagnostics 不再报 optional_packages（否则是假能力承诺）",
      "optional_packages" not in d,
      "它曾把 capstone / pefile 的 find_spec 结果当成可用能力报给客户端")
# 非空转反证：同一个 membership 判据在**确实存在**的键上必须为 True，
# 否则上面那条可能是「拿错了字典」而恒真。
check("（反证）同一判据在确实存在的字段上成立 → 上面那条不是恒真",
      "decompiler" in d and "engines" in d,
      f"顶层键={sorted(d.keys())}")

# 删它的**依据**也要钉住：源码里确实没有 import capstone / pefile。
# 哪天有人真接上指令级分析，这里会报红 —— 那时该做的是**恢复一个真的有读取方的
# 字段**，而不是把断言删掉。
_src_root = os.path.join(samples.ROOT, "src")
_src_files = []
for _dirpath, _dirnames, _filenames in os.walk(_src_root):
    for _fn in _filenames:
        if _fn.endswith(".py"):
            _src_files.append(os.path.join(_dirpath, _fn))
_src_text = "\n".join(
    open(_f, encoding="utf-8").read() for _f in sorted(_src_files))
check("源码里确实没有 import capstone（删掉那个字段的依据）",
      "import capstone" not in _src_text,
      f"扫了 {len(_src_files)} 个源文件")
check("源码里确实没有 import pefile（PE 解析是手写的）",
      "import pefile" not in _src_text)
# 非空转反证：这次扫描确实读到了源码内容（否则「没找到」只是因为读了个空串）。
check("（反证）同一次扫描能找到真实存在的 import → 扫描不是空转",
      "import sqlite3" in _src_text,
      f"{len(_src_text)} 字符")
gp = S.find_game_processes()
check("find_game_processes 可用（列运行中的游戏）", gp.get("ok") is True,
      f"本机发现 {gp.get('count')} 个")

# ================================================================ 2. 识别引擎
show("2. inspect_game —— 识别游戏引擎")
ig = S.inspect_game(GAME_DIR)
check("inspect_game 返回 ok", ig.get("ok"))
check("识别为 il2cpp", ig.get("engine") == "il2cpp", f"实际={ig.get('engine')}")
print(f"     主程序: {ig.get('main_exe')}")
print(f"     元数据: {ig.get('il2cpp', {}).get('metadata', '')[:70]}...")

# ================================================================ 3. 建索引
show("3. open_game —— 选定游戏并建索引")
og = S.open_game(game_dir=GAME_DIR)
check("open_game 返回 ok", og.get("ok"), og.get("error", ""))
if not og.get("ok"):
    print("  索引失败，后续无法继续"); sys.exit(1)
t0 = time.time()
og2 = S.open_game(game_dir=GAME_DIR)       # 第二次应复用同一会话，不重建
dt2 = time.time() - t0
check("重复 open_game 复用会话（不重建索引）",
      og2.get("ok") and og2.get("reused") is True,
      f"二次耗时 {dt2:.2f}s")
check("二次调用足够快（<1s，说明没重建）", dt2 < 1.0, f"{dt2:.2f}s")
idx_info = og2.get("index") or {}
print(f"     类型={idx_info.get('type_count')} 方法={idx_info.get('method_count')} "
      f"字段={idx_info.get('field_count')} 枚举={idx_info.get('enum_count')}")
check("类型数为 20072", idx_info.get("type_count") == 20072,
      f"实际={idx_info.get('type_count')}")

# 直接连索引库，供「total 是否真实」这类断言拿**独立于被测代码**的真值。
# 走 COUNT(*) 而不是再调一次被测工具 —— 拿被测实现当真值等于没验证。
_idx_conn = sqlite3.connect(str(state.state().index.db_path))

# ================================================================ 4. 搜索
show("4. search_classes —— 搜索类")
sc = S.search_classes(pattern="UserData_Service")
check("search_classes 返回 ok", sc.get("ok"))
print(f"     命中 {sc.get('total')} 个, 返回 {sc.get('returned')} 个, "
      f"handle={sc.get('handle')}")
names = [i.get("nested_name") or i.get("full_name") for i in sc.get("items", [])]
check("搜到 UserData_Service",
      any("UserData_Service" in (n or "") for n in names),
      f"items={names[:3]}")
check("返回结果带 handle", bool(sc.get("handle")))

mb = S.search_classes(subtype_of="UnityEngine.MonoBehaviour", limit=5)
check("按基类过滤可用", mb.get("ok") and (mb.get("total") or 0) > 1000,
      f"MonoBehaviour 子类 {mb.get('total')} 个")

en = S.search_classes(only_enums=True, pattern="CurrencyKey", limit=5)
check("枚举过滤可用", en.get("ok") and (en.get("total") or 0) >= 1,
      f"命中 {en.get('total')}")

# ---- list_images：MCP 层此前只在会话对象上验证过，工具层没跑过 ----
li = S.list_images()
check("list_images 返回 ok", li.get("ok"), li.get("error", ""))
print(f"     程序集 {li.get('total')} 个, 返回前 {li.get('returned')} 个")
check("程序集数量合理（>10）", (li.get("total") or 0) > 10,
      f"实际={li.get('total')}")
_top = (li.get("items") or [{}])[0]
check("每项含 image 与 type_count",
      bool(_top.get("image")) and _top.get("type_count") is not None,
      f"首项={_top}")
li_f = S.list_images(pattern="TeamTapas")
check("list_images 支持 pattern 过滤",
      li_f.get("ok") and (li_f.get("total") or 0) >= 1
      and all("TeamTapas" in (i.get("image") or "")
              for i in (li_f.get("items") or [])),
      f"命中 {li_f.get('total')} 个（过滤后每项都应含 TeamTapas）")
# 反例：不匹配的 pattern 必须如实返回 0，而不是回退成全量
li_none = S.list_images(pattern="绝对不存在的程序集名_xyz")
check("list_images pattern 无命中时如实返回 0",
      li_none.get("ok") and li_none.get("total") == 0
      and not (li_none.get("items") or []),
      f"total={li_none.get('total')}")
# 契约第 5 条：limit 截断必须如实标注，且要能翻页。
# 以前只给 total/returned，既无 has_more 也无 offset ——
# Agent 既无法确认被截断，也无法拿到后面的程序集。
li_p1 = S.list_images(limit=3, offset=0)
li_p2 = S.list_images(limit=3, offset=3)
check("list_images 如实标注 has_more",
      li_p1.get("has_more") is True and li_p1.get("offset") == 0
      and li_p1.get("limit") == 3,
      f"has_more={li_p1.get('has_more')} offset={li_p1.get('offset')}")
check("list_images 翻页两页不重叠",
      not ({i["image"] for i in li_p1["items"]}
           & {i["image"] for i in li_p2["items"]}),
      f"p1={len(li_p1['items'])} p2={len(li_p2['items'])}")
check("list_images 末页 has_more 为 False",
      S.list_images(limit=3, offset=(li.get("total") or 1) - 1)
      .get("has_more") is False)
check("list_images 过滤零命中时给出说明",
      bool(li_none.get("note")), str(li_none.get("note"))[:60])

# ---- search_fields / search_methods：total 真值、offset 翻页、owner 消歧 ----
# 这三项都是本轮修复的真实缺陷，断言要点是「对得上真值」，不是「没报错」。
# 真值直接来自索引库的 COUNT(*)，是独立于工具实现的一条路径。
show("4b. search_fields / search_methods —— total 真值 + 翻页 + owner 消歧")

_tp = "a"   # 用极泛的模式把结果数顶到句柄上限之上


def _truth(table: str, pat: str) -> int:
    """直接用 SQL 算真值（绕开被测代码）。"""
    row = _idx_conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE name LIKE ?", (f"%{pat}%",)).fetchone()
    return row[0]


for _tab, _fn, _label in (("fields", S.search_fields, "search_fields"),
                          ("methods", S.search_methods, "search_methods")):
    _truth_n = _truth(_tab, _tp)
    _r = _fn(pattern=_tp, limit=5)
    print(f"     {_label}({_tp!r}): total={_r.get('total')} 真值={_truth_n} "
          f"returned={_r.get('returned')} has_more={_r.get('has_more')}")
    check(f"{_label} 的 total 是真实总数（不是被 LIMIT 截断的值）",
          _r.get("total") == _truth_n,
          f"工具={_r.get('total')} 真值={_truth_n}")
    check(f"{_label} 返回了 returned/offset/limit/has_more",
          all(k in _r for k in ("returned", "offset", "limit", "has_more")))
    check(f"{_label} 结果远多于 limit 时 has_more 必须为 True",
          _r.get("has_more") is True, f"has_more={_r.get('has_more')}")

# total 超过句柄上限时要如实标注（句柄存不下全集，但不能装作存下了）
_big = S.search_fields(pattern=_tp, limit=5)
if (_big.get("total") or 0) > 20000:
    check("总数超过句柄上限时如实标注 handle_truncated",
          _big.get("handle_truncated") is True and bool(_big.get("handle_note")),
          f"handle_truncated={_big.get('handle_truncated')}")

# offset 必须真的生效：两页不能有重叠，且合起来是前 2*limit 条
_p1 = S.search_fields(pattern=_tp, offset=0, limit=10)
_p2 = S.search_fields(pattern=_tp, offset=10, limit=10)
_i1 = [i["index"] for i in _p1.get("items") or []]
_i2 = [i["index"] for i in _p2.get("items") or []]
print(f"     offset 翻页: 第1页 {len(_i1)} 条 / 第2页 {len(_i2)} 条, 重叠="
      f"{len(set(_i1) & set(_i2))}")
check("offset 生效（两页无重叠）", not (set(_i1) & set(_i2)),
      f"交集={sorted(set(_i1) & set(_i2))[:3]}")
check("第二页返回的 offset 被如实回显", _p2.get("offset") == 10,
      f"offset={_p2.get('offset')}")

# 小结果集不该出现 has_more=True（假截断）
_small = S.search_fields(pattern="Effect", limit=2000)
check("小结果集 has_more 为 False（不假报截断）",
      _small.get("has_more") is False,
      f"total={_small.get('total')} returned={_small.get('returned')} "
      f"has_more={_small.get('has_more')}")

# owner 命中多个同名类时必须拒绝。
# 这是本轮修的核心缺陷：原来会把两个无关类（跨程序集）的字段合并返回，
# ok 仍是 true，Agent 据此算偏移必然错。
_amb = S.search_fields(pattern="a", owner="UserData")
print(f"     owner='UserData'（同名类跨程序集）: ok={_amb.get('ok')} "
      f"candidates={len(_amb.get('candidates') or [])}")
check("owner 命中多个同名类时拒绝，不再静默合并",
      _amb.get("ok") is False and len(_amb.get("candidates") or []) > 1,
      f"ok={_amb.get('ok')} candidates={len(_amb.get('candidates') or [])}")
if _amb.get("ok") is False:
    check("拒绝时把候选类摆出来（含程序集，便于消歧）",
          all(c.get("image") for c in _amb.get("candidates") or []),
          f"{[c.get('image') for c in (_amb.get('candidates') or [])]}")
    check("拒绝时给出可操作的 hint",
          bool(_amb.get("hint")), f"{( _amb.get('hint') or '')[:60]}")

# 用 assembly 消歧后必须成功，且**只**返回那个类的字段
_dis = S.search_fields(pattern="a", owner="UserData", assembly="TeamTapas", limit=50)
_seen = sorted({i.get("owner") for i in _dis.get("items") or []})
print(f"     + assembly='TeamTapas': ok={_dis.get('ok')} total={_dis.get('total')} "
      f"类={_seen}")
check("用 assembly 消歧后成功", _dis.get("ok") is True, _dis.get("error", ""))
check("消歧后只返回一个类的字段（不再混入同名类）",
      len(_seen) == 1 and "TeamTapas" in (_seen[0] or ""),
      f"实际涉及 {len(_seen)} 个类：{_seen}")

# 指定了错误的 assembly → 必须报错，不能退回返回那个不属于它的类
_wrong = S.search_fields(pattern="a", owner="UserData", assembly="绝对不存在的程序集")
check("assembly 与 owner 不匹配时报错（不静默返回无关类）",
      _wrong.get("ok") is False, f"ok={_wrong.get('ok')} total={_wrong.get('total')}")

# 完整类名（无歧义）直接成功，且字段确实属于那个类
_full = S.search_fields(pattern="actorData",
                        owner="TeamTapas.ProjectR.UserDatas.UserData", limit=20)
check("完整类名可直接定位（无歧义路径）",
      _full.get("ok") is True and (_full.get("total") or 0) >= 1,
      f"total={_full.get('total')}")
check("命中字段的 owner 与请求的类一致",
      all(i.get("owner") == "TeamTapas.ProjectR.UserDatas.UserData"
          for i in _full.get("items") or []),
      f"{sorted({i.get('owner') for i in _full.get('items') or []})}")

# 不存在的类仍要明确报错
_nc = S.search_fields(pattern="a", owner="这个类不存在Xyz")
check("owner 指向不存在的类时报错", _nc.get("ok") is False,
      str(_nc.get("error"))[:70])
check("报错信息里带上请求的类名（可读性）",
      "这个类不存在Xyz" in str(_nc.get("error")),
      str(_nc.get("error"))[:70])

# ================================================================ 5. 类详情
show("5. class_detail —— 字段/方法/继承链")
cd = S.class_detail("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
check("class_detail 返回 ok", cd.get("ok"), cd.get("error", ""))
if cd.get("ok"):
    flds = cd.get("fields") or []
    meths = cd.get("methods") or []
    print(f"     字段 {len(flds)} 个, 方法 {len(meths)} 个, "
          f"父类={cd.get('parent')}")
    check("字段数为 26", len(flds) == 26, f"实际={len(flds)}")
    check("方法数为 50", len(meths) == 50, f"实际={len(meths)}")
    # 字段类型必须被解析出来（这是之前踩过坑的地方）
    typed = [f for f in flds if f.get("type")]
    check("字段类型全部解析成功", len(typed) == len(flds),
          f"{len(typed)}/{len(flds)}")
    store = [m for m in meths if m.get("name") == "get_storeStatSync"]
    check("能找到 get_storeStatSync", bool(store))
    if store:
        print(f"     get_storeStatSync token={store[0].get('token')}")

# 抽查字段类型是否与地面真值一致
pl = S.class_detail("PresetLoaderV2")
if pl.get("ok"):
    pf = {f["name"]: f.get("type") for f in (pl.get("fields") or [])}
    check("PresetLoaderV2.userDataPresetFile 类型 = UnityEngine.TextAsset",
          pf.get("userDataPresetFile") == "UnityEngine.TextAsset",
          f"实际={pf.get('userDataPresetFile')}")

# ---- derived_count 必须是真值（曾写死 limit=200，MonoBehaviour 报 200/实际 1548）----
show("5b. class_detail.derived_count —— 不许被 200 截断后谎报")
cd_mb = S.class_detail("UnityEngine.MonoBehaviour")
check("class_detail(MonoBehaviour) 返回 ok", cd_mb.get("ok"),
      cd_mb.get("error", ""))
if cd_mb.get("ok"):
    dc = cd_mb.get("derived_count")
    print(f"     derived_count={dc} 列表={len(cd_mb.get('derived') or [])} "
          f"has_more={cd_mb.get('derived_has_more')}")
    # 地面真值：本样本 MonoBehaviour 的派生类实测 1548 个
    check("derived_count 是真值 1548（不是被截断的 200）", dc == 1548,
          f"实际={dc} —— 若为 200 说明又被 limit 截断了")
    check("derived 列表被截断时如实标注",
          len(cd_mb.get("derived") or []) == 50
          and cd_mb.get("derived_has_more") is True,
          f"列表={len(cd_mb.get('derived') or [])} "
          f"has_more={cd_mb.get('derived_has_more')}")
    check("derived_returned 与列表长度一致",
          cd_mb.get("derived_returned") == len(cd_mb.get("derived") or []),
          f"returned={cd_mb.get('derived_returned')}")

# ---- 同名类跨程序集：class_detail / class_hierarchy / analyze_dependency 必须拒绝 ----
# 这三个工具返回的是「这个类的全部内容」，选错类不是少几条数据，
# 而是整份字段/方法表都是别的类的，且返回里看不出异常。
# 历史缺陷：它们用 `... LIMIT 1` 静默选中，实测 class_detail("UserData")
# 返回了 Unity.InputSystem.dll 那个（11 字段），而调用方多半要游戏自己的（23 字段）。
show("5c. 同名类跨程序集 —— 必须拒绝而不是静默选一个")

# 先确认真值：索引库里 'UserData' 确实对应 2 个类型
_dup = _idx_conn.execute(
    "SELECT COUNT(*) FROM types WHERE name = 'UserData'").fetchone()[0]
check("前提成立：'UserData' 在索引里对应 2 个同名类", _dup == 2, f"实际={_dup}")

for _fn, _label in ((S.class_detail, "class_detail"),
                    (S.class_hierarchy, "class_hierarchy"),
                    (S.analyze_dependency, "analyze_dependency")):
    _r = _fn(full_name="UserData")
    _c = _r.get("candidates") or []
    print(f"     {_label}('UserData'): ok={_r.get('ok')} candidates={len(_c)}")
    check(f"{_label} 遇同名类时拒绝（不再 LIMIT 1 选一个）",
          _r.get("ok") is False and len(_c) == 2,
          f"ok={_r.get('ok')} candidates={len(_c)}")
    check(f"{_label} 的候选带程序集（可用于 assembly 消歧）",
          all(x.get("image") for x in _c), f"{[x.get('image') for x in _c]}")

# 消歧后必须落到**游戏自己的**那个类，且字段数是它的真值 23
_cd_asm = S.class_detail(full_name="UserData", assembly="TeamTapas")
print(f"     class_detail('UserData', assembly='TeamTapas'): "
      f"ok={_cd_asm.get('ok')} full={_cd_asm.get('full_name')!r} "
      f"字段={len(_cd_asm.get('fields') or [])}")
check("用 assembly 消歧后解析到游戏自己的 UserData",
      _cd_asm.get("ok") is True
      and _cd_asm.get("full_name") == "TeamTapas.ProjectR.UserDatas.UserData",
      f"实际={_cd_asm.get('full_name')!r}")
check("消歧后字段数是该类的真值 23（不是 Unity 那个的 11）",
      len(_cd_asm.get("fields") or []) == 23,
      f"实际={len(_cd_asm.get('fields') or [])} —— 若为 11 说明落到了 Unity 的类")
check("消歧后 image 是游戏程序集",
      "TeamTapas" in (_cd_asm.get("image") or ""),
      f"实际={_cd_asm.get('image')!r}")

# class_hierarchy / analyze_dependency 的消歧路径同样要能走通
_ch_asm = S.class_hierarchy(full_name="UserData", assembly="TeamTapas")
check("class_hierarchy 也能用 assembly 消歧",
      _ch_asm.get("ok") is True
      and (_ch_asm.get("root") or "").startswith("TeamTapas"),
      f"root={_ch_asm.get('root')!r}")
_ad_asm = S.analyze_dependency(full_name="UserData", assembly="TeamTapas")
check("analyze_dependency 也能用 assembly 消歧",
      _ad_asm.get("ok") is True
      and (_ad_asm.get("class") or "").startswith("TeamTapas"),
      f"class={_ad_asm.get('class')!r}")

# 完整类名（无歧义）不应被这次收紧误伤
for _nm in ("TeamTapas.ProjectR.UserDatas.UserData",
            "TeamTapas.ProjectR.UserDatas.Service.UserData_Service",
            "PresetLoaderV2"):
    _r = S.class_detail(full_name=_nm)
    check(f"完整名/唯一短名未被误伤：{_nm}", _r.get("ok") is True,
          str(_r.get("error"))[:60])

# 不存在的类必须仍是明确报错（不能退化成 found=False 的空返回骗过调用方）
_absent = S.class_detail(full_name="这个类不存在Xyz")
check("不存在的类仍明确报错", _absent.get("ok") is False,
      str(_absent.get("error"))[:60])

# ---- class_hierarchy(down) 的 depth/parent 必须是**真实**的直接继承关系 ----
# 这是审计发现的「数据本身出错」最严重的一条：早期实现把 meta.find_derived
# （传递式，返回全部后代）当成「直接子类」用，于是根节点的第一层被塞进任意
# 深度的后代，全部标 depth=1、parent=根，而真实的直接子类大量缺席。
# 实测：真值直接子类 428 个，工具报了 500 个 depth=1，其中 319 个不是直接子类，
# 247 个真实直接子类根本没出现。Agent 用 down 摸继承树会得到完全错误的结构。
# 真值用 adapt.parent_name 独立重算（不经过被测的 _direct_children）。
show("5e. class_hierarchy(down) —— depth/parent 必须是真的直接继承关系")

from gamemod_mcp.engine import adapt as _adapt   # noqa: E402

_mb = "UnityEngine.MonoBehaviour"
_mbm = S.class_hierarchy(full_name=_mb, direction="down", depth=1)
check("class_hierarchy(down) 返回 ok", _mbm.get("ok") is True,
      str(_mbm.get("error"))[:60])
_idx_meta = state.state().index.meta
_mb_root = _idx_meta.type_by_name(_mb)
# 真值：直接子类 = adapt.parent_name 恰好等于根名
_truth_direct = {t.index for t in _idx_meta.types
                 if _adapt.parent_name(_idx_meta, t) == _mb_root.nested_name}
_idx_by_name = {t.nested_name: t.index for t in _idx_meta.types}
_d1 = [x for x in (_mbm.get("down") or []) if x.get("depth") == 1]
_d1_idx = {_idx_by_name[x["name"]] for x in _d1 if x["name"] in _idx_by_name}
print(f"     真值直接子类={len(_truth_direct)}  报告 depth=1={len(_d1)}  "
      f"假={len(_d1_idx - _truth_direct)}  缺失={len(_truth_direct - _d1_idx)}")
check("depth=1 条目数 == 真值直接子类数（不混入间接后代）",
      len(_d1) == len(_truth_direct),
      f"{len(_d1)} vs {len(_truth_direct)}")
check("depth=1 里没有一条是编造的（parent 写错）",
      not (_d1_idx - _truth_direct),
      f"假={len(_d1_idx - _truth_direct)}")
check("没有真实直接子类被漏掉",
      not (_truth_direct - _d1_idx),
      f"缺失={len(_truth_direct - _d1_idx)}")
check("如实给出 down_has_more 以暴露 _CHILD_CAP 截断",
      "down_has_more" in _mbm,
      f"down_has_more={_mbm.get('down_has_more')}")

# 深层同样要对：depth=2 的每一条，其 parent 必须是它**真实**的父类
_mbm2 = S.class_hierarchy(full_name=_mb, direction="down", depth=2)
_d2 = [x for x in (_mbm2.get("down") or []) if x.get("depth") == 2]
_bad_par = [x for x in _d2 if x["name"] in _idx_by_name
            and _adapt.parent_name(_idx_meta,
                                   _idx_meta.types[_idx_by_name[x["name"]]])
            != x["parent"]]
print(f"     depth=2 条目={len(_d2)}  其 parent 与真实父类不一致={len(_bad_par)}")
check("depth=2 的 parent 与真实父类一致", not _bad_par,
      f"不一致={len(_bad_par)}")

# 500 条上限被触发时必须如实标注（System.Object 有 6449 个直接子类）
_so = S.class_hierarchy(full_name="System.Object", direction="down", depth=1)
if _so.get("ok") and (_so.get("down_count") or 0) >= 500:
    check("触发 _CHILD_CAP 时如实标注 down_has_more",
          _so.get("down_has_more") is True
          and bool(_so.get("down_truncated_levels"))
          and bool(_so.get("note")),
          f"has_more={_so.get('down_has_more')} "
          f"levels={_so.get('down_truncated_levels')}")
else:
    print("     ⚠ System.Object 未触发上限，跳过截断断言")

check("报错信息里保留请求的类名（可读性）",
      "这个类不存在Xyz" in str(_absent.get("error")),
      str(_absent.get("error"))[:60])

# _index_query 的键冲突回归：索引层的 hint 与调用方兜底 hint 同名时
# 曾经直接抛 TypeError（got multiple values for keyword argument 'hint'）
_amb_cd = S.class_detail(full_name="UserData")
check("歧义返回不因 hint 键重名而抛异常（_index_query 回归）",
      isinstance(_amb_cd, dict) and "hint" in _amb_cd,
      f"hint={str(_amb_cd.get('hint'))[:50]}")
check("保留的是索引层更具体的 hint（不是调用方那句泛泛提示）",
      "search_classes 先确认名称" not in str(_amb_cd.get("hint")),
      f"hint={str(_amb_cd.get('hint'))[:70]}")

# ---- 拒绝返回必须**可操作**：hint 照做要能真的定位到类 ----
# 这里踩过三个坑，都是实测出来的：
#   1) hint 曾建议 full_name="State" —— 正是调用方刚传进来的歧义串，照做再错一次
#   2) hint 曾无条件建议 assembly= —— 候选全在同一映像时毫无用处（UxmlTraits 54 个）
#   3) 候选列表无上限 —— "<>c" 有 456 个候选、序列化 88 KB，白吃 Agent 上下文
show("5d. 歧义拒绝的可操作性 —— hint 必须真能用、候选必须有界")

for _nm in ("UserData", "State", "UxmlTraits", "InitData"):
    _r = S.class_detail(full_name=_nm)
    _c = _r.get("candidates") or []
    _cnt = _r.get("candidate_count")
    _size = len(json.dumps(_r, ensure_ascii=False).encode())
    print(f"     class_detail({_nm!r}): 候选={_cnt} 列出={len(_c)} "
          f"payload={_size/1024:.1f}KB assembly_helps={_r.get('assembly_helps')}")

    check(f"{_nm}: 候选总数如实给出 candidate_count",
          _cnt == len(_c) or (_cnt or 0) > len(_c),
          f"candidate_count={_cnt} 列出={len(_c)}")
    check(f"{_nm}: 候选列表有上限（不超 30 条）", len(_c) <= 30, f"{len(_c)}")
    check(f"{_nm}: 错误返回体积可控（< 20 KB）", _size < 20000,
          f"{_size/1024:.1f}KB")

    # 核心断言：照 hint 回灌必须成功。hint 是给 Agent 的下一步动作，
    # 它错了比不给 hint 更糟 —— 会让 Agent 在同一个错误上打转。
    _h = _r.get("hint") or ""
    check(f"{_nm}: 给出 hint", bool(_h), f"hint={_h!r}")
    if _h:
        _sug = _h.split("=", 1)[1].strip().strip('"')
        check(f"{_nm}: hint 建议的不是调用方刚传进来的那个歧义串（否则等于没建议）",
              _sug != _nm, f"hint 又建议了 {_sug!r}")
        _r2 = S.class_detail(full_name=_sug)
        check(f"{_nm}: 照 hint 回灌能真的定位到类（hint 有效性）",
              _r2.get("ok") is True and _r2.get("nested_name") == _sug,
              f"回灌 {_sug!r} -> ok={_r2.get('ok')}")

    # 候选全在同一映像时，成因描述不能说是「不同程序集」，且要提醒 assembly 无效
    _imgs = {x.get("image") for x in _c}
    if len(_imgs) == 1:
        check(f"{_nm}: 同映像候选时如实标注 assembly_helps=False",
              _r.get("assembly_helps") is False,
              f"assembly_helps={_r.get('assembly_helps')}")
        check(f"{_nm}: 同映像候选时提示 assembly 帮不上忙（不给空头支票）",
              "帮不上忙" in str(_r.get("error")),
              str(_r.get("error"))[:80])

# 最坏情形：'<>c' 有数百个候选，payload 曾经 88 KB
_big = S.class_detail(full_name="<>c")
_big_size = len(json.dumps(_big, ensure_ascii=False).encode())
print(f"     极端情形 '<>c': 候选={_big.get('candidate_count')} "
      f"列出={len(_big.get('candidates') or [])} payload={_big_size/1024:.1f}KB")
check("大量同名（'<>c'）时 payload 仍受控（< 20 KB）", _big_size < 20000,
      f"{_big_size/1024:.1f}KB —— 修复前是 88 KB")
check("大量同名时如实标注候选被截断",
      _big.get("candidates_truncated") is True
      and bool(_big.get("candidates_note")),
      f"truncated={_big.get('candidates_truncated')}")

# search_fields 的 owner 歧义 hint 同样要真能用
_sf = S.search_fields(pattern="a", owner="UserData", limit=5)
_sf_h = _sf.get("hint") or ""
check("search_fields 的歧义也给出 hint", bool(_sf_h), f"hint={_sf_h!r}")
if _sf_h:
    _sf_sug = _sf_h.split("=", 1)[1].strip().strip('"')
    _sf2 = S.search_fields(pattern="a", owner=_sf_sug, limit=5)
    check("search_fields 的 hint 照做能成功（hint 有效性）",
          _sf2.get("ok") is True, f"回灌 {_sf_sug!r} -> {_sf2.get('ok')}")

# ================================================================ 6. 句柄翻页
show("6. read_query_result —— 结果句柄翻页 + 结果内过滤")
h = sc.get("handle")
if h:
    p1 = S.read_query_result(h, offset=0, limit=2)
    check("翻页读取成功", p1.get("ok"), p1.get("error", ""))
    if p1.get("ok"):
        check("翻页返回 2 条", p1.get("returned") == 2,
              f"实际={p1.get('returned')}")
    sq = S.read_query_result(h, pattern="User")
    check("结果内过滤成功（pattern 参数，已合并原 search_query_result）",
          sq.get("ok"), sq.get("error", ""))
    # 回归：pattern="" 与不传 pattern 必须完全等价（这是合并的前提）
    _no_pat = S.read_query_result(h, offset=0, limit=5)
    _empty_pat = S.read_query_result(h, offset=0, limit=5, pattern="")
    check("pattern='' 与不传 pattern 的行为一致",
          (_no_pat.get("items") or []) == (_empty_pat.get("items") or [])
          and _no_pat.get("total") == _empty_pat.get("total"),
          f"items {len(_no_pat.get('items') or [])} vs "
          f"{len(_empty_pat.get('items') or [])}")
    # 过滤零命中要说清是「过滤没中」而非「句柄为空」
    _zero = S.read_query_result(h, pattern="绝对不可能命中的子串Xyzzy")
    check("结果内过滤零命中时给出说明",
          _zero.get("ok") is True and bool(_zero.get("note")),
          f"note={str(_zero.get('note'))[:50]}")
    bad = S.read_query_result("不存在的句柄-xyz")
    check("无效句柄被正确拒绝", bad.get("ok") is False, bad.get("error", "")[:40])

# ================================================================ 7. 启动注入
show("7. launch_game —— 启动游戏并注入探针")
lg = S.launch_game(exe=GAME_EXE)
check("launch_game 返回 ok", lg.get("ok"), lg.get("error", ""))
pid = None
if lg.get("ok"):
    pid = lg.get("pid")
    if lg.get("warning"):
        print(f"     ⚠️  {lg.get('warning')}")
    init = lg.get("init") or {}
    print(f"     pid={pid} 模块={init.get('module')} base={init.get('base')} "
          f"等待={init.get('waited')}s 尝试={init.get('attempts')}次")
    check("il2cpp 初始化成功（spawn 后轮询等到 DLL 加载）",
          bool(init.get("module")), init.get("error", ""))
    check("无缺失的 il2cpp API", len(init.get("api_missing") or []) == 0,
          f"缺失 {len(init.get('api_missing') or [])} 个")
else:
    print("  注入失败，跳过运行时验证")

sess = S.get_session()
check("get_session 反映已注入", (sess.get("runtime") or {}).get("pid") == pid,
      f"session pid={(sess.get('runtime') or {}).get('pid')} 期望={pid}")

# ================================================================ 8. 运行时一致性
if pid:
    show("8. runtime_info —— 静态/运行时一致性自检")
    ri = S.runtime_info()
    check("runtime_info 返回 ok", ri.get("ok"), ri.get("error", ""))
    if ri.get("ok"):
        print(f"     运行时映像={ri.get('image_count')} "
              f"静态映像={ri.get('static_image_count')} "
              f"一致={ri.get('image_count_matches')}")
        check("映像数一致", ri.get("image_count_matches") is True,
              "证明探针挂到的是同一个游戏实例")
        # 60 = 原 59 + il2cpp_class_get_declaring_type（定位嵌套类型所必需）
        check("可用导出 60 个", ri.get("api_available") == 60,
              f"实际={ri.get('api_available')}")

    # ============================================================ 9. 运行时类信息
    show("9. runtime_class_info —— 真实字段偏移与方法地址")
    rc = S.runtime_class_info(name="PresetLoaderV2")
    check("runtime_class_info 返回 ok", rc.get("ok"), rc.get("error", ""))
    if rc.get("ok"):
        rt_fields = {f["name"]: f for f in (rc.get("fields") or [])}
        print(f"     字段 {len(rt_fields)} 个, 方法 {len(rc.get('methods') or [])} 个")
        # 核心断言：运行时字段偏移必须与地面真值 dump 一致
        off = (rt_fields.get("userDataPresetFile") or {}).get("offset")
        check("userDataPresetFile 运行时偏移 = 32 (0x20)",
              off == 32, f"实际={off} —— 这是地面真值 dump 里的 /*0x20*/")
        # 所有字段都应有偏移
        with_off = [f for f in rt_fields.values() if f.get("offset") is not None]
        check("全部字段都有运行时偏移", len(with_off) == len(rt_fields),
              f"{len(with_off)}/{len(rt_fields)}")
        # 静态类型 vs 运行时偏移 交叉：userDataPresetFile 静态类型是 TextAsset
        check("静态类型解析与运行时偏移互相印证",
              (rt_fields.get("userDataPresetFile") or {}).get("type")
              == "UnityEngine.TextAsset",
              "静态说它是 TextAsset，运行时说它在 0x20")

    # ============================================================ 9b. 嵌套类型
    # 运行时类定位曾**完全拿不到嵌套类型**：il2cpp_class_from_name 只查顶层
    # 类型。实测本样本 20,072 个类型里 7,056 个（35%）是嵌套类型；
    # 名为 State 的 131 个类型只有 1 个是顶层，在 TeamTapas.ProjectR.dll 里
    # 遍历出的 118 个 State 用 from_name 逐个回查 —— 命中 0、漏 118。
    # 而 Mod 最常 Hook 的恰是这类内部类（状态机、协程体、私有嵌套类）。
    show("9b. runtime_class_info —— 嵌套类型必须能定位（曾 100% 不可达）")
    _idx9 = state.state().index
    # 选样本要同时满足三件事，否则断言会变成空转：
    #   · 是嵌套类型（declaring_il2cpp 非空）且全名含外层链
    #   · 有字段（否则「字段偏移都对」在 0 个字段上是假通过）
    #   · 短名在样本里**不唯一**（这样才跑得到歧义拒绝分支）
    _nested_row = _idx9.conn.execute(
        "SELECT t.name, t.nested_name, t.image, t.field_count, t.method_count "
        "FROM types t JOIN ("
        "  SELECT name FROM types GROUP BY name HAVING COUNT(*) > 1"
        ") d ON d.name = t.name "
        "WHERE t.declaring_il2cpp NOT IN (-1) AND t.nested_name LIKE '%.%.%' "
        "  AND t.field_count > 0 AND t.method_count > 0 "
        "ORDER BY t.field_count DESC LIMIT 1").fetchone()
    if _nested_row:
        _nt = _nested_row["nested_name"]
        print(f"     静态真值（嵌套类）: {_nt}  image={_nested_row['image']}")
        # (1) 用完整嵌套名定位 —— 修复前必然 not_found
        _rn = S.runtime_class_info(name=_nt, namespace="")
        check("按完整嵌套名能定位到嵌套类", _rn.get("ok") is True,
              f"{_nt} → {_rn.get('error')}")
        if _rn.get("ok"):
            # 真值比对：运行时拼出的全名必须等于静态嵌套名
            check("运行时 full_name == 静态 nested_name",
                  _rn.get("full_name") == _nt,
                  f"运行时={_rn.get('full_name')!r} 静态={_nt!r}")
            check("嵌套类拿到了真实字段偏移",
                  bool(_rn.get("fields"))
                  and all(f.get("offset") is not None
                          for f in _rn["fields"]),
                  f"字段数={_rn.get('field_count')}")
            check("嵌套类拿到了方法地址（Hook 落点）",
                  bool(_rn.get("methods"))
                  and any(m.get("pointer") for m in _rn["methods"]),
                  f"方法数={_rn.get('method_count')}")

        # (2) 只给短名时必须**拒绝并给候选**，不许静默取第一个
        _short = _nested_row["name"]
        _n_short = _idx9.conn.execute(
            "SELECT COUNT(*) c FROM types WHERE name=?", (_short,)).fetchone()["c"]
        print(f"     静态同名类型数: {_short} → {_n_short} 个")
        if _n_short > 1:
            _ra = S.runtime_class_info(name=_short, namespace="")
            # 用 `.get(...) or []` 而不是 `_ra["candidates"]`：会话万一中途失效，
            # 返回里根本没有 candidates 键，直接下标会抛 KeyError ——
            # 那是**测试脚本崩了**，而不是「断言失败」，两者不能混。
            _cands = _ra.get("candidates") or []
            check("短名歧义时拒绝而不是静默取第一个",
                  _ra.get("ok") is False and _ra.get("ambiguous") is True,
                  f"ok={_ra.get('ok')} ambiguous={_ra.get('ambiguous')} "
                  f"err={str(_ra.get('error'))[:60]}")
            check("拒绝时给出候选数（可操作）",
                  (_ra.get("candidate_count") or 0) > 1,
                  f"candidate_count={_ra.get('candidate_count')}")
            check("候选平铺在顶层（与静态侧拒绝形状一致）",
                  len(_cands) > 0,
                  f"candidates={len(_cands)} 条"
                  f"（err={str(_ra.get('error'))[:40]}）")
            # 候选里的 full_name 必须真能回灌定位（否则等于空头支票）
            _cand0 = _cands[0] if _cands else {}
            if _cand0.get("full_name"):
                _rc2 = S.runtime_class_info(name=_cand0["full_name"],
                                            namespace="")
                check("候选 full_name 回灌即可唯一定位",
                      _rc2.get("ok") is True
                      and _rc2.get("full_name") == _cand0["full_name"],
                      f"回灌 {_cand0['full_name']!r} → ok={_rc2.get('ok')}")
            check("候选有上限（不许把上百条全倒出来）",
                  len(_cands) <= 30,
                  f"返回 {len(_cands)} 条，"
                  f"truncated={_ra.get('candidates_truncated')}")

        # (3) 修嵌套不许把顶层类型弄坏
        _rt = S.runtime_class_info(name="PresetLoaderV2", namespace="")
        check("顶层类型仍然正常（没为修嵌套而回归）",
              _rt.get("ok") is True and _rt.get("full_name") == "PresetLoaderV2",
              f"full_name={_rt.get('full_name')!r}")
    else:
        print("     ⚠️  本样本没有可用的嵌套类，无法验证 —— 如实记录，不算通过")

    # ============================================================ 10. Hook 落点
    show("10. find_hook_target —— 定位落点并比对 token")
    ft = S.find_hook_target(name="UserData_Service",
                            namespace="TeamTapas.ProjectR.UserDatas.Service",
                            method="get_storeStatSync")
    check("find_hook_target 返回 ok", ft.get("ok"), ft.get("error", ""))
    chosen_token = None
    if ft.get("ok"):
        cands = ft.get("candidates") or []
        print(f"     候选 {len(cands)} 个")
        for c in cands[:3]:
            print(f"       token={c.get('token')} rva={c.get('rva_hex')} "
                  f"in_module={c.get('in_module')} ptr={c.get('pointer')}")
        usable = [c for c in cands if c.get("pointer")]
        check("至少一个候选有真实地址", bool(usable))
        if usable:
            chosen_token = usable[0].get("token")
            # 关键交叉验证：运行时 token 必须等于静态元数据的 token
            static_meth = [m for m in (cd.get("methods") or [])
                           if m.get("name") == "get_storeStatSync"]
            st_tok = static_meth[0].get("token") if static_meth else None
            check("运行时 token == 静态 token",
                  str(chosen_token).lower() == str(st_tok).lower(),
                  f"静态={st_tok} 运行时={chosen_token}")

    # ============================================================ 11. Hook 全流程
    show("11. hook_method / drain_events / hook_status / unhook")
    hm = S.hook_method(
        name="Time", method="get_frameCount",
        namespace="UnityEngine", max_calls=500,
        label="verify.frameCount",
    )
    check("hook_method 返回 ok", hm.get("ok"), hm.get("error", ""))
    hook_id = hm.get("hook_id") if hm.get("ok") else None
    if hook_id:
        ch = hm.get("chosen") or {}
        print(f"     hook_id={hook_id} rva={ch.get('rva_hex')} token={ch.get('token')}")
        check("hook 落点带 RVA", bool(ch.get("rva_hex")))

        # 本机无显示环境，游戏不跑渲染帧，故主动调用以证明 Hook 生效
        ev_before = S.drain_events(limit=10)
        check("drain_events 返回 ok", ev_before.get("ok"))

        S.eval_script(
            "(function(){"
            f"  var f = new NativeFunction(ptr('{ch.get('pointer')}'), 'int', []);"
            "  try { f(); } catch(e) { return 'threw:' + e.message; }"
            "  return 'called';"
            "})()"
        )
        time.sleep(0.5)
        ev = S.drain_events(limit=100)
        enters = [e for e in (ev.get("events") or []) if e.get("phase") == "enter"]
        check("捕获到 Hook 事件", len(enters) >= 1,
              f"{len(enters)} 条 — 证明 Hook 在真实游戏里生效")
        if enters:
            print(f"     事件样例: {json.dumps(enters[0], ensure_ascii=False)[:200]}")

        hs = S.hook_status()
        check("hook_status 返回 ok", hs.get("ok"))
        cnt = (hs.get("hooks") or [{}])[0].get("enter_count")
        check("hook_status 计数 > 0", (cnt or 0) > 0, f"enter_count={cnt}")

        uh = S.unhook(hook_id)
        check("unhook 返回 ok", uh.get("ok"), uh.get("error", ""))

        # 卸载后应不再产生新事件
        S.drain_events(limit=1000)
        S.eval_script(
            "(function(){"
            f"  var f = new NativeFunction(ptr('{ch.get('pointer')}'), 'int', []);"
            "  try { f(); } catch(e) { return 'threw'; }"
            "  return 'called';"
            "})()"
        )
        time.sleep(0.4)
        ev_after = S.drain_events(limit=100)
        after_enters = [e for e in (ev_after.get("events") or [])
                        if e.get("phase") == "enter"]
        check("卸载后不再捕获事件", len(after_enters) == 0,
              f"仍有 {len(after_enters)} 条")

    # ============================================================ 11b. 重载选择
    show("11b. hook_method 的重载选择 —— 不许盲选第一个候选")
    # 回归用例：曾经 `usable[0]` 盲取第一个候选，会在不知情时挂到错误重载上。
    #
    # 选 System.String.Concat 当靶子（实测运行时已加载、6 个重载且全部可挂）：
    #   token=0x0600026a param=3 / 0x0600026b param=1 / 0x0600026c param=2
    #   token=0x0600026d param=3 / 0x0600026e param=4 / 0x0600026f param=1
    # 它天然覆盖两种情形：param=4 唯一可区分，param=3 与 param=1 各有 2 个
    # → 正好用来验证「能区分时按参数选、不能区分时报歧义」。
    #
    # 教训：最初选了 BodyPart.Visualize（静态元数据里确实有 2 个重载），
    # 但它**运行时没被加载** → find_hook_target 返回「未找到类」。
    # 选靶子必须挑「运行时确实存在」的类，否则测的是环境不是产品。
    OVL_NS, OVL_CLS, OVL_M = "System", "String", "Concat"
    ovl_static = S.class_detail("System.String", method_name=OVL_M,
                                include_inherited=True)
    ovl_meths = ovl_static.get("methods") or []
    print(f"     静态元数据：{OVL_CLS}.{OVL_M} 有 {len(ovl_meths)} 个重载")
    for m in ovl_meths:
        print(f"       token={m.get('token')} 参数={m.get('parameter_count')}")

    rt_ovl = S.find_hook_target(name=OVL_CLS, method=OVL_M, namespace=OVL_NS)
    if not rt_ovl.get("ok"):
        check("运行时能找到 System.String.Concat", False,
              rt_ovl.get("error", "")[:80], warn_only=True)
    else:
        usable = [c for c in (rt_ovl.get("candidates") or [])
                  if c.get("pointer") and c.get("in_module") is not False]
        toks = [str(c.get("token", "")).lower() for c in usable]
        pcs = [c.get("param_count") for c in usable]
        print(f"     运行时候选 {len(rt_ovl.get('candidates') or [])} 个，"
              f"其中可挂 {len(usable)} 个：{list(zip(toks, pcs))}")

        check("System.String.Concat 运行时确有多重载（本用例前提）",
              len(usable) >= 2, f"可挂候选 {len(usable)} 个")

        if len(usable) >= 2:
            # ① 不给选择依据 + 多个候选 → 必须拒绝，而不是猜一个
            amb = S.hook_method(name=OVL_CLS, method=OVL_M, namespace=OVL_NS,
                                max_calls=5, label="verify.ovl.ambig")
            check("多候选且未指定重载 → 拒绝而非盲挂",
                  amb.get("ok") is False and bool(amb.get("error")),
                  f"ok={amb.get('ok')} error={str(amb.get('error'))[:60]}")
            check("拒绝时给出候选清单供 Agent 决策",
                  len(amb.get("candidates") or []) >= 2,
                  f"candidates={len(amb.get('candidates') or [])} 个")

            # ② 按 token 精确选择 —— 首选方式。故意选**最后一个**，
            #    若实现退回「取第一个」这条断言就会失败。
            want = usable[-1]
            want_tok = want.get("token")
            r1 = S.hook_method(name=OVL_CLS, method=OVL_M, namespace=OVL_NS,
                               token=want_tok, max_calls=5,
                               label="verify.ovl.token")
            got1 = (r1.get("chosen") or {}).get("token", "")
            check("按 token 指定重载 → 挂上的就是它（不是第一个）",
                  r1.get("ok") and str(got1).lower() == str(want_tok).lower(),
                  f"想要 {want_tok} 实际 {got1}")
            if r1.get("ok"):
                # 交叉验证：chosen.token 必须能在静态元数据里找到同名重载
                static_tok = {str(m.get("token", "")).lower() for m in ovl_meths}
                check("chosen.token 在静态元数据中存在（静态/运行时对账）",
                      str(got1).lower() in static_tok,
                      f"{got1} ∈ {sorted(static_tok)}")
                S.unhook(r1.get("hook_id"))

            # ③ 按 param_count 选择：挑一个**参数个数唯一**的候选
            uniq_pc = next((p for p in pcs if pcs.count(p) == 1), None)
            if uniq_pc is not None:
                r2 = S.hook_method(name=OVL_CLS, method=OVL_M, namespace=OVL_NS,
                                   param_count=uniq_pc, max_calls=5,
                                   label="verify.ovl.pc")
                got2 = (r2.get("chosen") or {}).get("token", "")
                ch2 = next((c for c in usable
                            if str(c.get("token", "")).lower() == str(got2).lower()),
                           None)
                check(f"按 param_count={uniq_pc} 指定重载 → 参数个数相符",
                      r2.get("ok") and (ch2 or {}).get("param_count") == uniq_pc,
                      f"chosen={got2} param={(ch2 or {}).get('param_count')}")
                if r2.get("ok"):
                    S.unhook(r2.get("hook_id"))

            # ④ 参数个数有歧义 → 必须报歧义并提示改用 token，不许随便挑
            dup_pc = next((p for p in pcs if pcs.count(p) > 1), None)
            if dup_pc is not None:
                r3 = S.hook_method(name=OVL_CLS, method=OVL_M, namespace=OVL_NS,
                                   param_count=dup_pc, max_calls=5,
                                   label="verify.ovl.dup")
                check(f"param_count={dup_pc} 有多个重载 → 报歧义而非任选",
                      r3.get("ok") is False and "token" in str(r3.get("error")),
                      f"ok={r3.get('ok')} err={str(r3.get('error'))[:60]}")

            # ⑤ 不存在的 token → 明确报错，不许随便挂一个
            r4 = S.hook_method(name=OVL_CLS, method=OVL_M, namespace=OVL_NS,
                               token="0xDEADBEEF", max_calls=5,
                               label="verify.ovl.bad")
            check("不存在的 token 被拒绝", r4.get("ok") is False,
                  str(r4.get("error"))[:70])

    # ⑥ 单一候选不受影响（Time.get_frameCount 无重载，走简单路径）
    r5 = S.hook_method(name="Time", method="get_frameCount",
                       namespace="UnityEngine", max_calls=5,
                       label="verify.ovl.single")
    check("无重载方法仍可直接挂（简单路径未被破坏）",
          r5.get("ok"), r5.get("error", "")[:60])
    if r5.get("ok"):
        S.unhook(r5.get("hook_id"))

    # ============================================ 11c. 参数捕获与返回值类型
    show("11c. args_type 参数捕获 / retval_type 推断（Frida 17 回归）")
    # 回归背景：Frida 17 起 onEnter 的 `args` 是**寄存器视图**，不再暴露
    # `length` —— 读 `args.length` 会抛 `RangeError: invalid array index`。
    # 老代码的判界语句 `if (i >= args.length) break;` 自己就是异常源，于是
    # **任何带 args_type 的 Hook 在 onEnter 就抛异常，整条 enter 事件
    # （含 from / thread / args）全部丢失**，只剩 leave。症状极具误导性：
    # 调用方会以为「这个方法的参数没捕获到」，而不是「探针坏了」。
    #
    # 这里拿 Mathf.Abs(float) 当靶子，一次覆盖三条路径：
    #   · 浮点参数（x64 走 XMM0，根本不在整型寄存器里）
    #   · 浮点返回值（同样在 XMM0；按 ptr 读 RAX 会给出无意义的整数）
    #   · 返回值类型按静态返回类型自动推断
    abs_static = S.class_detail("UnityEngine.Mathf", method_name="Abs")
    abs_f = [m for m in (abs_static.get("methods") or [])
             if str(m.get("return_type")) == "System.Single"]
    check("静态元数据里能看到 Mathf.Abs(float)", bool(abs_f),
          f"共 {len(abs_static.get('methods') or [])} 个重载")
    if abs_f:
        S.drain_events(limit=1000)          # 清空旧事件，避免拿旧事件冒充
        h_abs = S.hook_method(name="Mathf", method="Abs", namespace="UnityEngine",
                              token=abs_f[0].get("token"), args_type="float",
                              max_calls=4, label="verify.args.abs")
        check("挂 Mathf.Abs(float) 成功", h_abs.get("ok"),
              str(h_abs.get("error"))[:60])
        check("返回值类型按静态返回类型自动推断为 float（不用手填）",
              h_abs.get("retval_type") == "float",
              f"retval_type={h_abs.get('retval_type')} "
              f"return_type={h_abs.get('return_type')}")
        if h_abs.get("ok"):
            a_addr = (h_abs.get("chosen") or {}).get("pointer")
            called = S.eval_script(
                "(function(){"
                f"  var f = new NativeFunction(ptr('{a_addr}'), 'float', ['float']);"
                "  return f(-2.5);"
                "})()"
            )
            check("探针之外强制调用 Mathf.Abs(-2.5) 得到 2.5（对照组）",
                  abs(float(called.get("value") or 0) - 2.5) < 1e-6,
                  f"value={called.get('value')}")
            time.sleep(0.5)
            evs = S.drain_events(limit=200).get("events") or []
            e_in = [e for e in evs if e.get("phase") == "enter"]
            e_out = [e for e in evs if e.get("phase") == "leave"]
            e_err = [e for e in evs if e.get("type") == "script_error"]
            check("捕获到 enter 事件（老代码这里一条都没有）",
                  bool(e_in), f"enter={len(e_in)} leave={len(e_out)}")
            got_args = ((e_in[0].get("args") if e_in else None) or [])
            check("enter 的 args[0] == -2.5（浮点参数从 XMM0 取）",
                  bool(got_args) and abs(float(got_args[0]) + 2.5) < 1e-6,
                  f"args={got_args}")
            got_rv = e_out[0].get("retval") if e_out else None
            check("leave 的 retval == 2.5（不再按 ptr 读 RAX）",
                  got_rv is not None and abs(float(got_rv) - 2.5) < 1e-6,
                  f"retval={got_rv}")
            check("onEnter 不再抛异常（无 script_error 事件）",
                  not e_err,
                  f"{[str(e.get('description'))[:40] for e in e_err][:2]}")
            S.unhook(h_abs.get("hook_id"))

    # 规格名写错必须报错：以前静默落到 `default: raw.toString()`，
    # 于是 `flaot` 会被当成指针读一个整型寄存器，给出看似合理的错值。
    bad_spec = S.hook_method(name="Mathf", method="Abs", namespace="UnityEngine",
                             token=(abs_f[0].get("token") if abs_f else ""),
                             args_type="flaot", max_calls=2,
                             label="verify.args.bad")
    check("args_type 写错规格名 → 明确报错（不静默降级）",
          bad_spec.get("ok") is False and "flaot" in str(bad_spec.get("error")),
          str(bad_spec.get("error"))[:70])
    bad_rv = S.hook_address(address="0x1000", args_type="int",
                            retval_type="flaot", max_calls=1)
    check("retval_type 写错规格名 → 明确报错",
          bad_rv.get("ok") is False and "flaot" in str(bad_rv.get("error")),
          str(bad_rv.get("error"))[:70])

    # 实例方法的第 1 个整型位置就是 this —— 这是「拿对象指针去读字段」的正路，
    # 也是 read_field_value 唯一能拿到 instance 的来源（静态字段读不了，
    # 见 read_field_value 的 instance_only 约束）。
    # 这条同时覆盖「自然调用」（不靠强制调用）与「args[0] 真是对象」。
    S.drain_events(limit=1000)
    h_self = S.hook_method(name="Object", method="get_name",
                           namespace="UnityEngine", args_type="ptr",
                           max_calls=8, label="verify.args.this")
    check("挂 UnityEngine.Object.get_name（实例方法）成功", h_self.get("ok"),
          str(h_self.get("error"))[:60])
    if h_self.get("ok"):
        this_ptr, waited = None, 0.0
        while waited < 25.0 and not this_ptr:
            time.sleep(1.0)
            waited += 1.0
            for e in (S.drain_events(limit=200).get("events") or []):
                if e.get("id") == h_self.get("hook_id") and e.get("phase") == "enter":
                    a = e.get("args") or []
                    if a:
                        this_ptr = a[0]
                        break
        check(f"实例方法的 enter 事件带 args[0]=this（自然调用，等了 {waited:.0f}s）",
              bool(this_ptr), f"this={this_ptr}")
        if this_ptr:
            # 用这个 this 真读一个字段：args[0] 若不是一个真对象，这里必然失败
            cd_obj = S.class_detail("UnityEngine.Object")
            flds = [f.get("name") for f in (cd_obj.get("fields") or [])][:3]
            ok_field, tried = None, []
            for fn in flds:
                rv = S.read_field_value(instance=this_ptr, name="Object",
                                        field=fn, namespace="UnityEngine")
                tried.append(f"{fn}={'ok' if rv.get('ok') else str(rv.get('error'))[:24]}")
                if rv.get("ok"):
                    ok_field = (fn, rv)
                    break
            check("read_field_value 能读 this 的实例字段（args[0] 确实是对象）",
                  ok_field is not None, "；".join(tried) or "该类没有字段")

        # ---- find_instances：**不靠 Hook** 直接枚举活实例
        #
        # 这是「最后一公里」的关键能力：在此之前，拿 instance 指针的唯一
        # 办法就是上面那段「Hook 一个恰好会经过该对象的函数、等它自然调用」
        # —— 碰运气。现在可以直接问运行时。
        #
        # 判据分三层，逐层加强：
        #   1. 工具返回 ok 且 count>0（拿到了东西）
        #   2. **独立读对象头**解出类名，与工具报的 class_name 一致
        #      （证明指针真的是那个类的实例，不是野指针）
        #   3. 派生类语义可核查：exact + derived == count
        print()
        print("     find_instances —— 直接枚举活对象实例：")
        fi = S.find_instances(name="Transform", namespace="UnityEngine",
                              max_count=5)
        check("find_instances 返回 ok", fi.get("ok") is True,
              str(fi.get("error"))[:100])
        if fi.get("ok"):
            print(f"       class={fi.get('class')} count={fi.get('count')} "
                  f"overflow={fi.get('overflow')}")
            print(f"       class_name_counts={fi.get('class_name_counts')} "
                  f"exact={fi.get('exact_class_count')} "
                  f"derived={fi.get('derived_count')}")
            check("拿到非空实例列表", (fi.get("count") or 0) > 0,
                  f"count={fi.get('count')}")
            check("count 不超过 max_count", (fi.get("count") or 0) <= 5,
                  f"count={fi.get('count')} max_count=5")
            check("exact_class_count + derived_count == count",
                  (fi.get("exact_class_count") or 0)
                  + (fi.get("derived_count") or 0) == fi.get("count"),
                  f"{fi.get('exact_class_count')}+{fi.get('derived_count')}"
                  f" vs {fi.get('count')}")

            insts = fi.get("instances") or []
            check("instances 每项都带 class_name",
                  bool(insts) and all(x.get("class_name") for x in insts),
                  f"前 2 项={insts[:2]}")
            # 第 2 层：独立复核指针有效 —— 读对象头，解出类名，与工具报的对照
            if insts:
                head = S.read_memory(address=insts[0]["pointer"], size=8)
                kaddr = None
                h = (head.get("hex") or "") if head.get("ok") else ""
                if len(h) >= 16:
                    kaddr = hex(int.from_bytes(bytes.fromhex(h[:16]), "little"))
                check("实例指针可读（不是野指针）", kaddr is not None,
                      f"read_memory ok={head.get('ok')}")
                if kaddr:
                    nm = S.eval_script(
                        "(function(){"
                        f" var k = ptr('{kaddr}');"
                        "  try { return String(funcs.il2cpp_class_get_name(k)"
                        ".readUtf8String()); } catch(e) { return 'ERR'; }"
                        "})()"
                    ).get("value")
                    check("对象头解出的类名 == instances[0].class_name"
                          "（证明这确实是该类的实例）",
                          nm == insts[0].get("class_name"),
                          f"内存={nm!r} 工具={insts[0].get('class_name')!r}")
        # 值类型必须明确报错，不许用空列表冒充「没有实例」
        vt = S.find_instances(name="Vector3", namespace="UnityEngine")
        check("值类型如实报错（不返回空列表冒充「没有实例」）",
              vt.get("ok") is False and "值类型" in str(vt.get("error", "")),
              f"ok={vt.get('ok')} err={str(vt.get('error'))[:60]}")
        if ok_field:
            _fr = ok_field[1]
            print(f"       {ok_field[0]} = {_fr.get('raw_hex')} "
                  f"（宽度 {_fr.get('read_width')} 字节，"
                  f"地址 {_fr.get('field_address')}，"
                  f"as_pointer={_fr.get('as_pointer')}）")
        S.unhook(h_self.get("hook_id"))

    # ============================================================ 12. 内存与扫描
    show("12. read_memory / write_memory / scan_pattern / eval_script")
    # 用持久缓冲区做读写往返（避免 GC 干扰）
    import gamemod_mcp.engine.state as _st
    buf = _st.state().runtime.alloc_buffer(16)
    check("alloc_buffer 返回 ok", buf.get("ok"), str(buf)[:60])
    if buf.get("ok"):
        addr = buf.get("address")
        w = S.write_memory(addr, "deadbeefcafebabe")
        check("write_memory 返回 ok", w.get("ok"), w.get("error", ""))
        r = S.read_memory(addr, 8)
        check("read_memory 往返一致",
              (r.get("hex") or "").lower() == "deadbeefcafebabe",
              f"写入 deadbeefcafebabe 读回 {r.get('hex')}")
        _st.state().runtime.free_buffer(addr)

    bad = S.read_memory("0x1", 8)
    check("无效地址被正确拒绝", bad.get("ok") is False, bad.get("error", "")[:40])

    # 0x0 单独要验：Frida 的 `readByteArray` 对**不可读地址返回 null**（不像
    # `readU8` 会抛异常），而 `new Uint8Array(null)` 得到长度 0 的数组 ——
    # 老写法于是把「读不到」报成 `{ok: true, size: 4, hex: ""}`：**假成功**。
    # 实测 `read_memory("0x0", 4)` 就命中过这条（多游戏矩阵里读模块头时
    # 拿到空 hex 却 ok=true）。0x1 走的是抛异常那条路，覆盖不到它。
    zero = S.read_memory("0x0", 4)
    check("空指针 0x0 也必须 ok=false（不许拿空 hex 冒充成功）",
          zero.get("ok") is False and not zero.get("hex"),
          f"ok={zero.get('ok')} hex={zero.get('hex')!r} "
          f"error={str(zero.get('error'))[:60]}")

    # 护栏：写已被 Hook 的函数入口实测会让游戏数秒内崩在 0xc0000005
    # （Windows 事件日志里就是该方法 RVA 处的访问违例），所以默认必须拦住。
    # 这里**只验拒绝**，不验 force=True 的放行 —— 那一下真的会崩游戏，
    # 放行路径的判定逻辑由 verify_server_helpers.py 的纯函数用例覆盖。
    h_guard = S.hook_method(name="Time", method="get_frameCount",
                            namespace="UnityEngine", max_calls=2,
                            label="verify.mem.guard")
    check("为内存护栏准备一个 Hook", h_guard.get("ok"),
          str(h_guard.get("error"))[:60])
    if h_guard.get("ok"):
        g_tgt = h_guard.get("target") or (h_guard.get("chosen") or {}).get("pointer")
        g_bytes = S.read_memory(g_tgt, 8).get("hex") or "90"
        blocked = S.write_memory(g_tgt, g_bytes)
        check("write_memory 拒绝写已 Hook 的函数入口（实测会崩游戏）",
              blocked.get("ok") is False and "Hook" in str(blocked.get("error")),
              str(blocked.get("error"))[:80])
        check("拒绝时给出是哪个 Hook 被撞上",
              (blocked.get("hook") or {}).get("hook_id") == h_guard.get("hook_id"),
              f"hook={blocked.get('hook')}")
        # 卸载之后同一地址就能写了（护栏不是按地址永久拉黑）。
        # 注意：必须**重新读**一次字节 —— Hook 期间读到的是 Frida 写的跳转，
        # 卸载后它已被还原成原序言，写回旧字节等于把跳转又贴回去。
        S.unhook(h_guard.get("hook_id"))
        g_now = S.read_memory(g_tgt, 8).get("hex") or "90"
        after_unhook = S.write_memory(g_tgt, g_now)
        check("unhook 之后同一地址可以正常写（护栏不是永久拉黑）",
              after_unhook.get("ok") is True,
              f"写回原字节 {g_now}：{str(after_unhook.get('error'))[:50]}")
        check("卸载前后同一入口的字节确实不同（说明读到的真是跳转）",
              g_now.lower() != g_bytes.lower(),
              f"Hook 中 {g_bytes} → 卸载后 {g_now}")

    sp = S.scan_pattern("4D 5A", max_results=8)
    check("scan_pattern 返回 ok", sp.get("ok"), sp.get("error", ""))
    if sp.get("ok"):
        # 探针返回的字段名是 matched（不是 count）——
        # 注意 IL2CPP 模块内的可读段未必从 PE 头开始，
        # 命中的是模块内出现的 MZ 字节序列，数量因模块而异。
        n = sp.get("matched")
        check("AOB 扫描返回命中数（字段名 matched）", n is not None,
              f"matched={n} 扫描段数={sp.get('scanned_ranges')} "
              f"模块={sp.get('scanned_module')}")

    es = S.eval_script("Process.arch")
    check("eval_script 可用", es.get("ok"), str(es.get("value"))[:40])

    # ============================================================ 13. 错误路径
    show("13. 错误处理 —— 失败必须显式报错，不能假装成功")
    # 未注入时调运行时工具（用一个全新会话不好构造，改为验证参数校验）
    nf = S.class_detail("这个类根本不存在XyzAbc")
    check("不存在的类被正确拒绝", nf.get("ok") is False, nf.get("error", "")[:50])
    nb = S.find_hook_target(name="UserData_Service", method="完全不存在的方法Xyz")
    check("不存在的方法被正确拒绝", nb.get("ok") is False, nb.get("error", "")[:50])
    badh = S.read_query_result("伪造句柄")
    check("伪造句柄被正确拒绝", badh.get("ok") is False)

    # ============================================================ 14. 收尾
    show("14. close_session —— 收尾并还原游戏状态")
    cs = S.close_session()
    check("close_session 返回 ok", cs.get("ok"), cs.get("error", ""))
    check("close_session 如实给出 restored", "restored" in cs,
          f"restored={cs.get('restored')}")
    check("close_session 回报索引已关闭",
          (cs.get("index") or {}).get("ok") is True,
          f"index={cs.get('index')}")

# ---- 假成功回归：close 失败时不许报 restored:True ----
# 审计发现的最危险一条：早期 RuntimeSession.close() 把卸载 Hook / unload /
# detach 三步各包一个 `except Exception: pass`，然后无条件返回 restored:True。
# 实测三步全抛异常时依然报成功 —— 而 close_session 的语义就是「游戏已恢复原状」，
# Agent 会带着仍被篡改的进程收工。这里用桩对象复现，不依赖真的注入。
show("14b. teardown 不许假成功（桩对象复现）")

from gamemod_mcp.runtime import session as _sess_mod   # noqa: E402


def _stub_session(pid: int) -> Any:
    s = _sess_mod.RuntimeSession.__new__(_sess_mod.RuntimeSession)
    s.pid = pid
    s.name = "stub"
    s._alive = True
    return s


class _Boom:
    """任何属性访问都返回一个必抛异常的 callable。"""
    def __getattr__(self, _n):
        def _f(*_a, **_k):
            raise RuntimeError(f"{_n} 被拒绝（模拟）")
        return _f


_s_all_bad = _stub_session(99999)
_s_all_bad._script = _Boom()
_s_all_bad._session = _Boom()
_s_all_bad.hook_status = lambda: (_ for _ in ()).throw(
    RuntimeError("hook_status 被拒绝（模拟）"))
_s_all_bad.unhook_all = lambda: (_ for _ in ()).throw(
    RuntimeError("unhook 被拒绝（模拟）"))
_r_bad = _s_all_bad.close()
print(f"     三步全失败 -> ok={_r_bad.get('ok')} "
      f"restored={_r_bad.get('restored')} steps={_r_bad.get('steps')}")
check("三步全失败时 ok 为 False（不许假成功）",
      _r_bad.get("ok") is False, f"ok={_r_bad.get('ok')}")
check("三步全失败时 restored 为 False",
      _r_bad.get("restored") is False)
check("失败时给出 error", bool(_r_bad.get("error")))
check("失败时逐步如实标注哪一步没成",
      _r_bad.get("steps") == {"hooks_removed": False,
                              "script_unloaded": False,
                              "detached": False},
      f"steps={_r_bad.get('steps')}")
check("失败时会话保持可重试（_alive 仍为 True）",
      _s_all_bad._alive is True)

_s_all_ok = _stub_session(99998)
_s_all_ok.hook_status = lambda: {"count": 3}
_s_all_ok.unhook_all = lambda: None
_s_all_ok._script = type("X", (), {"unload": lambda self: None})()
_s_all_ok._session = type("Y", (), {"detach": lambda self: None})()
_r_ok = _s_all_ok.close()
check("全部成功时才报 restored:True",
      _r_ok.get("ok") is True and _r_ok.get("restored") is True
      and _r_ok.get("hooks_removed") == 3,
      f"ok={_r_ok.get('ok')} restored={_r_ok.get('restored')}")

# close_session 顶层 ok 必须反映子步骤失败（以前埋进 runtime 子对象里）
_st_now = state.state()


class _BadRT:
    def close(self):
        raise RuntimeError("detach 被拒绝（模拟）")


_st_now.runtime = _BadRT()
_cs_bad = S.close_session(close_runtime=True, close_index=False)
print(f"     子步骤失败 -> 顶层 ok={_cs_bad.get('ok')} "
      f"error={str(_cs_bad.get('error'))[:40]}")
check("子步骤失败时顶层 ok 为 False",
      _cs_bad.get("ok") is False, f"ok={_cs_bad.get('ok')}")
check("子步骤失败时顶层带 error", bool(_cs_bad.get("error")))
check("子步骤失败时 restored 为 False",
      _cs_bad.get("restored") is False)
check("关闭失败时保留 runtime 以便重试", _st_now.runtime is not None)
_st_now.runtime = None

# ---- 同名进程已在跑：必须**当场**拦住，不许白等 60 秒 ----
# 实测踩到过：Unity 的单实例锁会让第二个进程不加载 GameAssembly.dll，
# 于是 `launch_game` 轮询 60 秒后报「IL2CPP 运行时未就绪」—— 看起来像探针
# 坏了，实际是「已经有一个同名进程在跑」。反证：上面第 7 节在**没有**同名
# 进程时 launch 成功，说明这条拦截不是无差别拒绝。
show("15. launch_game —— 同名进程已在跑时当场拦住")
if pid:
    _dup = S.launch_game(exe=GAME_EXE)
    print(f"     第二次 launch_game -> ok={_dup.get('ok')} "
          f"running_pid={_dup.get('running_pid')} "
          f"error={str(_dup.get('error'))[:70]}")
    check("同名进程已在跑时返回 ok:False（不是白等超时）",
          _dup.get("ok") is False, str(_dup.get("error"))[:90])
    check("指出是哪个 pid 在跑", _dup.get("running_pid") == pid,
          f"running_pid={_dup.get('running_pid')} 期望={pid}")
    check("给出两条出路（注入已有进程 / 先结束它）",
          "inject_process" in str(_dup.get("hint"))
          and "taskkill" in str(_dup.get("hint")),
          str(_dup.get("hint"))[:110])
else:
    check("能验证同名进程拦截（需要先成功 launch 一次）", False,
          "第 7 节没拿到 pid", warn_only=True)

# ================================================================ 结果
show("端到端结果汇总（通过 MCP 工具层）")
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项 / 警告 {len(WARN)} 项")
if FAIL:
    print("\n  ❌ 失败项：")
    for f_ in FAIL:
        print(f"     - {f_}")
if WARN:
    print("\n  ⚠️  警告项（多为子模块尚未完成或环境限制）：")
    for w in WARN:
        print(f"     - {w}")
if not FAIL:
    print("\n  ★ 通过 MCP 工具层的端到端流程全部走通。")

# 清理 spawn 出来的游戏
if pid:
    print()
    print("  清理：结束游戏进程树…")
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=20)
        print("  已结束")
    except Exception as exc:
        print(f"  结束失败，请手动检查: {exc}")
