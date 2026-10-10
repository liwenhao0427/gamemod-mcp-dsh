# 索引层验收：构建 SQLite 索引并用真实游戏数据跑通全部查询
import sys, io, time, shutil
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
# 仓库路径由 samples.py 推导（不许硬编码工作区绝对路径）
# 用 os.path 推导，避免依赖后面才 import 的 pathlib
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import samples  # noqa: E402
samples.add_src_to_path()

from gamemod_mcp.engine.index import Index, default_cache_dir

# 样本路径：根目录从 samples.py 取，派生路径用相对路径拼出来
_GAME = samples.find_il2cpp()
META = (_GAME / "Garden of Witches_Data" / "il2cpp_data"
        / "Metadata" / "global-metadata.dat") if _GAME else None
# 用**本脚本专属**的子目录，不要清空共享的 .cache-test。
#
# 历史教训：这里原本是 `shutil.rmtree(samples.CACHE_TEST)`，
# 而 CACHE_TEST 是所有脚本共用的目录 —— 后果有二：
#   1. 会删掉别的脚本放在那里的文件（实测删掉过一个待用的脚本）；
#   2. 动态覆盖率的日志 `.cache-test/tool_calls.jsonl` 也在里面，
#      本脚本一跑就把**之前**的记录清空了。当前恰好没有「只被
#      更早脚本覆盖」的工具，所以 39/39 仍能通过，把问题掩盖了 ——
#      但只要有那样一个工具，就会变成「明明跑过却报未覆盖」的假红。
# 清自己的临时目录同样能达到「强制走完整构建」的目的。
CACHE = str(samples.CACHE_TEST / "verify_index")

# 样本不可用时记为「无法验证」并跳过（跳过不算失败）
if not samples.require(META, "IL2CPP 元数据 Garden of Witches"):
    sys.exit(0)

# 清掉旧缓存，强制走完整构建路径（只清自己的目录）
shutil.rmtree(CACHE, ignore_errors=True)

print("=" * 74)
print("1. 首次构建索引（含 sha256 + 全量解析 + 建表）")
print("=" * 74)
t0 = time.time()
idx = Index.open(META, cache_dir=CACHE)
t1 = time.time()
print(f"  构建耗时 {t1-t0:.2f} 秒")
print(f"  数据库 {idx.db_path.stat().st_size:,} 字节")
idx.close()

print()
print("=" * 74)
print("2. 二次打开（应命中缓存，秒开）")
print("=" * 74)
t0 = time.time()
idx = Index.open(META, cache_dir=CACHE)
t1 = time.time()
print(f"  打开耗时 {t1-t0:.2f} 秒")

info = idx.index_info()
print(f"  概况: 类型={info['type_count']} 方法={info['method_count']} "
      f"字段={info['field_count']} 枚举={info['enum_count']} 程序集={info['image_count']}")

print()
print("=" * 74)
print("3. search_classes：找玩家相关类")
print("=" * 74)
r = idx.search_classes("Player", limit=6)
print(f"  命中 {r['total']:,} 个，返回 {r['returned']} 个，句柄={r['handle']}")
for it in r["items"]:
    print(f"    {it['full_name']:<58} [{it['image']}] 字段{it['field_count']} 方法{it['method_count']}")

print()
print("=" * 74)
print("4. search_classes：只找枚举（游戏常量表）")
print("=" * 74)
r = idx.search_classes("Currency", only_enums=True, limit=6)
print(f"  命中 {r['total']} 个")
for it in r["items"]:
    print(f"    {it['full_name']}  is_enum={it['is_enum']}  token={it['token']}")

print()
print("=" * 74)
print("5. search_classes：按基类过滤（MonoBehaviour 子类）")
print("=" * 74)
r = idx.search_classes("", subtype_of="UnityEngine.MonoBehaviour", limit=6)
print(f"  命中 {r['total']:,} 个")
for it in r["items"]:
    print(f"    {it['full_name']}")

print()
print("=" * 74)
print("6. class_detail：UserData_Service 完整信息")
print("=" * 74)
d = idx.class_detail("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
print(f"  类={d['full_name']}")
print(f"  程序集={d['image']}  基类={d['parent']}  token={d['token']}")
print(f"  字段 {d['field_count']} 个，前 3: {[x['name'] for x in d['fields'][:3]]}")
print(f"  方法 {d['method_count']} 个，前 3: {[x['name'] for x in d['methods'][:3]]}")
print(f"  基类链={d['base_chain']}")
print(f"  子类数={d['derived_count']}")
m0 = d["methods"][0]
print(f"  首方法详情: {m0['name']} token={m0['token']} "
      f"返回={m0['return_type']} 参数={m0['parameters']} 静态={m0['is_static']}")

print()
print("=" * 74)
print("7. class_detail(include_inherited=True)：含继承成员")
print("=" * 74)
d2 = idx.class_detail("TeamTapas.ProjectR.UserDatas.Service.UserData_Service",
                      include_inherited=True)
print(f"  自身成员 字段{d['field_count']}/方法{d['method_count']} → "
      f"含继承后 字段{d2['field_count']}/方法{d2['method_count']}")

print()
print("=" * 74)
print("8. find_derived_classes：React 类")
print("=" * 74)
r = idx.find_derived_classes("UnityEngine.MonoBehaviour", limit=5)
print(f"  MonoBehaviour 共 {r['total']:,} 个子类，前 5:")
for it in r["items"][:5]:
    print(f"    {it['full_name']}")

print()
print("=" * 74)
print("9. find_derived_classes(assembly=)：游戏自己的脚本")
print("=" * 74)
# find_monobehaviours 已并入本方法：assembly 参数比原来「按程序集名
# 前缀猜哪些是引擎自带的」更明确可控。
r = idx.find_derived_classes("UnityEngine.MonoBehaviour",
                             assembly="Assembly-CSharp", limit=8)
print(f"  限 Assembly-CSharp 共 {r['total']:,} 个游戏脚本，前 8:")
for it in r["items"][:8]:
    print(f"    {it['full_name']:<56} [{it['image']}]")

print()
print("=" * 74)
print("10. search_methods / search_fields")
print("=" * 74)
r = idx.search_methods("SetCurrency", limit=5)
print(f"  search_methods('SetCurrency') 命中 {r['total']}:")
for it in r["items"]:
    print(f"    {it['owner']}.{it['name']}  返回={it['return_type']} token={it['token']}")
r = idx.search_fields("currency", limit=5)
print(f"  search_fields('currency') 命中 {r['total']}:")
for it in r["items"]:
    print(f"    {it['owner']}.{it['name']}  类型={it['field_type']}")

print()
print("=" * 74)
print("11. 结果句柄翻页 read_query_result")
print("=" * 74)
r = idx.search_classes("Service", limit=5)
print(f"  句柄={r['handle']} 总数={r['total']}")
p1 = idx.read_query_result(r["handle"], offset=0, limit=3)
p2 = idx.read_query_result(r["handle"], offset=3, limit=3)
print(f"    第1页: {[x['name'] for x in p1['items']]} has_more={p1['has_more']}")
print(f"    第2页: {[x['name'] for x in p2['items']]} has_more={p2['has_more']}")

print()
print("=" * 74)
print("12. read_query_result(pattern=...)：结果内再过滤")
print("=" * 74)
# 原 search_query_result 已按契约第 3 条合并进 read_query_result 的 pattern 参数
s = idx.read_query_result(r["handle"], limit=5, pattern="UserData")
print(f"  在 {r['total']} 条结果里筛 'UserData' → {s['total']} 条")
for it in s["items"][:5]:
    print(f"    {it['full_name']}")

print()
print("=" * 74)
print("13. analyze_dependency")
print("=" * 74)
dep = idx.analyze_dependency("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
print(f"  基类={dep['parent']}  引用了 {dep['referenced_count']} 个类型")
for x in dep["referenced_types"][:8]:
    print(f"    {x['type']}  x{x['count']}")

print()
print("=" * 74)
print("14. class_hierarchy")
print("=" * 74)
h = idx.class_hierarchy("TeamTapas.ProjectR.Domain.CurrencyKey", direction="both", depth=3)
print(f"  向上: {h.get('up')}")
print(f"  向下: {h.get('down_count', 0)} 个")

print()
print("=" * 74)
print("15. class_detail + 按方法名收窄（原 get_method_source 的能力）")
print("=" * 74)
# get_method_source 已并入 class_detail 的 method_name 参数：
# 它原本就是「class_detail 结果里按名字过滤 methods」，独立成工具
# 只会让 Agent 犹豫该调哪个。
_cls = "TeamTapas.ProjectR.UserDatas.Service.UserData_Service"
det = idx.class_detail(_cls, include_inherited=True)
hits = [x for x in det.get("methods", []) if x["name"] == ".ctor"]
print(f"  {_cls} 共 {det.get('method_count')} 个方法，"
      f"其中 .ctor {len(hits)} 个重载：")
for x in hits[:5]:
    print(f"    {x['name']} token={x['token']} "
          f"参数数={x.get('parameter_count')} 参数={x.get('parameters')}")

idx.close()
print()
print("✅ 索引层全部查询通过")
