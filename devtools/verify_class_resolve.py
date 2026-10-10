# 验证运行时「类解析」的三条规则（短名 / 命名空间 / 嵌套名）
#
# 背景：多游戏矩阵跑出来的两个真实缺陷（都在 `agent.js` 的 resolveClass 上）：
#
#   ① 老写法 `e.namespace === (namespace||'') && e.name === name` 在
#      `namespace` 为空时，会把**嵌套类型**也算命中 —— 而嵌套类型的 namespace
#      本来就是空的。实测 RIP_nz 上 `runtime_class_info(name="Transform")`
#      **静默返回**了 `UnityEngine.RuleTile.TilingRuleOutput.Transform`
#      这个嵌套**枚举**（返回里 is_enum=true、字段是 Fixed/Rotated/MirrorX），
#      调用方完全看不出自己拿错了类。
#
#   ② 反过来，带命名空间的顶层类型在不给 namespace 时又**找不到**：
#      实测 Meta Ghost / Pass The Fear / PathOfIdle / Wanderburg 上
#      `runtime_class_info(name="Transform")` 报「未找到类 Transform」，
#      而工具文档写着 namespace「可选」。
#
# 修好之后应当满足：
#   · 只给短名 → 按**顶层**类型解析，唯一才采用，多个则 ambiguous + candidates
#   · 嵌套类型必须用完整嵌套名，短名不会把它兜出来
#   · 显式 namespace / 完整名 / 短名唯一 三条路都指向同一个类
import sys, io, os, json, subprocess
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples  # noqa: E402
samples.add_src_to_path()

from pathlib import Path  # noqa: E402

from gamemod_mcp.runtime import spawn_and_attach, close_all_sessions  # noqa: E402

FAILS: list[str] = []


def show(title):
    print()
    print("=" * 76)
    print(title)
    print("=" * 76)


def check(label, ok, detail=""):
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)
    return ok


def sig(res):
    """取「同一个类」的判定依据：完整名 + 映像 + 字段偏移表。"""
    if not res.get("ok"):
        return None
    return (res.get("full_name"), res.get("image"),
            tuple(sorted((f.get("name"), f.get("offset"))
                         for f in (res.get("fields") or []))))


game = samples.find_il2cpp()
if game is None:
    print("⚠️ 无法验证：samples.find_il2cpp() 找不到 IL2CPP 样本游戏")
    sys.exit(0)
exe = samples.game_exe(game)

show("0. 启动并注入样本")
print(f"  样本: {game}")
sess = None
try:
    sess = spawn_and_attach(str(exe), cwd=str(Path(exe).parent), init=False)
    ready = sess.wait_ready(timeout=60.0)
    print(f"  pid={sess.pid} init.ok={ready.get('ok')} "
          f"attempts={ready.get('attempts')} fallbacks={ready.get('fallbacks')}")
    if not ready.get("ok"):
        print(f"  ❌ 探针未就绪：{ready.get('error')}")
        sys.exit(1)

    # ---------------------------------------------------------- 1. 短名解析
    show("1. 只给短名：必须解析到**顶层**类型，且不能是嵌套枚举")
    short = sess.class_info(name="Transform")
    print(f"  class_info(name='Transform') -> ok={short.get('ok')} "
          f"full_name={short.get('full_name')} image={short.get('image')} "
          f"is_enum={short.get('is_enum')} 字段数={len(short.get('fields') or [])}")
    if short.get("ambiguous"):
        print(f"    歧义（{short.get('candidate_count')} 个候选）："
              f"{json.dumps(short.get('candidates'), ensure_ascii=False)[:200]}")
    check("只给短名能解析成功（不是「未找到类」）", bool(short.get("ok")),
          str(short.get("error"))[:120])
    check("解析到的是 UnityEngine.Transform",
          short.get("full_name") == "UnityEngine.Transform",
          f"实际 {short.get('full_name')}")
    # 嵌套类型的完整名会多一层（如 UnityEngine.RuleTile.TilingRuleOutput.Transform），
    # 顶层类型只有一个点 —— 用点数把「兜到了嵌套类型」这件事卡死。
    check("**不是**嵌套类型（完整名只有一个点）",
          str(short.get("full_name") or "").count(".") == 1,
          f"full_name={short.get('full_name')}")
    check("不是枚举（RIP_nz 上的错解是嵌套枚举，is_enum=true）",
          short.get("is_enum") is False, f"is_enum={short.get('is_enum')}")

    # ---------------------------------------------------------- 2. 三条路一致
    show("2. 短名 / 显式 namespace / 完整名 必须指向同一个类")
    by_ns = sess.class_info(name="Transform", namespace="UnityEngine")
    by_full = sess.class_info(name="UnityEngine.Transform")
    print(f"  namespace='UnityEngine'  -> {by_ns.get('full_name')} "
          f"ok={by_ns.get('ok')} 字段数={len(by_ns.get('fields') or [])}")
    print(f"  name='UnityEngine.Transform' -> {by_full.get('full_name')} "
          f"ok={by_full.get('ok')} 字段数={len(by_full.get('fields') or [])}")
    s1, s2, s3 = sig(short), sig(by_ns), sig(by_full)
    check("显式 namespace 也能解析", bool(by_ns.get("ok")),
          str(by_ns.get("error"))[:120])
    check("完整名也能解析", bool(by_full.get("ok")),
          str(by_full.get("error"))[:120])
    check("三条路的完整名相同",
          s1 and s2 and s3 and s1[0] == s2[0] == s3[0],
          f"{s1 and s1[0]} / {s2 and s2[0]} / {s3 and s3[0]}")
    check("三条路的字段偏移表逐条相同（不是同名不同类）",
          s1 == s2 == s3,
          "" if s1 == s2 == s3 else "偏移表不一致")

    # ---------------------------------------------------------- 3. 嵌套类型
    show("3. 嵌套类型：完整嵌套名能查到，短名不许把它兜出来")
    for probe in ("RuleTile.TilingRuleOutput.Transform", "State"):
        r = sess.class_info(name=probe)
        print(f"  class_info(name='{probe}') -> ok={r.get('ok')} "
              f"full_name={r.get('full_name')} ambiguous={r.get('ambiguous')} "
              f"candidates={r.get('candidate_count')}")
    # 关键断言：短名 "State" 若命中多个嵌套类型，必须报歧义而不是随便挑
    st = sess.class_info(name="State")
    print(f"  短名 'State': ok={st.get('ok')} ambiguous={st.get('ambiguous')} "
          f"candidate_count={st.get('candidate_count')} "
          f"full_name={st.get('full_name')}")
    if st.get("ambiguous"):
        cands = st.get("candidates") or []
        print(f"    候选样例: {json.dumps(cands[:3], ensure_ascii=False)}")
        check("歧义时给出了候选（不让 Agent 猜）", len(cands) > 0)
        check("歧义时没有返回任何类的字段（没偷偷挑一个）",
              not st.get("fields"))
    else:
        print("    该样本里 'State' 不歧义（顶层唯一或不存在）—— "
              "如实记录，不当作通过")

    # ---------------------------------------------------------- 4. 不存在的类
    show("4. 不存在的类：要给出「怎么办」，而不是干巴巴一句未找到")
    nf = sess.class_info(name="NoSuchClassXyzzy123")
    print(f"  ok={nf.get('ok')} error={str(nf.get('error'))[:90]}")
    print(f"  detail={json.dumps(nf.get('detail'), ensure_ascii=False)[:220]}")
    check("不存在的类如实报失败", nf.get("ok") is False)
    hint = str((nf.get("detail") or {}).get("hint", ""))
    check("失败里带 namespace/完整名的提示（老版本只有「未找到类 X」）",
          "namespace" in hint, hint[:100])

    # ---------------------------------------------------------- 5. 反证
    show("5. 非空转反证：同一个嵌套类，短名查不到、完整嵌套名才查得到")
    # 先在本样本里**找一个真实存在的嵌套类型**，且它的短名在顶层类型里没人用
    # —— 这样「短名查不到」就不是因为撞名歧义，而是因为规则确实排除了嵌套。
    cand = sess.eval_js("""
        (function(){
          try {
            var domain = funcs.il2cpp_domain_get();
            var cnt = Memory.alloc(Process.pointerSize);
            cnt.writeU32(0);
            var asms = funcs.il2cpp_domain_get_assemblies(domain, cnt);
            var n = cnt.readU32();
            var nested = [], top = {};
            for (var i = 0; i < n; i++) {
              var asm = asms.add(i * Process.pointerSize).readPointer();
              if (asm.isNull()) { continue; }
              var img = funcs.il2cpp_assembly_get_image(asm);
              if (!img || img.isNull()) { continue; }
              var cc = funcs.il2cpp_image_get_class_count(img).toInt32();
              for (var j = 0; j < cc; j++) {
                var k = funcs.il2cpp_image_get_class(img, j);
                if (!k || k.isNull()) { continue; }
                var nm = funcs.il2cpp_class_get_name(k);
                if (!nm || nm.isNull()) { continue; }
                var s = nm.readUtf8String();
                var outer = funcs.il2cpp_class_get_declaring_type
                    ? funcs.il2cpp_class_get_declaring_type(k) : null;
                var isNested = !!(outer && !outer.isNull());
                if (!isNested) { top[s] = 1; continue; }
                // 嵌套类型：拼出完整名（外层链 + 短名）
                var parts = [s];
                var cur = outer, guard = 0;
                while (cur && !cur.isNull() && guard++ < 16) {
                  var on = funcs.il2cpp_class_get_name(cur);
                  if (on && !on.isNull()) { parts.unshift(on.readUtf8String()); }
                  var ons = funcs.il2cpp_class_get_namespace
                      ? funcs.il2cpp_class_get_namespace(cur) : null;
                  var onsStr = (ons && !ons.isNull()) ? ons.readUtf8String() : '';
                  if (onsStr) { parts.unshift(onsStr); break; }
                  cur = funcs.il2cpp_class_get_declaring_type(cur);
                }
                nested.push({short: s, full: parts.join('.'),
                             image: funcs.il2cpp_image_get_name
                                 ? funcs.il2cpp_image_get_name(img).readUtf8String() : ''});
              }
            }
            var pick = null;
            for (var t = 0; t < nested.length; t++) {
              if (!top[nested[t].short]) { pick = nested[t]; break; }
            }
            return {nested_count: nested.length, top_count: Object.keys(top).length,
                    pick: pick};
          } catch (e) { return 'err: ' + e.message; }
        })()
    """).get("value")
    print(f"  运行时索引：嵌套类型 {cand.get('nested_count') if isinstance(cand, dict) else cand} 个，"
          f"顶层类型 {cand.get('top_count') if isinstance(cand, dict) else ''} 个")
    pick = cand.get("pick") if isinstance(cand, dict) else None
    if not pick:
        print("  ⚠️ 本样本里找不到「短名不与顶层类型撞名」的嵌套类型 —— 反证跳过（如实记录）")
    else:
        print(f"  选中的嵌套类型: 短名={pick['short']} 完整名={pick['full']} "
              f"映像={pick['image']}")
        by_short = sess.class_info(name=pick["short"])
        by_full = sess.class_info(name=pick["full"])
        print(f"  用短名查 -> ok={by_short.get('ok')} "
              f"error={str(by_short.get('error'))[:70]} "
              f"full_name={by_short.get('full_name')}")
        print(f"  用完整嵌套名查 -> ok={by_full.get('ok')} "
              f"full_name={by_full.get('full_name')}")
        check("短名**查不到**这个嵌套类型（老规则会把它兜出来）",
              by_short.get("ok") is False
              and by_short.get("full_name") != pick["full"],
              f"短名竟解析到 {by_short.get('full_name')}")
        check("完整嵌套名能查到它",
              by_full.get("ok") is True
              and by_full.get("full_name") == pick["full"],
              f"实际 {by_full.get('full_name')}")

finally:
    try:
        close_all_sessions()
    except Exception as exc:  # noqa: BLE001
        print(f"  关闭会话时出错：{exc}")
    subprocess.run(["taskkill", "/F", "/T", "/IM", Path(exe).name],
                   capture_output=True)

show("汇总")
if FAILS:
    print(f"  ❌ 失败 {len(FAILS)} 项：")
    for f in FAILS:
        print(f"     - {f}")
    sys.exit(1)
print("  ✅ 全部通过")
