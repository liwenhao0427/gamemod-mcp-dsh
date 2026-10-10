'use strict';
/**
 * GameMod MCP —— 游戏内运行时探针（注入脚本）
 * ============================================================================
 * 职责：在目标进程内部解析**引擎的**导出符号，把「静态元数据」与「运行时实况」
 *       对上：
 *         · 字段的真实运行时偏移（静态元数据里没有，泛型实例化后才确定）
 *         · 方法的真实代码地址 → 这就是 Hook 落点
 *         · 类/方法/字段的运行时枚举与类型名
 *         · 内存读写、AOB 扫描、任意 JS 求值（兜底手段）
 *
 * 支持两个后端，共用同一套 rpc.exports（宿主不需要知道底下是哪个）：
 *   · IL2CPP —— GameAssembly.dll / libil2cpp.so，走官方 il2cpp-api.h
 *   · Mono   —— mono-2.0-bdwgc.dll 等，走 Mono 的 embedding API
 * 两个后端的差异**只体现在本文件的两段后端实现里**，RPC 层只调 BE.*。
 *
 * 设计原则（与 AiMod 的「元数据优先」一致）：
 *   1. 优先走引擎官方导出的稳定接口，不硬编码内部结构体布局。
 *      内部布局随版本变化，硬编码是崩溃与错值的根源。
 *   2. 任何一步失败都返回结构化错误，绝不返回看似成功的假数据。
 *   3. 所有对外返回的值必须是可 JSON 序列化的。
 *
 * 未经验证的部分（诚实声明）：
 *   · IL2CPP 的导出符号名取自 Unity 官方 il2cpp-api.h；不同 Unity 版本可能
 *     缺少部分导出（例如 il2cpp_field_get_offset 在个别版本被内联）。
 *     脚本会如实报告哪些 API 可用，不使用不可用 API。
 *   · Mono 后端所需的导出**逐个在四个 Mono 样本上核实过**（见 SKILL.md 的
 *     引擎差异表）；本版 Mono 确实没有的导出（mono_method_get_param_count /
 *     mono_gc_disable / mono_gc_enable）一律用替代路径或如实报不支持。
 *   · 回退路径（读 Il2CppClass / FieldInfo 结构体偏移）取自社区通用布局，
 *     **未经本项目地面真值验证**，仅在导出缺失时启用并会在结果里标注。
 * ============================================================================
 */
(function () {

    // ---------------------------------------------------------------- 常量

    // 各平台 IL2CPP 运行时模块名
    var MODULE_CANDIDATES = [
        'GameAssembly.dll',
        'libil2cpp.so',
        'libil2cpp.dylib'
    ];

    // 官方 il2cpp-api.h 导出符号（按用途分组，便于缺失时给出可读报告）
    var API_NAMES = [
        // 域 / 线程 / GC
        'il2cpp_domain_get',
        'il2cpp_domain_get_assemblies',
        'il2cpp_thread_attach',
        'il2cpp_thread_detach',
        'il2cpp_gc_disable',
        'il2cpp_gc_enable',
        'il2cpp_gc_collect',
        // 程序集 / 映像
        'il2cpp_assembly_get_image',
        'il2cpp_image_get_name',
        'il2cpp_image_get_filename',
        'il2cpp_image_get_class_count',
        'il2cpp_image_get_class',
        // 类
        'il2cpp_class_from_name',
        'il2cpp_class_get_name',
        'il2cpp_class_get_namespace',
        'il2cpp_class_get_parent',
        'il2cpp_class_get_declaring_type',
        'il2cpp_class_get_image',
        'il2cpp_class_get_flags',
        'il2cpp_class_get_type',
        'il2cpp_class_get_interfaces',
        'il2cpp_class_get_nested_types',
        'il2cpp_class_instance_size',
        'il2cpp_class_value_size',
        'il2cpp_class_is_enum',
        'il2cpp_class_is_valuetype',
        'il2cpp_class_is_interface',
        'il2cpp_class_is_abstract',
        'il2cpp_class_is_generic',
        // 字段
        'il2cpp_class_get_fields',
        'il2cpp_field_get_name',
        'il2cpp_field_get_offset',
        'il2cpp_field_get_type',
        'il2cpp_field_get_parent',
        'il2cpp_field_get_flags',
        'il2cpp_field_static_get_value',
        'il2cpp_field_get_value',
        'il2cpp_field_set_value',
        // 方法
        'il2cpp_class_get_methods',
        'il2cpp_class_get_method_from_name',
        'il2cpp_method_get_name',
        'il2cpp_method_get_param_count',
        'il2cpp_method_get_param',
        'il2cpp_method_get_param_name',
        'il2cpp_method_get_return_type',
        'il2cpp_method_get_declaring_type',
        'il2cpp_method_is_instance',
        'il2cpp_method_is_generic',
        'il2cpp_method_is_inflated',
        // 类型
        'il2cpp_type_get_name',
        'il2cpp_type_get_object',
        'il2cpp_free',
        // 对象 / 字符串
        'il2cpp_object_new',
        // 实例枚举用：拿到某个实例**真实**的类（liveness 会连派生类实例
        // 一起返回 —— 实测 find_instances(Transform) 拿到的是 RectTransform）
        'il2cpp_object_get_class',
        'il2cpp_string_new',
        'il2cpp_string_length',
        'il2cpp_string_chars',
        // 调用
        'il2cpp_runtime_invoke',
        'il2cpp_resolve_icall',
        // 元数据
        'il2cpp_method_get_token',
        'il2cpp_class_get_type_token',
        // 实例枚举（GC liveness）—— 见 findInstances() 的说明。
        // 这四个是 Unity 自家内存分析器用的导出，也是 frida-il2cpp-bridge
        // 的 `gc.choose` 走的路径；`il2cpp_class_get_instances` 这种
        // 「按类直接列实例」的导出**并不存在**（5 个样本实测）。
        'il2cpp_unity_liveness_allocate_struct',
        'il2cpp_unity_liveness_calculation_from_statics',
        'il2cpp_unity_liveness_finalize',
        'il2cpp_unity_liveness_free_struct',
        // 注意导出名是 `il2cpp_stop_gc_world`，**不是** `il2cpp_gc_stop_world`
        // （本机 5 个样本实测：前者 5/5 存在，后者 0/5）。
        'il2cpp_stop_gc_world',
        'il2cpp_start_gc_world',
        'il2cpp_alloc'
    ];

    // Il2CppString 布局（64 位）：
    //   Il2CppObject { Il2CppClass* klass; void* monitor; } = 16 字节
    //   int32_t length;   @ +0x10
    //   uint16_t chars[]; @ +0x14（UTF-16LE）
    var IL2CPP_STRING_LENGTH_OFFSET = 0x10;
    var IL2CPP_STRING_CHARS_OFFSET = 0x14;

    // 回退布局（未经验证，仅在官方导出缺失时启用）
    //   FieldInfo { const char* name; const Il2CppType* type; Il2CppClass* parent;
    //               int32_t offset; uint32_t token; }
    var FIELDINFO_OFFSET_FALLBACK = 0x18;
    //   MethodInfo { Il2CppMethodPointer methodPointer; ... }
    var METHODINFO_POINTER_OFFSET = 0x00;

    // 各平台 Mono 运行时模块名（Unity 的 MonoBleedingEdge/EmbedRuntime 下）
    var MONO_MODULE_CANDIDATES = [
        'mono-2.0-bdwgc.dll',
        'mono-2.0-sgen.dll',
        'mono.dll',
        'libmonosgen-2.0.so',
        'libmonobdwgc-2.0.so',
        'libmono-2.0.so',
        'libmonosgen-2.0.dylib'
    ];

    /**
     * Mono embedding API 导出符号。
     *
     * 这份清单是**按能力逐个在四个 Mono 样本上核实过的**（不是照抄文档）：
     * 缺失的那几个都有替代路径或明确不支持，见各处注释与 SKILL.md 的引擎差异表。
     * 特别地：`mono_method_get_param_count` 在本版 Mono **不存在**，
     * 参数个数一律走 mono_method_signature + mono_signature_get_param_count。
     */
    var MONO_API_NAMES = [
        // 域 / 线程
        'mono_get_root_domain',
        'mono_domain_get',
        'mono_thread_attach',
        'mono_jit_thread_attach',
        'mono_thread_detach',
        // 程序集 / 映像
        'mono_assembly_foreach',
        'mono_domain_assembly_foreach',
        'mono_assembly_get_image',
        'mono_assembly_get_name',
        'mono_image_loaded',
        'mono_image_get_name',
        'mono_image_get_filename',
        'mono_image_get_table_rows',
        'mono_image_is_dynamic',
        // 按 TypeDef 行号取类（短名解析的兜底路径；Mono 没有 mono_image_get_class）
        'mono_image_get_table_info',
        'mono_table_info_get_rows',
        // 类
        'mono_class_from_name',
        'mono_class_get',
        'mono_class_get_name',
        'mono_class_get_namespace',
        'mono_class_get_parent',
        'mono_class_get_nesting_type',
        'mono_class_get_nested_types',
        'mono_class_get_image',
        'mono_class_get_flags',
        'mono_class_get_type_token',
        'mono_class_instance_size',
        'mono_class_num_fields',
        'mono_class_num_methods',
        'mono_class_is_enum',
        'mono_class_is_valuetype',
        'mono_class_get_interfaces',
        // 字段
        'mono_class_get_fields',
        'mono_class_get_field_from_name',
        'mono_field_get_name',
        'mono_field_get_offset',
        'mono_field_get_type',
        'mono_field_get_flags',
        'mono_field_static_get_value',
        'mono_field_get_value',
        // 方法
        'mono_class_get_methods',
        'mono_class_get_method_from_name',
        'mono_method_get_name',
        'mono_method_get_token',
        'mono_method_get_flags',
        'mono_method_signature',
        'mono_signature_get_param_count',
        'mono_signature_get_return_type',
        'mono_signature_is_instance',
        'mono_method_get_class',
        'mono_compile_method',
        'mono_free_method',
        // 类型 / 字符串
        'mono_type_get_name',
        'mono_string_to_utf8',
        'mono_string_new',
        'mono_free',
        // 实例枚举（Mono 侧只有受限的堆遍历；`mono_gc_walk_heap` 的官方
        // 注释写明「只在 pre-stop-world 回调内有效」—— 见 findInstancesMono）。
        // Mono **没有** il2cpp 那套 liveness 导出，也没有按类列实例的 API。
        'mono_gc_walk_heap',
        'mono_object_get_class'
    ];

    // 回退：System.String 长度字段名（不同 Mono/CoreCLR 命名不同）
    // 实测本版 Unity Mono 用 _stringLength；CoreCLR 用 m_stringLength。
    // 所以**查出来**，不硬编码 —— 硬编码 m_stringLength 在这里直接给 NULL。
    var MONO_STRING_LENGTH_FIELDS = ['_stringLength', 'm_stringLength', 'length'];

    // ---------------------------------------------------------------- 运行时状态

    var mod = null;          // 当前引擎的运行时模块
    var api = {};            // 已解析的导出（缺失为 null）
    var apiMissing = [];     // 缺失的导出名
    var funcs = {};          // NativeFunction 包装
    var fallbacks = [];      // 已启用的回退路径说明
    var hooks = {};          // hookId -> listener
    var scratch = [];        // 持久缓冲区：防止 Memory.alloc 被 GC 回收
    var hookSeq = 0;
    var hookCounts = {};     // hookId -> 已触发次数
    var gcDisabled = false;
    var attached = false;
    //: 探针线程附着过的 IL2CPP 域指针（卸载前要还回去，见 releaseDomain）
    var attachedDomain = null;

    // 后端（引擎实现）：RPC 层只调 BE.*，两个引擎的差异都收敛在下面两段实现里
    var BE = null;

    // Mono 后端状态
    var monoMod = null;          // Mono 运行时模块
    var monoFuncs = {};          // Mono NativeFunction 包装
    var monoDomain = null;       // MonoDomain*
    var monoImagesCache = null;  // [{name, class_count, image}]，**仅一次 RPC 内有效**
    // 短名兜底索引：{key, rows:[{klass, image, name, namespace}]}，**仅一次 RPC 内有效**。
    // 只在精确写法查不到时才建（一次约几万次 Mono 调用），所以不放进热路径。
    var monoTypeIndexCache = null;
    var monoKeep = [];           // 必须长期存活的 Frida 回调（被 GC 回收后再调用会崩）
    var monoStringLayout = null; // {field, offset} —— 初始化时查出来，不硬编码
    var monoTypeNameAllocates = null; // mono_type_get_name 是否堆分配（实测确认后决定是否 free）

    // ---------------------------------------------------------------- 基础工具

    function fail(msg, extra) {
        var out = { ok: false, error: String(msg) };
        if (extra) { out.detail = extra; }
        return out;
    }

    /**
     * 统一把整数格式化成固定宽度的十六进制串。
     *
     * 为什么要固定宽度：token 是元数据里的「表号 + 行号」（如方法表 0x06），
     * 静态侧格式化成 0x060005d5、运行时若格式化成 0x60005d5，
     * 字符串比较就会误判为不一致。固定 8 位宽可避免这类假阴性。
     */
    function hex32(v, width) {
        var w = width || 8;
        var s = v.toString(16);
        while (s.length < w) { s = '0' + s; }
        return '0x' + s;
    }

    /**
     * 取 IL2CPP 域指针，**取不到有效指针就返回 null**。
     *
     * 为什么不直接 `funcs.il2cpp_domain_get()`：IL2CPP 初始化完成前它会返回
     * 野指针（实测 0x135）。野指针本身读不出异常，但一旦传给
     * `il2cpp_thread_attach` 就是 access violation；而那个异常被吞掉之后，
     * 后续 il2cpp 调用会在未附着的线程上执行，**把游戏搞崩**。
     * 所以这里主动读一下它指向的第一个指针：能读通才算「域真的建好了」。
     */
    function domainPointer() {
        try {
            var d = funcs.il2cpp_domain_get();
            if (!d || d.isNull()) { return null; }
            d.readPointer();
            return d;
        } catch (e) {
            return null;
        }
    }

    /** 安全读 C 字符串，空指针返回 null 而不是抛异常。 */
    function cstr(p) {
        if (p === null || p === undefined) { return null; }
        try {
            var q = ptr(p);
            if (q.isNull()) { return null; }
            return q.readUtf8String();
        } catch (e) {
            return null;
        }
    }

    /** Il2CppString* → JS 字符串。 */
    function il2cppString(p) {
        if (p === null || p === undefined) { return null; }
        try {
            var q = ptr(p);
            if (q.isNull()) { return null; }
            var len = q.add(IL2CPP_STRING_LENGTH_OFFSET).readS32();
            if (len < 0 || len > 0x100000) { return null; }   // 明显不合理
            return q.add(IL2CPP_STRING_CHARS_OFFSET).readUtf16String(len);
        } catch (e) {
            return null;
        }
    }

    /**
     * 按**当前后端**解码托管字符串（Hook 事件里的 string 参数与 this_str 用它）。
     *
     * 两个引擎的字符串布局不同：IL2CPP 固定 0x10/0x14；
     * Mono 的长度字段偏移要运行时查（本版 mscorlib 是 _stringLength @16）。
     * 后端还没选定时退回 IL2CPP 布局（保持原有行为，不改变已通过验收的路径）。
     */
    function managedStringOf(p) {
        if (BE) {
            try { return BE.stringOf(p); } catch (e) { return null; }
        }
        return il2cppString(p);
    }

    /** 调 il2cpp_type_get_name，并释放返回的堆字符串（避免泄漏）。 */
    function typeName(typePtr) {
        if (typePtr === null || typePtr === undefined) { return null; }
        if (!funcs.il2cpp_type_get_name) { return null; }
        try {
            var p = funcs.il2cpp_type_get_name(ptr(typePtr));
            if (p.isNull()) { return null; }
            var s = p.readUtf8String();
            if (funcs.il2cpp_free) {
                try { funcs.il2cpp_free(p); } catch (e) { /* 释放失败不影响结果 */ }
            }
            return s;
        } catch (e) {
            return null;
        }
    }

    /** 取类名（优先导出，缺失时回退读结构体并登记）。 */
    function className(klass) {
        if (funcs.il2cpp_class_get_name) {
            return cstr(funcs.il2cpp_class_get_name(klass));
        }
        return null;
    }

    function classNamespace(klass) {
        if (funcs.il2cpp_class_get_namespace) {
            return cstr(funcs.il2cpp_class_get_namespace(klass));
        }
        return null;
    }

    /** 是否嵌套类型（IL2CPP 里嵌套类型的 namespace 为空、只有短名）。 */
    function isNestedClass(klass) {
        if (!funcs.il2cpp_class_get_declaring_type) { return false; }
        try {
            var outer = funcs.il2cpp_class_get_declaring_type(klass);
            return !!(outer && !outer.isNull());
        } catch (e) {
            return false;
        }
    }

    /** 由类拼出完整名（嵌套类型会补上外层链）。 */
    function fullNameOf(klass) {
        return nestedFullName(klass);
    }

    /**
     * 拼出含嵌套外层的完整名。
     *
     * 为什么必须走 declaring_type：IL2CPP 里 `namespace` 只属于**最外层**类型，
     * 嵌套类型的 namespace 是空的、名字只有短名。实测样本 20,072 个类型里
     * 7,056 个（35%）是嵌套类型，131 个叫 `State` 的类型只有 1 个是顶层 ——
     * 直接 `ns + '.' + name` 会把 130 个 `State` 全拼成同一个错名。
     *
     * 自底向上收集短名，遇到第一个非空 namespace 就补上并停止。
     * `il2cpp_class_get_declaring_type` 缺失时退化为原来的行为。
     */
    function nestedFullName(klass) {
        var parts = [];
        var cur = klass;
        var guard = 0;
        while (cur && !cur.isNull() && guard++ < 32) {
            var nm = className(cur);
            if (nm) { parts.unshift(nm); }
            var ns = classNamespace(cur);
            if (ns && ns.length) { parts.unshift(ns); break; }
            if (!funcs.il2cpp_class_get_declaring_type) { break; }
            var outer;
            try { outer = funcs.il2cpp_class_get_declaring_type(cur); }
            catch (e) { break; }
            if (!outer || outer.isNull()) { break; }
            cur = outer;
        }
        return parts.length ? parts.join('.') : null;
    }

    /**
     * 把绝对地址换算成「模块内 RVA + 十六进制」——Hook 落点就用这个。
     *
     * **只有地址真的落在当前模块内才给 RVA**。这一点在 Mono 上尤其重要：
     * Mono 是 JIT，`mono_compile_method` 给出的机器码**不在 mono 模块内**
     * （实测 in_module=false），此时 `addr - mod.base` 算出来的是个毫无意义的
     * 大数（例如 0xb6b68000）。把它当 RVA 输出，Agent 就会拿它去和静态元数据
     * 的 IL-RVA 对照 —— 那是假数据，比「没有 RVA」糟得多。
     */
    function describeAddress(addr) {
        var q = ptr(addr);
        if (q.isNull()) { return null; }
        var out = { address: q.toString() };
        if (mod) {
            out.module = mod.name;
            out.in_module = isInModule(q);
            if (out.in_module) {
                var rva = q.sub(mod.base);
                out.rva = rva.toString();
                out.rva_hex = hex32(rva);
            }
        } else {
            out.in_module = false;
        }
        return out;
    }

    /** 判断地址是否落在当前运行时模块内（区分托管方法与 icall/转发桩/JIT 代码）。 */
    function isInModule(addr) {
        if (!mod) { return false; }
        var q = ptr(addr);
        return q.compare(mod.base) >= 0 && q.compare(mod.base.add(mod.size)) < 0;
    }

    // ---------------------------------------------------------------- 模块与导出解析

    function resolveModule(preferred) {
        var names = preferred ? [preferred].concat(MODULE_CANDIDATES) : MODULE_CANDIDATES;
        for (var i = 0; i < names.length; i++) {
            var m = Process.findModuleByName(names[i]);
            if (m) { return m; }
        }
        return null;
    }

    function resolveExports() {
        api = {};
        apiMissing = [];
        for (var i = 0; i < API_NAMES.length; i++) {
            var nm = API_NAMES[i];
            var p = null;
            try {
                if (mod) { p = mod.findExportByName(nm); }
            } catch (e) {
                p = null;
            }
            if (p) { api[nm] = p; } else { apiMissing.push(nm); }
        }
    }

    /**
     * 用 NativeFunction 逐个包装。
     * 返回类型统一用 'pointer'：C 的 int32 返回值从 x0/w0 返回，
     * 低 32 位即为有效值，调用方按需截断，避免各函数签名不一致带来的错误。
     */
    function buildFunctions() {
        funcs = {};
        var sigs = {
            il2cpp_domain_get: ['pointer', []],
            il2cpp_domain_get_assemblies: ['pointer', ['pointer', 'pointer']],
            il2cpp_thread_attach: ['pointer', ['pointer']],
            il2cpp_thread_detach: ['void', ['pointer']],
            il2cpp_gc_disable: ['void', []],
            il2cpp_gc_enable: ['void', []],
            il2cpp_gc_collect: ['void', ['int']],

            il2cpp_assembly_get_image: ['pointer', ['pointer']],
            il2cpp_image_get_name: ['pointer', ['pointer']],
            il2cpp_image_get_filename: ['pointer', ['pointer']],
            il2cpp_image_get_class_count: ['pointer', ['pointer']],
            // 注意：第二个参数是**序号 size_t**，不是指针。写成 'pointer'
            // 会直接抛 "expected a pointer"（已实测）。
            il2cpp_image_get_class: ['pointer', ['pointer', 'size_t']],

            il2cpp_class_from_name: ['pointer', ['pointer', 'pointer', 'pointer']],
            il2cpp_class_get_name: ['pointer', ['pointer']],
            il2cpp_class_get_namespace: ['pointer', ['pointer']],
            il2cpp_class_get_parent: ['pointer', ['pointer']],
            // 嵌套类型的**外层类**。别和 il2cpp_class_get_parent（基类）搞混：
            // 对 MonoBehaviour 的子类，get_parent 给基类，
            // get_declaring_type 给外层嵌套类（顶层类为 NULL）。
            il2cpp_class_get_declaring_type: ['pointer', ['pointer']],
            il2cpp_class_get_image: ['pointer', ['pointer']],
            il2cpp_class_get_flags: ['pointer', ['pointer']],
            il2cpp_class_get_type: ['pointer', ['pointer']],
            il2cpp_class_get_interfaces: ['pointer', ['pointer', 'pointer']],
            il2cpp_class_get_nested_types: ['pointer', ['pointer', 'pointer']],
            il2cpp_class_instance_size: ['pointer', ['pointer']],
            il2cpp_class_value_size: ['pointer', ['pointer', 'pointer']],
            il2cpp_class_is_enum: ['pointer', ['pointer']],
            il2cpp_class_is_valuetype: ['pointer', ['pointer']],
            il2cpp_class_is_interface: ['pointer', ['pointer']],
            il2cpp_class_is_abstract: ['pointer', ['pointer']],
            il2cpp_class_is_generic: ['pointer', ['pointer']],

            il2cpp_class_get_fields: ['pointer', ['pointer', 'pointer']],
            il2cpp_field_get_name: ['pointer', ['pointer']],
            il2cpp_field_get_offset: ['pointer', ['pointer']],
            il2cpp_field_get_type: ['pointer', ['pointer']],
            il2cpp_field_get_parent: ['pointer', ['pointer']],
            il2cpp_field_get_flags: ['pointer', ['pointer']],
            il2cpp_field_static_get_value: ['void', ['pointer', 'pointer']],
            il2cpp_field_get_value: ['void', ['pointer', 'pointer', 'pointer']],
            il2cpp_field_set_value: ['void', ['pointer', 'pointer', 'pointer']],

            il2cpp_class_get_methods: ['pointer', ['pointer', 'pointer']],
            il2cpp_class_get_method_from_name: ['pointer', ['pointer', 'pointer', 'pointer']],
            il2cpp_method_get_name: ['pointer', ['pointer']],
            il2cpp_method_get_param_count: ['pointer', ['pointer']],
            il2cpp_method_get_param: ['pointer', ['pointer', 'pointer']],
            il2cpp_method_get_param_name: ['pointer', ['pointer', 'pointer']],
            il2cpp_method_get_return_type: ['pointer', ['pointer']],
            il2cpp_method_get_declaring_type: ['pointer', ['pointer']],
            il2cpp_method_is_instance: ['pointer', ['pointer']],
            il2cpp_method_is_generic: ['pointer', ['pointer']],
            il2cpp_method_is_inflated: ['pointer', ['pointer']],

            il2cpp_type_get_name: ['pointer', ['pointer']],
            il2cpp_type_get_object: ['pointer', ['pointer']],
            il2cpp_free: ['void', ['pointer']],

            il2cpp_object_new: ['pointer', ['pointer']],
            il2cpp_object_get_class: ['pointer', ['pointer']],
            il2cpp_string_new: ['pointer', ['pointer']],
            il2cpp_string_length: ['pointer', ['pointer']],
            il2cpp_string_chars: ['pointer', ['pointer']],

            il2cpp_runtime_invoke: ['pointer', ['pointer', 'pointer', 'pointer', 'pointer']],
            il2cpp_resolve_icall: ['pointer', ['pointer']],

            il2cpp_method_get_token: ['pointer', ['pointer']],
            il2cpp_class_get_type_token: ['pointer', ['pointer']],

            // 实例枚举（GC liveness）。签名逐字对照
            // `frida-il2cpp-bridge@0.14.0/lib/exports.ts`，写错会崩游戏。
            il2cpp_unity_liveness_allocate_struct:
                ['pointer', ['pointer', 'int', 'pointer', 'pointer', 'pointer']],
            il2cpp_unity_liveness_calculation_from_statics: ['void', ['pointer']],
            il2cpp_unity_liveness_finalize: ['void', ['pointer']],
            il2cpp_unity_liveness_free_struct: ['void', ['pointer']],
            il2cpp_stop_gc_world: ['void', []],
            il2cpp_start_gc_world: ['void', []],
            il2cpp_alloc: ['pointer', ['size_t']]
        };

        for (var nm in sigs) {
            if (!Object.prototype.hasOwnProperty.call(sigs, nm)) { continue; }
            if (!api[nm]) { continue; }
            try {
                funcs[nm] = new NativeFunction(api[nm], sigs[nm][0], sigs[nm][1]);
            } catch (e) {
                // 包装失败（少见）：当作不可用，并记录原因
                apiMissing.push(nm + '(wrap失败:' + e.message + ')');
            }
        }
    }

    /** 首次调用时初始化：找模块、解析导出、包装函数、附着线程。 */
    function ensureReady(preferredModule) {
        if (attached) { return { ok: true, already: true }; }

        mod = resolveModule(preferredModule);
        if (!mod) {
            return fail('未找到 IL2CPP 运行时模块（已尝试 ' +
                        MODULE_CANDIDATES.join(' / ') + '）',
                        { tried: MODULE_CANDIDATES, process: Process.id });
        }

        resolveExports();
        buildFunctions();
        // 重新绑定导出后，之前按旧函数指针建的类索引必须作废
        classIndex = null;

        if (!funcs.il2cpp_domain_get) {
            return fail('模块存在但缺少 il2cpp_domain_get 导出，' +
                        '该游戏可能不是 IL2CPP 构建，或符号已被剥离',
                        { module: mod.name, missing: apiMissing });
        }

        // 附着当前 Frida 线程到 GC 域，否则调用 il2cpp 接口可能崩溃。
        //
        // **这里的判断很关键，是实测踩出来的**：`GameAssembly.dll` 已加载、
        // 导出也解析得到，并不代表 IL2CPP 运行时已经初始化好 ——
        // `il2cpp_init` 还没跑完时 `il2cpp_domain_get()` 会返回**野指针**。
        // 实测 9 个 IL2CPP 游戏里有 6 个命中：`il2cpp_thread_attach` 报
        // `access violation accessing 0x135`，而老代码把它当作「可降级的
        // fallback」吞掉继续跑 → 之后每一次 il2cpp 调用都在**未附着的线程**
        // 上执行 → 游戏触发 IL2CPP 断言（0x80000003）**当场崩溃**。
        // 事件日志里的证据：Meta Ghost / PassTheFear / Wanderburg 的
        // GameAssembly.dll 0x80000003，且只在这 6 个游戏上出现。
        //
        // 所以：域没好就返回**可重试**的失败，让宿主继续轮询
        // （`RuntimeSession.wait_ready` 会每 0.5s 重试，最多 60s）；
        // 附着失败也一律拒绝继续，绝不「静默降级后带着一个会崩游戏的会话」。
        if (funcs.il2cpp_thread_attach) {
            var dom = domainPointer();
            if (!dom) {
                return fail('IL2CPP 域尚未初始化（il2cpp_domain_get 还没返回有效指针），' +
                            '稍后重试即可',
                            { module: mod.name });
            }
            try {
                funcs.il2cpp_thread_attach(dom);
                attachedDomain = dom;
            } catch (e) {
                return fail('无法把探针线程附着到 IL2CPP 域：' + e.message +
                            '。未附着的线程上调用 il2cpp 接口会让游戏崩溃，' +
                            '因此这里拒绝继续（稍后重试通常就能成功）',
                            { module: mod.name, domain: dom.toString() });
            }
        }

        // 字段偏移导出缺失时启用回退（明确登记，结果里会标注）
        if (!funcs.il2cpp_field_get_offset) {
            fallbacks.push(
                'il2cpp_field_get_offset 不可用，字段偏移改用 FieldInfo+' +
                FIELDINFO_OFFSET_FALLBACK.toString(16) + ' 回退读取（未经地面真值验证）'
            );
        }

        attached = true;
        return {
            ok: true,
            module: mod.name,
            base: mod.base.toString(),
            size: mod.size,
            api_available: Object.keys(funcs).length,
            api_missing: apiMissing,
            fallbacks: fallbacks
        };
    }

    // ---------------------------------------------------------------- 域与映像

    /**
     * 把探针线程从 IL2CPP 域摘出去 —— **卸载前必须调用**。
     *
     * 为什么必须做：`ensureReady` 用 `il2cpp_thread_attach(dom)` 把 Frida 的
     * 线程挂进 IL2CPP 域，IL2CPP 会为此在线程本地存一份该线程的 `Il2CppThread`
     * 记录。若不摘，`script.unload()` / `session.detach()` 之后：
     *
     *   · Frida 的 agent 内存被释放（模块名在事件日志里是
     *     `frida-agent.dll_unloaded`）；
     *   · 而 IL2CPP 域里**仍留着那条线程记录**，指向已经失效的内存；
     *   · 等下一次 GC 走到那条记录时解引用 —— 访问违例 `0xc0000005`。
     *
     * 实测症状与此完全吻合：三款不同 IL2CPP 游戏（Ved / Pass The Fear /
     * PathOfIdle）都在 detach 后 **80~160 秒**崩（等一次 GC 的时间），
     * 崩溃模块恒为 `frida-agent.dll_unloaded`、偏移恒为 `0x9c7e0`；
     * 而**非 Unity 进程**（python）detach 后完好存活 —— 因为没有 IL2CPP 域。
     * Ved 的 `ved.gc.log` 里那行 `Collecting from unknown thread` 也是同一件事。
     *
     * 失败不抛：卸载路径上宁可少摘一次，也不要因为摘失败而让 close 报错。
     *
     * @returns {{ok: boolean, detached: boolean, error?: string}}
     */
    function releaseDomain() {
        if (!attachedDomain || !funcs.il2cpp_thread_detach) {
            return { ok: true, detached: false };
        }
        try {
            funcs.il2cpp_thread_detach(attachedDomain);
            attachedDomain = null;
            attached = false;
            return { ok: true, detached: true };
        } catch (e) {
            return { ok: false, detached: false, error: e.message };
        }
    }

    function allImages() {
        var domain = funcs.il2cpp_domain_get();
        var countPtr = Memory.alloc(Process.pointerSize);
        countPtr.writeU32(0);
        var assemblies = funcs.il2cpp_domain_get_assemblies(domain, countPtr);
        var count = countPtr.readU32();
        var out = [];
        for (var i = 0; i < count; i++) {
            var asm = assemblies.add(i * Process.pointerSize).readPointer();
            if (asm.isNull()) { continue; }
            var img = funcs.il2cpp_assembly_get_image
                ? funcs.il2cpp_assembly_get_image(asm) : null;
            if (!img || img.isNull()) { continue; }
            var rec = { image: img };
            if (funcs.il2cpp_image_get_name) {
                rec.name = cstr(funcs.il2cpp_image_get_name(img));
            }
            if (funcs.il2cpp_image_get_filename) {
                rec.filename = cstr(funcs.il2cpp_image_get_filename(img));
            }
            if (funcs.il2cpp_image_get_class_count) {
                rec.class_count = funcs.il2cpp_image_get_class_count(img).toInt32();
            }
            out.push(rec);
        }
        return out;
    }

    function findImage(name) {
        var imgs = allImages();
        var needle = String(name).toLowerCase();
        for (var i = 0; i < imgs.length; i++) {
            var n = (imgs[i].name || '').toLowerCase();
            var f = (imgs[i].filename || '').toLowerCase();
            if (n === needle || f === needle || n.indexOf(needle) >= 0 ||
                f.indexOf(needle) >= 0) {
                return imgs[i];
            }
        }
        return null;
    }

    // ---------------------------------------------------------------- 类 / 字段 / 方法

    /**
     * 运行时类索引（惰性构建一次，之后复用）。
     *
     * 为什么不能只用 `il2cpp_class_from_name`：那个 API **只查得到顶层类型**，
     * 嵌套类型一个都拿不到。实测样本（Garden of Witches）：
     *   · 20,072 个类型里 7,056 个（35%）是嵌套类型；
     *   · 名为 `State` 的类型有 131 个，其中**只有 1 个是顶层**；
     *   · 在 `TeamTapas.ProjectR.dll` 里遍历出 118 个 `State`，
     *     用 `il2cpp_class_from_name` 逐个回查 —— **命中 0 个、漏掉 118 个**。
     * 也就是说「嵌套类在运行时根本查不到」，而 Mod 最常 Hook 的
     * 恰恰是这类内部类（状态机、协程体、私有嵌套类）。
     *
     * 全量枚举实测 94 个映像 / 20,072 个类 / 119 ms，一次构建即可长期复用。
     */
    var classIndex = null;

    function buildClassIndex() {
        if (!funcs.il2cpp_image_get_class_count || !funcs.il2cpp_image_get_class) {
            return null;   // 缺 API，调用方退化为 from_name 快路径
        }
        var byFull = {};
        var byShort = {};
        var imgs = allImages();
        var n = 0;
        for (var i = 0; i < imgs.length; i++) {
            var cnt;
            try {
                cnt = funcs.il2cpp_image_get_class_count(
                    imgs[i].image).toInt32();
            } catch (e) { continue; }
            for (var j = 0; j < cnt; j++) {
                var k;
                try { k = funcs.il2cpp_image_get_class(imgs[i].image, j); }
                catch (e) { continue; }
                if (!k || k.isNull()) { continue; }
                var shortName = className(k);
                if (!shortName) { continue; }
                var ent = {
                    klass: k,
                    image: imgs[i],
                    name: shortName,
                    namespace: classNamespace(k) || '',
                    full_name: nestedFullName(k),
                    // 嵌套与否必须问 declaring_type，**不能**用
                    // `full_name !== name` 去猜 —— 带命名空间的顶层类型
                    // （UnityEngine.Transform）也满足那个条件，会把它们
                    // 一起误判成嵌套类型。
                    is_nested: isNestedClass(k)
                };
                if (ent.full_name) {
                    if (!byFull[ent.full_name]) { byFull[ent.full_name] = []; }
                    byFull[ent.full_name].push(ent);
                }
                if (!byShort[shortName]) { byShort[shortName] = []; }
                byShort[shortName].push(ent);
                n++;
            }
        }
        return { byFull: byFull, byShort: byShort, count: n };
    }

    function classIndexOf() {
        if (classIndex === null) { classIndex = buildClassIndex(); }
        return classIndex;
    }

    /**
     * 解析类。返回以下之一：
     *   {ok: true,  klass, image, entry}
     *   {ok: false, not_found: true}
     *   {ok: false, ambiguous: true, candidates: [...], candidate_count, want_full}
     *
     * **歧义一律拒绝**，绝不「取第一个」。理由与静态侧同名类消歧同源：
     * 挂到错误的重载/类上，Hook 照样会成功，错误要到很久以后才以
     * 「数据不对」的形式暴露 —— 那比直接报错危险得多。
     */
    function resolveClass(imageName, namespace, name) {
        var img = null;
        if (imageName) {
            img = findImage(imageName);
            if (!img) {
                return { ok: false, error: '未找到映像 ' + imageName };
            }
        }
        var wantFull = namespace ? (namespace + '.' + name) : String(name);
        var idx = classIndexOf();
        if (!idx) {
            // 退化路径：只有 from_name 可用（顶层类型 + 第一个命中）
            var imgs0 = img ? [img] : allImages();
            var nsBuf = Memory.allocUtf8String(namespace || '');
            var nmBuf = Memory.allocUtf8String(name);
            for (var i0 = 0; i0 < imgs0.length; i0++) {
                var k0 = funcs.il2cpp_class_from_name(imgs0[i0].image, nsBuf,
                                                     nmBuf);
                if (k0 && !k0.isNull()) {
                    return { ok: true, klass: k0, image: imgs0[i0],
                             degraded: '缺少 il2cpp_image_get_class，' +
                                       '嵌套类型无法定位' };
                }
            }
            return { ok: false, not_found: true };
        }

        // 候选来源两处，都要看：
        //   · byShort[name] —— 调用方给的是「命名空间 + 短名」（静态索引的口径）
        //   · byFull[wantFull] —— 调用方直接给了含嵌套外层的完整名
        var pool = (idx.byShort[name] || []).concat(idx.byFull[wantFull] || []);
        var seen = {};
        var hits = [];
        for (var i = 0; i < pool.length; i++) {
            var e = pool[i];
            if (img && e.image.name !== img.name) { continue; }
            // 嵌套类型：namespace 是空的（IL2CPP 的 namespace 只属于最外层），
            // 判定必须问 declaring_type，见 classIndex 里的 is_nested。
            var isNested = (e.is_nested === true);
            // 命中条件三条：
            //   ① 完整名相等（调用方给了含嵌套外层的完整名）
            //   ② 调用方**显式**给了 namespace：按 namespace + 短名匹配
            //   ③ 调用方没给 namespace：只匹配**顶层**类型
            // ③ 里必须排除嵌套类型 —— 老写法是
            // `e.namespace === '' && e.name === name`，而嵌套类型的 namespace
            // 正好也是空，于是 `name="Transform"` 会命中
            // `UnityEngine.RuleTile.TilingRuleOutput.Transform` 这个嵌套枚举，
            // **静默返回完全不相干的类**（实测 RIP_nz 上就是这样，
            // 而且 fields 非空，看起来一切正常）。
            var byFullName = (e.full_name === wantFull);
            var byNsShort = (namespace !== '' &&
                             e.namespace === namespace && e.name === String(name));
            var byTopShort = (namespace === '' && !isNested &&
                              e.name === String(name));
            if (!byFullName && !byNsShort && !byTopShort) { continue; }
            var key = e.full_name + '@' + e.image.name;
            if (seen[key]) { continue; }
            seen[key] = 1;
            hits.push(e);
        }
        if (!hits.length) { return { ok: false, not_found: true }; }
        if (hits.length > 1) {
            return {
                ok: false,
                ambiguous: true,
                candidate_count: hits.length,
                want_full: wantFull,
                candidates: hits.slice(0, 30).map(function (e) {
                    return {
                        full_name: e.full_name,
                        name: e.name,
                        namespace: e.namespace,
                        image: e.image.name
                    };
                }),
                candidates_truncated: hits.length > 30
            };
        }
        return { ok: true, klass: hits[0].klass, image: hits[0].image,
                 entry: hits[0] };
    }

    /** 把 resolveClass 的失败转成统一的可操作错误返回。 */
    function resolveClassOrFail(imageName, namespace, name) {
        var r = resolveClass(imageName, namespace, name);
        if (r.ok) { return r; }
        var label = (namespace ? namespace + '.' : '') + name;
        if (r.error) { return fail(r.error, { image: imageName }); }
        if (r.ambiguous) {
            // 候选与计数**平铺在顶层**（不用 fail 的 detail 包裹）：
            // 静态侧 class_detail/search_fields 的同类拒绝就是这个形状，
            // 两侧返回结构一致，Agent 才不用按工具记两套读法。
            return {
                ok: false,
                error: '类名有歧义：' + label + ' 在运行时匹配到 ' +
                       r.candidate_count + ' 个类。请用 namespace= 或 image= ' +
                       '缩小范围；嵌套类型请用完整嵌套名' +
                       '（candidates[].full_name）指定唯一目标。',
                ambiguous: true,
                candidate_count: r.candidate_count,
                want_full: r.want_full,
                candidates: r.candidates,
                candidates_truncated: r.candidates_truncated,
                hint: '嵌套类型（如 Xxx.State）在静态索引里 namespace 为空，' +
                      '要定位它请把 name 传成完整的嵌套名。'
            };
        }
        // 找不到时把「命名空间」这条硬门槛说清楚 —— 老错误信息
        // 「未找到类 Transform」会让 Agent 以为类不存在，然后放弃。
        return fail('未找到类 ' + label, {
            image: imageName || '(全部映像)',
            hint: '带命名空间的类型（如 UnityEngine.Transform）需要同时给 ' +
                  'namespace=（或把 name 传成完整名）。可用静态侧 ' +
                  'search_classes(pattern="' + name + '") 先确认它的 ' +
                  'namespace 与 nested_name。'
        });
    }

    function fieldOffset(fieldInfo) {
        if (funcs.il2cpp_field_get_offset) {
            return { offset: funcs.il2cpp_field_get_offset(fieldInfo).toInt32(),
                     via: 'il2cpp_field_get_offset' };
        }
        // 回退：读 FieldInfo 结构体的 offset 成员
        try {
            return {
                offset: fieldInfo.add(FIELDINFO_OFFSET_FALLBACK).readS32(),
                via: 'FieldInfo+' + FIELDINFO_OFFSET_FALLBACK.toString(16) + '(回退)'
            };
        } catch (e) {
            return { offset: null, via: null, error: e.message };
        }
    }

    /** 枚举类的字段，带运行时偏移与类型名。 */
    function fieldsOf(klass, wantStatic) {
        if (!funcs.il2cpp_class_get_fields) { return []; }
        var iter = Memory.alloc(Process.pointerSize);
        iter.writePointer(NULL);
        var out = [];
        var guard = 0;
        for (;;) {
            var f = funcs.il2cpp_class_get_fields(klass, iter);
            if (!f || f.isNull()) { break; }
            if (++guard > 100000) { break; }   // 防御性上限
            var rec = {
                name: cstr(funcs.il2cpp_field_get_name(f)),
                type: funcs.il2cpp_field_get_type
                    ? typeName(funcs.il2cpp_field_get_type(f)) : null,
                token: null
            };
            var off = fieldOffset(f);
            rec.offset = off.offset;
            rec.offset_via = off.via;
            if (funcs.il2cpp_field_get_flags) {
                rec.flags = hex32(funcs.il2cpp_field_get_flags(f).toUInt32());
            }
            // 静态字段没有实例偏移语义，用 flag 区分（0x10 = FIELD_ATTRIBUTE_STATIC）
            rec.is_static = rec.flags
                ? ((parseInt(rec.flags, 16) & 0x10) !== 0) : null;
            if (wantStatic === true && rec.is_static !== true) { continue; }
            if (wantStatic === false && rec.is_static === true) { continue; }
            out.push(rec);
        }
        return out;
    }

    /** 取 MethodInfo 的原生代码地址。 */
    function methodPointer(methodInfo) {
        try {
            return methodInfo.readPointer();
        } catch (e) {
            return null;
        }
    }

    /** 枚举类的方法，带真实代码地址 / RVA。 */
    function methodsOf(klass) {
        if (!funcs.il2cpp_class_get_methods) { return []; }
        var iter = Memory.alloc(Process.pointerSize);
        iter.writePointer(NULL);
        var out = [];
        var guard = 0;
        for (;;) {
            var m = funcs.il2cpp_class_get_methods(klass, iter);
            if (!m || m.isNull()) { break; }
            if (++guard > 100000) { break; }
            var mp = methodPointer(m);
            var rec = {
                name: cstr(funcs.il2cpp_method_get_name(m)),
                param_count: funcs.il2cpp_method_get_param_count
                    ? funcs.il2cpp_method_get_param_count(m).toInt32() : null,
                return_type: funcs.il2cpp_method_get_return_type
                    ? typeName(funcs.il2cpp_method_get_return_type(m)) : null,
                is_instance: funcs.il2cpp_method_is_instance
                    ? (funcs.il2cpp_method_is_instance(m).toInt32() !== 0) : null,
                is_generic: funcs.il2cpp_method_is_generic
                    ? (funcs.il2cpp_method_is_generic(m).toInt32() !== 0) : null,
                token: funcs.il2cpp_method_get_token
                    ? hex32(funcs.il2cpp_method_get_token(m).toUInt32())
                    : null,
                method_info: m.toString()
            };
            if (mp && !mp.isNull()) {
                var d = describeAddress(mp);
                rec.pointer = d.address;
                rec.rva = d.rva;
                rec.rva_hex = d.rva_hex;
                rec.in_module = isInModule(mp);
            } else {
                rec.pointer = null;
                rec.note = '方法未获得原生代码地址（可能是泛型定义或抽象方法）';
            }
            out.push(rec);
        }
        return out;
    }

    /** 参数名列表（失败返回空数组，不抛异常）。 */
    function paramNames(methodInfo) {
        if (!funcs.il2cpp_method_get_param_name || !funcs.il2cpp_method_get_param_count) {
            return [];
        }
        var n = funcs.il2cpp_method_get_param_count(methodInfo).toInt32();
        var out = [];
        for (var i = 0; i < n; i++) {
            out.push(cstr(funcs.il2cpp_method_get_param_name(methodInfo, i)));
        }
        return out;
    }

    // ---------------------------------------------------------------- Hook

    /**
     * 安全地取第 i 个整型寄存器参数。
     *
     * 为什么不能写 `if (i >= args.length) break;`：Frida 17 起 `args` 是
     * **寄存器视图**，根本不暴露 `length` —— 读 `args.length` 会直接抛
     * `RangeError: invalid array index`。老代码的判界语句自己就是异常源，
     * 于是任何带 `args_type` 的 Hook 在 onEnter 里就抛异常，
     * **整条 enter 事件（含 from / thread / args）全部丢失**，只剩 leave，
     * 调用方会误以为「参数没捕获到」而不是「探针坏了」。
     * 所以越界只能一个一个试，失败即视为「没有这个参数」。
     */
    function safeArg(args, i) {
        try {
            var v = args[i];
            return (v === undefined) ? null : v;
        } catch (e) {
            return null;
        }
    }

    /**
     * 从 XMM 寄存器里读浮点值。
     *
     * x64 的浮点参数/返回值**不走** RCX/RDX/R8/R9：`args[i]` 拿到的是与
     * 该浮点参数无关的整型寄存器（实测 Mathf.Abs(-2.5f) 的 args[0]
     * 恰好是 RCX 的残留值）。Frida 17 把 xmm 暴露成 16 字节 ArrayBuffer，
     * 更老的版本是 `{f: [...], d: [...]}`，这里两种都认。
     */
    function xmmValue(ctx, index, kind) {
        try {
            if (!ctx) { return null; }
            var reg = ctx['xmm' + index];
            if (reg === undefined || reg === null) { return null; }
            if (reg.f && kind === 'float' && reg.f.length > 0) { return reg.f[0]; }
            if (reg.d && kind === 'double' && reg.d.length > 0) { return reg.d[0]; }
            var buf = null, off = 0;
            if (reg instanceof ArrayBuffer) {
                buf = reg;
            } else if (typeof reg.byteLength === 'number') {
                off = reg.byteOffset || 0;
                buf = reg.buffer;
            }
            var need = (kind === 'double') ? 8 : 4;
            if (!buf || buf.byteLength < off + need) { return null; }
            var dv = new DataView(buf, off);
            return (kind === 'double') ? dv.getFloat64(0, true) : dv.getFloat32(0, true);
        } catch (e) {
            return null;
        }
    }

    /**
     * 按参数规格从整型寄存器读值。
     * 规格字符串：int / uint / long / ulong / bool / ptr / string / cstring
     *
     * float / double **不经过这里**：x64 的浮点参数与返回值走 XMM 寄存器，
     * 一律由 xmmValue 处理。这里对 fp 规格显式报错，避免路由漏了之后
     * 静默按指针去读内存（那会读出垃圾或直接崩）。
     */
    function readArg(spec, raw) {
        try {
            switch (spec) {
                case 'int':     return raw.toInt32();
                case 'uint':    return raw.toUInt32();
                case 'long':    return raw.toInt64().toString();
                case 'ulong':   return raw.toUInt64().toString();
                case 'bool':    return raw.toInt32() !== 0;
                case 'float':
                case 'double':  throw new Error('浮点参数/返回值必须走 xmmValue');
                case 'ptr':     return raw.toString();
                case 'cstring': return cstr(raw);
                case 'string':  return managedStringOf(raw);
                default:        return raw.toString();
            }
        } catch (e) {
            return '<读取失败:' + e.message + '>';
        }
    }

    function hookAt(spec) {
        var target;
        try {
            target = ptr(spec.address);
        } catch (e) {
            return fail('地址无法解析：' + spec.address);
        }

        var id = 'h' + (++hookSeq);
        var argSpecs = spec.args || [];
        var maxCalls = typeof spec.max_calls === 'number' ? spec.max_calls : 200;
        var wantBacktrace = !!spec.backtrace;
        var wantRetval = spec.retval !== false;      // 默认捕获返回值
        var retvalSpec = spec.retval_type || 'ptr';
        var counts = { enter: 0, leave: 0 };
        hookCounts[id] = counts;

        try {
            var listener = Interceptor.attach(target, {
                onEnter: function (args) {
                    if (counts.enter >= maxCalls) { return; }
                    counts.enter++;
                    var rec = {
                        type: 'hook', id: id, label: spec.label || null,
                        phase: 'enter',
                        seq: counts.enter,
                        thread: this.threadId,
                        from: describeAddress(this.returnAddress)
                    };
                    if (spec.this_type === 'string') {
                        var selfPtr = safeArg(args, 0);
                        if (selfPtr !== null) { rec.this_str = managedStringOf(selfPtr); }
                    }
                    // 整型参数按 RCX/RDX/R8/R9 顺序取（`this` 占第一个），
                    // 浮点参数按 XMM0..3 顺序取 —— 两套计数器互不影响，
                    // 混用签名时不能拿同一个下标去查两处。
                    var vals = [];
                    var intIdx = 0, fpIdx = 0, argErr = null;
                    for (var i = 0; i < argSpecs.length; i++) {
                        var specI = argSpecs[i];
                        if (specI === 'float' || specI === 'double') {
                            var fv = xmmValue(this.context, fpIdx, specI);
                            fpIdx++;
                            if (fv === null) {
                                argErr = 'args[' + i + ']（' + specI + '）取不到：'
                                       + 'xmm' + (fpIdx - 1) + ' 不可读';
                                vals.push(null);
                                continue;
                            }
                            vals.push(fv);
                            continue;
                        }
                        var rawI = safeArg(args, intIdx);
                        intIdx++;
                        if (rawI === null) { break; }
                        try {
                            vals.push(readArg(specI, rawI));
                        } catch (e) {
                            argErr = 'args[' + i + '] 读取失败：' + e.message;
                            break;
                        }
                    }
                    rec.args = vals;
                    // 参数读取失败**不能**把整条 enter 事件吞掉：
                    // 调用方要的是「这次调用发生了、从哪来」，参数是附加值。
                    if (argErr) { rec.args_error = argErr; }
                    if (wantBacktrace) {
                        try {
                            rec.backtrace = Thread.backtrace(this.context,
                                Backtracer.ACCURATE).map(function (a) {
                                    var d = describeAddress(a);
                                    return d ? (d.module ? (d.module + '!' + d.rva_hex)
                                                         : d.address)
                                             : String(a);
                                });
                        } catch (e) {
                            rec.backtrace = ['<回溯失败:' + e.message + '>'];
                        }
                    }
                    try { send(rec); } catch (e) { /* 发送失败忽略，不阻断游戏 */ }
                },
                onLeave: function (retval) {
                    if (counts.leave >= maxCalls) { return; }
                    counts.leave++;
                    var rec = {
                        type: 'hook', id: id, label: spec.label || null,
                        phase: 'leave',
                        seq: counts.leave,
                        thread: this.threadId
                    };
                    if (wantRetval) {
                        // 浮点返回值在 XMM0，不在 RAX —— 按 ptr 读会给出一个
                        // 看着像地址、其实毫无意义的整数（实测 0x20da0a48360）。
                        if (retvalSpec === 'float' || retvalSpec === 'double') {
                            var rv = xmmValue(this.context, 0, retvalSpec);
                            rec.retval = (rv === null) ? null : rv;
                            if (rv === null) { rec.retval_error = 'xmm0 不可读'; }
                        } else {
                            rec.retval = readArg(retvalSpec, retval);
                        }
                    }
                    try { send(rec); } catch (e) { /* 同上 */ }
                }
            });
            hooks[id] = { listener: listener, spec: spec, target: target };
            return {
                ok: true,
                hook_id: id,
                target: target.toString(),
                target_rva: describeAddress(target),
                in_module: isInModule(target),
                args: argSpecs,
                label: spec.label || null,
                note: '事件通过 send() 推送到宿主，用 drain_events 读取'
            };
        } catch (e) {
            return fail('Hook 失败：' + e.message, { address: spec.address });
        }
    }

    // ---------------------------------------------------------------- 导出接口

    // ================================================================ 共用：带继承链的字段枚举

    /**
     * 沿父类链收集字段（IL2CPP 与 Mono 共用）。
     *
     * 为什么必须有这一层：**两个引擎的字段枚举 API 都只给本类自己声明的字段**。
     * Mono 侧实测（Archon Soul / UnityEngine.Transform）：
     *   Transform 自己 0 个字段、Component 0 个、Object 5 个（m_CachedPtr 在 Object 上）
     * —— 于是 `runtime_class_info("Transform")` 返回 0 个字段。这不是工具坏了，
     * 是 Unity 的字段本来就都在基类上。IL2CPP 侧同样只给本类字段，
     * 这也是「字段偏移查不到」类反馈的同一个根因。
     *
     * 规则：
     *   · 从最派生类往上走，最多 32 层（防元数据异常造成死循环）；
     *   · 同名同静态性的字段**以最派生的那个为准**（C# 的隐藏语义）；
     *   · 非本类声明的字段加 `inherited_from: <声明类全名>`，让调用方能分辨来源。
     */
    function fieldsWithInheritance(klass, wantStatic) {
        var out = [];
        var seen = {};
        var cur = klass;
        var depth = 0;
        while (cur && !cur.isNull() && depth < 32) {
            var own = [];
            try { own = BE.fieldsOfOwn(cur, wantStatic) || []; } catch (e) { own = []; }
            var owner = null;
            try { owner = BE.fullNameOf(cur); } catch (e) { owner = null; }
            for (var i = 0; i < own.length; i++) {
                var rec = own[i];
                var key = String(rec.name) + '|' + (rec.is_static === true ? 's' : 'i');
                if (seen[key]) { continue; }
                seen[key] = true;
                if (depth > 0 && owner) { rec.inherited_from = owner; }
                out.push(rec);
            }
            var par = null;
            try { par = BE.parentOf(cur); } catch (e) { par = null; }
            if (!par || par.isNull()) { break; }
            cur = par;
            depth++;
        }
        return out;
    }

    // ================================================================ Mono 后端
    //
    // 与 IL2CPP 后端的差别（每一条都是实测出来的，不是推测）：
    //   · 就绪判断：mono_get_root_domain() 返回 NULL 就是没起来（比 IL2CPP 的野指针干净）
    //   · 附线程：mono_thread_attach(domain) —— 四个样本上都可用；
    //     代码里仍保留 mono_jit_thread_attach 作为备选
    //   · Hook 落点：mono_compile_method() 触发 JIT 并给出**真实机器码地址**；
    //     该地址**不在 mono 模块内**（实测 in_module=false），所以没有 RVA ——
    //     结果里不输出 rva（输出一个相对模块的假 RVA 就是假数据）
    //   · 抽象方法：mono_compile_method 返回 NULL（实测 System.IO.Stream.Read），
    //     不崩 —— 如实记为「无代码地址」
    //   · 参数个数：本版 Mono **没有** mono_method_get_param_count 导出，
    //     统一走 mono_method_signature + mono_signature_get_param_count
    //   · 嵌套类型：mono_class_from_name 认的是
    //     (最外层命名空间, "Outer/Inner") 斜杠形式；点号形式一律查不到（实测四种写法）
    //   · 字符串：System.String 的长度字段在本版 mscorlib 里叫 _stringLength
    //     （不是 CoreCLR 的 m_stringLength，实测按名查 m_stringLength 给 NULL），
    //     偏移在初始化时**查出来**，不硬编码
    //   · GC 控制：本版 Mono 没有 mono_gc_disable / mono_gc_enable 导出（四个样本一致），
    //     如实报「不支持」，不假装成功

    // Mono 导出符号的签名表（只登记 MONO_API_NAMES 里用得到的）
    //
    // ⚠️ 数值返回统一按 'pointer' 读，调用方用 .toInt32()/.toUInt32() 取值。
    //
    // 为什么不用 Frida 的 'int'/'uint32'/'bool'：那三种返回类型给的是 **JS number**，
    // 而 'pointer' 给的是 NativePointer —— 两种写法本身都对，但**混用就会炸**。
    // 实测踩到的：表里写 'int'、调用方按 IL2CPP 的约定写 `.toInt32()`，
    // 于是 `mono_field_get_offset(f).toInt32()` 抛
    // `TypeError: not a function`（number 没有 toInt32），**初始化直接失败**；
    // 更糟的是有 4 处调用点在 try/catch 里，会静默变成 null ——
    // 表现成「字段偏移查不到」，而不是报错。
    // 本文件 IL2CPP 侧历来就是 'pointer' + toInt32 的写法，Mono 侧跟它保持一致，
    // 这样 12 个数值调用点无需各写一套。'pointer' 读的是整个 RAX，
    // toInt32() 取低 32 位 —— 对 int/uint32/bool 返回都是正确的。
    var MONO_SIGS = {
        mono_get_root_domain: ['pointer', []],
        mono_domain_get: ['pointer', []],
        mono_thread_attach: ['pointer', ['pointer']],
        mono_jit_thread_attach: ['pointer', ['pointer']],
        mono_thread_detach: ['void', ['pointer']],
        mono_assembly_foreach: ['void', ['pointer', 'pointer']],
        mono_domain_assembly_foreach: ['void', ['pointer', 'pointer', 'pointer']],
        mono_assembly_get_image: ['pointer', ['pointer']],
        mono_assembly_get_name: ['pointer', ['pointer']],
        mono_image_loaded: ['pointer', ['pointer']],
        mono_image_get_name: ['pointer', ['pointer']],
        mono_image_get_filename: ['pointer', ['pointer']],
        mono_image_get_table_rows: ['pointer', ['pointer', 'int']],
        mono_image_is_dynamic: ['pointer', ['pointer']],
        // 注意第二个参数是 TypeDef token（guint32），按 uint32 传，
        // 这样调用方可以直接给 JS 数字；'pointer' 参数则必须给 NativePointer。
        mono_class_get: ['pointer', ['pointer', 'uint32']],
        mono_image_get_table_info: ['pointer', ['pointer', 'int']],
        mono_table_info_get_rows: ['pointer', ['pointer']],
        mono_class_from_name: ['pointer', ['pointer', 'pointer', 'pointer']],
        mono_class_get_name: ['pointer', ['pointer']],
        mono_class_get_namespace: ['pointer', ['pointer']],
        mono_class_get_parent: ['pointer', ['pointer']],
        mono_class_get_nesting_type: ['pointer', ['pointer']],
        mono_class_get_nested_types: ['pointer', ['pointer', 'pointer']],
        mono_class_get_image: ['pointer', ['pointer']],
        mono_class_get_flags: ['pointer', ['pointer']],
        mono_class_get_type_token: ['pointer', ['pointer']],
        mono_class_instance_size: ['pointer', ['pointer']],
        mono_class_num_fields: ['pointer', ['pointer']],
        mono_class_num_methods: ['pointer', ['pointer']],
        mono_class_is_enum: ['pointer', ['pointer']],
        mono_class_is_valuetype: ['pointer', ['pointer']],
        mono_class_get_interfaces: ['pointer', ['pointer', 'pointer']],
        // 实例枚举（签名逐字对照 mono/metadata/mono-gc.h）：
        //   int mono_gc_walk_heap(int flags, MonoGCReferences cb, void *data)
        // 回调返回非 0 继续遍历。官方限制：堆遍历「只在 pre-stop-world
        // 回调内有效」—— 见 findInstancesMono 的说明。
        //
        // ⚠️ 返回类型登记为 **pointer** 而非 int：本文件统一如此
        // （见 buildFunctions 的说明 —— 数值返回按 pointer 拿、调用方
        // 用 `.toInt32()` 截断）。历史上非 pointer 的数值返回类型
        // 造成过一次真实崩溃，verify_mono_runtime.py 有断言钉住这条规矩。
        mono_gc_walk_heap: ['pointer', ['int', 'pointer', 'pointer']],
        mono_object_get_class: ['pointer', ['pointer']],
        mono_class_get_fields: ['pointer', ['pointer', 'pointer']],
        mono_class_get_field_from_name: ['pointer', ['pointer', 'pointer']],
        mono_field_get_name: ['pointer', ['pointer']],
        mono_field_get_offset: ['pointer', ['pointer']],
        mono_field_get_type: ['pointer', ['pointer']],
        mono_field_get_flags: ['pointer', ['pointer']],
        mono_field_static_get_value: ['void', ['pointer', 'pointer']],
        mono_field_get_value: ['void', ['pointer', 'pointer', 'pointer']],
        mono_class_get_methods: ['pointer', ['pointer', 'pointer']],
        mono_class_get_method_from_name: ['pointer', ['pointer', 'pointer', 'int']],
        mono_method_get_name: ['pointer', ['pointer']],
        mono_method_get_token: ['pointer', ['pointer']],
        mono_method_get_flags: ['pointer', ['pointer', 'pointer']],
        mono_method_signature: ['pointer', ['pointer']],
        mono_signature_get_param_count: ['pointer', ['pointer']],
        mono_signature_get_return_type: ['pointer', ['pointer']],
        mono_signature_is_instance: ['pointer', ['pointer']],
        mono_method_get_class: ['pointer', ['pointer']],
        mono_compile_method: ['pointer', ['pointer']],
        mono_free_method: ['void', ['pointer']],
        mono_type_get_name: ['pointer', ['pointer']],
        mono_string_to_utf8: ['pointer', ['pointer']],
        mono_string_new: ['pointer', ['pointer', 'pointer']],
        mono_free: ['void', ['pointer']]
    };

    function monoResolveModule(preferredModule) {
        var names = [];
        if (preferredModule) { names.push(preferredModule); }
        for (var i = 0; i < MONO_MODULE_CANDIDATES.length; i++) {
            names.push(MONO_MODULE_CANDIDATES[i]);
        }
        for (var j = 0; j < names.length; j++) {
            var m = Process.findModuleByName(names[j]);
            if (m) { return m; }
        }
        return null;
    }

    /** 绑定 Mono 导出；返回缺失清单（不抛异常，缺谁报谁）。 */
    function monoBind() {
        var missing = [];
        monoFuncs = {};
        for (var i = 0; i < MONO_API_NAMES.length; i++) {
            var name = MONO_API_NAMES[i];
            var p = null;
            try { p = monoMod.findExportByName(name); } catch (e) { p = null; }
            if (!p) { missing.push(name); continue; }
            var sig = MONO_SIGS[name];
            if (!sig) {
                // 有导出但没登记签名：不硬猜参数类型（猜错就是崩溃），如实记为不可用
                missing.push(name + '(未登记签名)');
                continue;
            }
            try {
                monoFuncs[name] = new NativeFunction(p, sig[0], sig[1]);
            } catch (e) {
                missing.push(name + '(绑定失败：' + e.message + ')');
            }
        }
        return missing;
    }

    /**
     * 查出 System.String 的长度字段偏移（**不硬编码**）。
     *
     * 实测本版 Unity Mono 的字段名是 `_stringLength`（CoreCLR 是 `m_stringLength`，
     * 按那个名字查直接返回 NULL）。不同 Mono 分支命名不同，所以：
     * 先按候选名字查，都查不到就遍历字段找「名字含 stringLength 的 int 实例字段」。
     */
    function monoDiscoverStringLayout() {
        if (!monoFuncs.mono_class_from_name || !monoFuncs.mono_image_loaded) { return null; }
        var img = monoFuncs.mono_image_loaded(Memory.allocUtf8String('mscorlib'));
        if (!img || img.isNull()) { return null; }
        var klass = monoFuncs.mono_class_from_name(
            img, Memory.allocUtf8String('System'), Memory.allocUtf8String('String'));
        if (!klass || klass.isNull()) { return null; }

        if (monoFuncs.mono_class_get_field_from_name && monoFuncs.mono_field_get_offset) {
            for (var i = 0; i < MONO_STRING_LENGTH_FIELDS.length; i++) {
                var f = null;
                try {
                    f = monoFuncs.mono_class_get_field_from_name(
                        klass, Memory.allocUtf8String(MONO_STRING_LENGTH_FIELDS[i]));
                } catch (e) { f = null; }
                if (f && !f.isNull()) {
                    return { field: MONO_STRING_LENGTH_FIELDS[i],
                             offset: monoFuncs.mono_field_get_offset(f).toInt32(),
                             via: 'mono_class_get_field_from_name' };
                }
            }
        }
        // 回退：遍历字段找名字里含 stringLength 的实例字段
        // 注意守卫里**必须**包含 mono_field_get_offset：这一支会调它取偏移，
        // 漏掉它就会出现「守卫通过、调用却 undefined」的 TypeError
        // （实测踩过：主分支守卫里有、回退分支漏了）。
        if (monoFuncs.mono_class_get_fields && monoFuncs.mono_field_get_name &&
            monoFuncs.mono_field_get_offset) {
            var iter = Memory.alloc(Process.pointerSize);
            iter.writePointer(NULL);
            var guard = 0;
            for (;;) {
                var fld = monoFuncs.mono_class_get_fields(klass, iter);
                if (!fld || fld.isNull()) { break; }
                if (++guard > 200) { break; }
                var nm = cstr(monoFuncs.mono_field_get_name(fld)) || '';
                if (nm.toLowerCase().indexOf('stringlength') >= 0) {
                    return { field: nm,
                             offset: monoFuncs.mono_field_get_offset(fld).toInt32(),
                             via: '字段遍历（按名字匹配 stringLength）' };
                }
            }
        }
        return null;
    }

    function monoEnsureReady(preferredModule) {
        monoMod = monoResolveModule(preferredModule);
        if (!monoMod) {
            return fail('未找到 Mono 运行时模块（尝试过：' +
                        MONO_MODULE_CANDIDATES.join(' / ') + '）',
                        { retryable: true, process: Process.id });
        }
        mod = monoMod;
        var missing = monoBind();
        apiMissing = missing;
        fallbacks = [];

        // 就绪判断：域指针为空 = Mono 还没初始化好。
        // 这一步必须**先做**：没起来的域上调用任何 mono_* 都可能让游戏崩溃。
        if (!monoFuncs.mono_get_root_domain) {
            return fail('Mono 模块里没有 mono_get_root_domain 导出，无法确认运行时是否就绪',
                        { module: monoMod.name });
        }
        var dom = null;
        try { dom = monoFuncs.mono_get_root_domain(); } catch (e) { dom = null; }
        if (!dom || dom.isNull()) {
            return fail('Mono 运行时尚未初始化（mono_get_root_domain 返回 NULL），稍后重试即可',
                        { module: monoMod.name, retryable: true });
        }
        monoDomain = dom;

        // 附着当前线程。未附着的线程上调用 Mono 接口会让游戏崩溃，
        // 所以附着失败一律拒绝继续（与 IL2CPP 侧同样的理由）。
        try {
            if (monoFuncs.mono_thread_attach) {
                monoFuncs.mono_thread_attach(dom);
            } else if (monoFuncs.mono_jit_thread_attach) {
                fallbacks.push('mono_thread_attach 不可用，改用 mono_jit_thread_attach');
                monoFuncs.mono_jit_thread_attach(dom);
            } else {
                return fail('Mono 模块里既没有 mono_thread_attach 也没有 ' +
                            'mono_jit_thread_attach，无法把探针线程附着到 Mono 域',
                            { module: monoMod.name });
            }
        } catch (e) {
            return fail('无法把探针线程附着到 Mono 域：' + e.message +
                        '。未附着的线程上调用 Mono 接口会让游戏崩溃，' +
                        '因此这里拒绝继续（稍后重试通常就能成功）',
                        { module: monoMod.name, domain: dom.toString(), retryable: true });
        }

        // 字符串布局：查出来（查不到就如实标注，字符串相关能力降级但不假成功）
        //
        // 这里**必须**包 try/catch：探测失败只是「字符串参数读不出来」这一项降级，
        // 不该让整个 Mono 后端初始化失败。实测踩到的正是这个 ——
        // 探测里一个 TypeError 让 init 直接红，于是 Mono 游戏**所有**运行时工具
        // 全都不可用（本该只有字符串那一项降级）。
        try {
            monoStringLayout = monoDiscoverStringLayout();
        } catch (e) {
            monoStringLayout = null;
            fallbacks.push('字符串布局探测抛异常：' + e.message);
        }
        if (!monoStringLayout) {
            fallbacks.push('未能查出 System.String 的长度字段偏移，' +
                           'hook 事件里的字符串参数与 this_str 会返回 null');
        }

        // mono_type_get_name 的返回值是否堆分配在不同 Mono 版本不一致，
        // 直接 free 有崩游戏的风险 → 实测两次调用同一类型看指针是否相同，
        // 相同说明是缓存/静态串（不能 free），不同说明是新分配（可 free）。
        monoTypeNameAllocates = null;
        if (monoFuncs.mono_type_get_name && monoFuncs.mono_class_from_name &&
            monoFuncs.mono_image_loaded && monoFuncs.mono_string_to_utf8) {
            try {
                var timg = monoFuncs.mono_image_loaded(Memory.allocUtf8String('mscorlib'));
                if (timg && !timg.isNull()) {
                    var tcls = monoFuncs.mono_class_from_name(
                        timg, Memory.allocUtf8String('System'),
                        Memory.allocUtf8String('String'));
                    if (tcls && !tcls.isNull() && monoFuncs.mono_class_get_fields) {
                        var it = Memory.alloc(Process.pointerSize);
                        it.writePointer(NULL);
                        var f0 = monoFuncs.mono_class_get_fields(tcls, it);
                        if (f0 && !f0.isNull() && monoFuncs.mono_field_get_type) {
                            var ty = monoFuncs.mono_field_get_type(f0);
                            var n1 = monoFuncs.mono_type_get_name(ty);
                            var n2 = monoFuncs.mono_type_get_name(ty);
                            monoTypeNameAllocates = (n1.toString() !== n2.toString());
                        }
                    }
                }
            } catch (e) { monoTypeNameAllocates = null; }
        }
        if (monoTypeNameAllocates === false) {
            fallbacks.push('mono_type_get_name 返回的是缓存串（实测两次调用同址），' +
                           '因此不释放，避免误 free 导致崩溃');
        }

        attached = true;
        return {
            ok: true,
            engine: 'mono',
            module: monoMod.name,
            base: monoMod.base.toString(),
            size: monoMod.size,
            domain: dom.toString(),
            api_available: Object.keys(monoFuncs).length,
            api_missing: missing,
            fallbacks: fallbacks,
            string_layout: monoStringLayout
                ? { field: monoStringLayout.field, offset: monoStringLayout.offset,
                    via: monoStringLayout.via }
                : null
        };
    }

    /**
     * 枚举已加载的程序集（带 MonoImage* 指针，供类解析直接使用）。
     *
     * `monoImagesCache` 只在**一次 RPC 内**有效：RPC 入口统一清空（见文件末尾
     * 的 exports 包装）。Mono 的程序集是懒加载的，跨 RPC 缓存会让后加载的
     * `UnityEngine.*` 永远查不到，而报出来的错是「类不存在」。
     */
    function monoImages() {
        if (monoImagesCache) { return monoImagesCache; }
        if (!monoFuncs.mono_assembly_foreach || !monoFuncs.mono_assembly_get_image) {
            return [];
        }
        var out = [];
        var seenImg = {};    // 按 MonoImage* 去重
        // 回调必须保持引用：NativeCallback 被 GC 回收后再被 C 侧调用会直接崩
        var cb = new NativeCallback(function (asm, userData) {
            try {
                if (out.length > 5000) { return; }
                var img = monoFuncs.mono_assembly_get_image(asm);
                if (!img || img.isNull()) { return; }
                var pk = img.toString();
                if (seenImg[pk]) { return; }
                seenImg[pk] = true;
                var file = null;
                if (monoFuncs.mono_image_get_filename) {
                    try { file = cstr(monoFuncs.mono_image_get_filename(img)); }
                    catch (e) { file = null; }
                }
                // 内存加载的程序集（Unity/BepInEx 常见）拿到的不是路径，而是
                // `data-00000219A9D5A040` 这种合成名（内嵌映像指针）—— 实测。
                // 那种东西当「路径」显示会误导人，所以只在看着像路径时才带上。
                if (file && (/^data-/i.test(file) || !/[\\/]/.test(file))) {
                    file = null;
                }
                var rec = { image: img,
                            name: cstr(monoFuncs.mono_image_get_name(img)) };
                if (file) { rec.file = file; }
                if (monoFuncs.mono_image_get_table_rows) {
                    // TypeDef 表（表号 2）的行数就是类数。
                    // 实测与静态索引的类型数、微软 System.Reflection.Metadata 的真值三方一致。
                    rec.class_count = monoFuncs.mono_image_get_table_rows(img, 2).toInt32();
                }
                out.push(rec);
            } catch (e) { /* 单个程序集读失败不影响整体 */ }
        }, 'void', ['pointer', 'pointer']);
        monoKeep.push(cb);
        try {
            monoFuncs.mono_assembly_foreach(cb, ptr(0));
        } catch (e) {
            monoKeep.pop();
            return [];
        }
        monoImagesCache = out;
        return out;
    }

    function monoFindImages(name) {
        var imgs = monoImages();
        if (!name) { return imgs; }
        var needle = String(name).toLowerCase();
        var bare = needle.replace(/\.dll$/, '').replace(/\.exe$/, '');
        var hits = [];
        var i;
        for (i = 0; i < imgs.length; i++) {
            var n = (imgs[i].name || '').toLowerCase();
            if (n === needle || n === bare) { hits.push(imgs[i]); }
        }
        if (!hits.length) {
            // 退一步做子串匹配（与 IL2CPP 侧的 findImage 行为一致）
            for (i = 0; i < imgs.length; i++) {
                var m = (imgs[i].name || '').toLowerCase();
                if (m.indexOf(bare) >= 0) { hits.push(imgs[i]); }
            }
        }
        return hits;
    }

    /**
     * 把「静态索引给的命名」映射成 mono_class_from_name 认的 (namespace, name) 形式。
     *
     * 实测（Archon Soul / FlyingWormConsole3.LiteNetLib.NetPeer.IncomingFragments）：
     *   ✅ ("FlyingWormConsole3.LiteNetLib", "NetPeer/IncomingFragments")
     *   ❌ ("",            "NetPeer/IncomingFragments")
     *   ❌ ("",            "FlyingWormConsole3.LiteNetLib.NetPeer.IncomingFragments")
     *   ❌ ("FlyingWormConsole3.LiteNetLib", "NetPeer.IncomingFragments")
     *   ❌ ("FlyingWormConsole3.LiteNetLib", "IncomingFragments")
     * 即：**命名空间给到最外层类型所在的命名空间，嵌套链用 / 连接**。
     * 调用方可能给 (ns, 短名)、(ns, Outer.Inner) 或 (空, 完整点号名)，
     * 所以这里穷举拆分点，谁命中算谁（命中多个类才报歧义）。
     */
    function monoNameForms(namespace, name) {
        var out = [];
        var seen = {};
        function push(ns, nm) {
            if (!nm) { return; }
            var key = ns + '|' + nm;
            if (seen[key]) { return; }
            seen[key] = true;
            out.push([ns, nm]);
        }
        var nm = String(name || '');
        if (!nm) { return out; }
        push(namespace || '', nm);
        push(namespace || '', nm.replace(/\./g, '/'));
        var dotted = (namespace ? namespace + '.' + nm : nm);
        var parts = dotted.split('.');
        for (var i = 1; i < parts.length; i++) {
            push(parts.slice(0, i).join('.'), parts.slice(i).join('/'));
        }
        return out;
    }

    /**
     * 按 TypeDef 行号枚举一个映像里的全部类，供**短名兜底**使用。
     *
     * 为什么需要它：IL2CPP 侧有全量类索引（`il2cpp_image_get_class_count` /
     * `il2cpp_image_get_class`），所以 `runtime_class_info(name="Time")` 这种
     * **不给命名空间**的用法能命中 `UnityEngine.Time`。Mono 没有
     * `mono_image_get_class` 这类接口，于是短名解析一直缺失 ——
     * 实测 `monoNameForms("", "Time")` 只生成 `("", "Time")`，
     * 而类在 `UnityEngine.Time`，结果报「运行时没找到类 Time」，
     * 与工具文档承诺的「不给命名空间也能按短名解析」不符（两个引擎不一致）。
     *
     * Mono 的可行路径是**按 TypeDef 行号取类**：
     *   mono_image_get_table_info(image, 2)，2 即 MONO_TABLE_TYPEDEF
     *     → mono_table_info_get_rows(info) 得到行数
     *   → 第 i 行的 token 是 `0x02000000 | i`，交给 `mono_class_get` 拿到 MonoClass。
     * 用 `mono_class_get` 而不是 `mono_class_get_full`：后者要多传一个 MonoError*
     * 结构体，传 NULL 是否安全没有保证；前者只需要 (image, token)。
     * 这三个导出在**四个 Mono 样本里都核实存在**（见 verify_mono_runtime.py §2）。
     *
     * 代价：一次全量扫描是几万次 Mono 调用（实测样本 TypeDef 行数合计约 2 万），
     * 所以只在精确写法查不到时调用，且按 RPC 失效（懒加载会带来新类）。
     */
    function monoBuildTypeIndex(imageName) {
        var key = String(imageName || '');
        if (monoTypeIndexCache && monoTypeIndexCache.key === key) {
            return monoTypeIndexCache.rows;
        }
        var rows = [];
        if (!monoFuncs.mono_image_get_table_info || !monoFuncs.mono_table_info_get_rows ||
            !monoFuncs.mono_class_get || !monoFuncs.mono_class_get_name) {
            return rows;
        }
        var imgs = monoFindImages(imageName);
        for (var i = 0; i < imgs.length; i++) {
            var total = 0;
            try {
                var ti = monoFuncs.mono_image_get_table_info(imgs[i].image, 2);
                if (!ti || ti.isNull()) { continue; }
                total = monoFuncs.mono_table_info_get_rows(ti).toInt32();
            } catch (e) { continue; }
            for (var r = 1; r <= total; r++) {
                var k = null;
                try { k = monoFuncs.mono_class_get(imgs[i].image, 0x02000000 | r); }
                catch (e) { continue; }
                if (!k || k.isNull()) { continue; }
                var nm = null;
                try { nm = cstr(monoFuncs.mono_class_get_name(k)); } catch (e) { nm = null; }
                if (!nm) { continue; }
                var ns = '';
                if (monoFuncs.mono_class_get_namespace) {
                    try {
                        ns = cstr(monoFuncs.mono_class_get_namespace(k)) || '';
                    } catch (e) { ns = ''; }
                }
                rows.push({ klass: k, image: imgs[i], name: nm, namespace: ns });
            }
        }
        monoTypeIndexCache = { key: key, rows: rows };
        return rows;
    }

    /**
     * 候选去重键：**程序集名** + 命名空间 + 短名。
     *
     * 为什么不用「程序集文件路径」：实测（Archon Soul，BepInEx 内存加载）
     * `mono_image_get_filename` 返回的是 `data-00000219A9D5A040` 这种
     * **合成名**（内嵌映像指针），不是路径 —— 按路径去重根本不会命中。
     *
     * 为什么需要去重：实测游戏运行约 90 秒后，`UnityEngine.Time` 会被枚举出
     * **两条完全一样**的顶层记录：
     *     UnityEngine.CoreModule TypeDef 行 740 token=0x20002e4
     *     ns='UnityEngine' 顶层=True  klass=0x25ff6b54948
     *     UnityEngine.CoreModule TypeDef 行 740 token=0x20002e4
     *     ns='UnityEngine' 顶层=True  klass=0x25f920a9ab0
     * 行号、token、命名空间、程序集名全都相同，只有 klass 指针不同 ——
     * 即**同一个程序集被加载出了两个映像**（同一个 dll 加载两次）。
     * 不去重的话，一次本可解析的查询会变成「命中 2 个同名类」，
     * 而且调用方**无法消解**：两条的 image / namespace / full_name 都一模一样。
     *
     * 代价说清楚：两个**不同**程序集若恰好同名且含同名同命名空间的类，
     * 会被合并成一条。实测没遇到过；而上面那种重复是实测到的真实情况，
     * 且它导致的错误无法由调用方消解，所以选择合并（保留先枚举到的那条）。
     */
    function monoHitKey(imgRec, form) {
        var n = (imgRec && imgRec.name) || '';
        return String(n).toLowerCase() + '\u0000' + (form[0] || '') +
               '\u0000' + (form[1] || '');
    }

    /** 解析类；返回 [{klass, image}]（可能多个 → 由调用方报歧义）。 */
    function monoResolveClass(imageName, namespace, name) {
        var imgs = monoFindImages(imageName);
        var forms = monoNameForms(namespace, name);
        var hits = [];
        var seenPtr = {};
        var seenDup = {};
        for (var i = 0; i < imgs.length; i++) {
            for (var j = 0; j < forms.length; j++) {
                var k = null;
                try {
                    k = monoFuncs.mono_class_from_name(
                        imgs[i].image,
                        Memory.allocUtf8String(forms[j][0]),
                        Memory.allocUtf8String(forms[j][1]));
                } catch (e) { k = null; }
                if (!k || k.isNull()) { continue; }
                var key = k.toString();
                if (seenPtr[key]) { continue; }   // 同一类被多种写法命中：只算一次
                seenPtr[key] = true;
                // 同一个程序集文件若被枚举出两个 MonoImage*，这里会拿到两个
                // 不同的 klass 指针 → 上面那层挡不住 → 再用文件路径挡一次。
                var dk = monoHitKey(imgs[i], forms[j]);
                if (seenDup[dk]) { continue; }
                seenDup[dk] = true;
                hits.push({ klass: k, image: imgs[i], form: forms[j] });
            }
        }
        if (hits.length) { return hits; }

        // 精确写法一个都没命中 → 按短名全量扫描兜底。
        // 只匹配**短名**（名字最后一个点号之后那段），命名空间给了就按它过滤。
        // 这样 `runtime_class_info(name="Time")` 也能命中 `UnityEngine.Time`，
        // 与 IL2CPP 侧的行为一致；命中多个同名类仍然报歧义，不替调用方猜。
        var wantName = String(name || '');
        if (!wantName) { return hits; }
        var shortName = wantName.split('.').pop();
        var wantNs = String(namespace || '');
        // 工具文档承诺的是「按短名在**顶层类型**里解析」，所以只给短名时
        // 不接受嵌套类型。否则 `Time` 会同时命中嵌套的
        // `UnityEngine.InputForUI.InputManagerProvider.Time` 而报歧义 ——
        // 实测正是如此，把一次本可成功的查询变成了要人工消歧。
        var topLevelOnly = wantName.indexOf('.') < 0;
        var idx = monoBuildTypeIndex(imageName);
        for (var m = 0; m < idx.length; m++) {
            var e = idx[m];
            if (e.name !== shortName) { continue; }
            if (wantNs && e.namespace !== wantNs) { continue; }
            if (topLevelOnly && monoFuncs.mono_class_get_nesting_type) {
                var outer = null;
                try { outer = monoFuncs.mono_class_get_nesting_type(e.klass); }
                catch (err) { outer = null; }
                if (outer && !outer.isNull()) { continue; }
            }
            var kk = e.klass.toString();
            if (seenPtr[kk]) { continue; }
            seenPtr[kk] = true;
            var dk2 = monoHitKey(e.image, [e.namespace || '', e.name]);
            if (seenDup[dk2]) { continue; }
            seenDup[dk2] = true;
            hits.push({ klass: e.klass, image: e.image,
                        form: [e.namespace, e.name], via: '短名扫描' });
        }
        return hits;
    }

    function monoResolveClassOrFail(imageName, namespace, name) {
        if (!monoFuncs.mono_class_from_name) {
            return fail('mono_class_from_name 不可用，无法按名解析类');
        }
        var hits = monoResolveClass(imageName, namespace, name);
        if (!hits.length) {
            var imgs = monoFindImages(imageName);
            return fail('运行时没找到类 ' +
                        (namespace ? namespace + '.' : '') + name +
                        (imageName ? '（限程序集 ' + imageName + '）' : ''), {
                searched_images: imgs.length,
                image_hint: imgs.length ? null
                    : '指定/默认的程序集没有加载；用 list_images 看已加载的程序集',
                note: '静态索引里有、运行时没有，通常意味着这个类在本局游戏里没被加载' +
                      '（Unity 会裁剪未使用的程序集）；也可能是命名空间/嵌套名给法不对。' +
                      '**还有一种很常见的情况：游戏刚启动，Mono 的程序集还在懒加载**' +
                      '（实测刚注入时只能枚举到十来个 BepInEx 自己的程序集，' +
                      'UnityEngine.* 要过几秒才出现）——这种情况稍后重试即可，' +
                      '不必改写类名。已加载程序集数见 searched_images，' +
                      '它应当随游戏运行**变多**；若始终不变，才是真的没有这个类'
            });
        }
        if (hits.length > 1) {
            return fail('类名有歧义：命中 ' + hits.length + ' 个同名类', {
                ambiguous: true,
                candidates: hits.map(function (h) {
                    return { image: h.image.name,
                             full_name: monoClassFullName(h.klass),
                             klass: h.klass.toString(),
                             via: h.via || '精确写法' };
                })
            });
        }
        return { ok: true, klass: hits[0].klass, image: hits[0].image };
    }

    /** 类的完整名（嵌套类型用点号连接，与静态索引的 nested_name 对齐）。 */
    function monoClassFullName(klass) {
        var parts = [];
        var cur = klass;
        var guard = 0;
        while (cur && !cur.isNull() && guard++ < 32) {
            var nm = null;
            try { nm = cstr(monoFuncs.mono_class_get_name(cur)); } catch (e) { nm = null; }
            if (nm) { parts.unshift(nm); }
            var ns = null;
            try { ns = cstr(monoFuncs.mono_class_get_namespace(cur)); } catch (e) { ns = null; }
            if (ns && ns.length) { parts.unshift(ns); break; }
            var outer = null;
            try {
                outer = monoFuncs.mono_class_get_nesting_type
                    ? monoFuncs.mono_class_get_nesting_type(cur) : null;
            } catch (e) { outer = null; }
            if (!outer || outer.isNull()) { break; }
            cur = outer;
        }
        return parts.length ? parts.join('.') : null;
    }

    function monoParentOf(klass) {
        if (!monoFuncs.mono_class_get_parent) { return null; }
        try { return monoFuncs.mono_class_get_parent(klass); } catch (e) { return null; }
    }

    function monoClassAttrs(klass) {
        var out = { full_name: monoClassFullName(klass) };
        var par = monoParentOf(klass);
        out.parent = (par && !par.isNull()) ? monoClassFullName(par) : null;
        if (monoFuncs.mono_class_get_flags) {
            var fl = null;
            try { fl = monoFuncs.mono_class_get_flags(klass).toUInt32(); } catch (e) { fl = null; }
            if (fl !== null) {
                out.flags = hex32(fl);
                // ECMA-335 TypeAttributes：0x20 = Interface，0x80 = Abstract。
                // 本版 Mono 没有 mono_class_is_interface / mono_class_is_abstract 导出
                // （四个样本一致），所以从 flags 位判断。
                out.is_interface = (fl & 0x20) !== 0;
                out.is_abstract = (fl & 0x80) !== 0;
            }
        }
        if (monoFuncs.mono_class_is_enum) {
            try { out.is_enum = monoFuncs.mono_class_is_enum(klass).toInt32() !== 0; }
            catch (e) { /* 拿不到就不给这个字段 */ }
        }
        if (monoFuncs.mono_class_is_valuetype) {
            try { out.is_value_type = monoFuncs.mono_class_is_valuetype(klass).toInt32() !== 0; }
            catch (e) { /* 同上 */ }
        }
        if (monoFuncs.mono_class_instance_size) {
            try { out.instance_size = monoFuncs.mono_class_instance_size(klass).toInt32(); }
            catch (e) { /* 同上 */ }
        }
        if (monoFuncs.mono_class_get_type_token) {
            try { out.token = hex32(monoFuncs.mono_class_get_type_token(klass).toUInt32()); }
            catch (e) { /* 同上 */ }
        }
        if (monoFuncs.mono_class_num_fields) {
            try { out.own_field_count = monoFuncs.mono_class_num_fields(klass).toInt32(); }
            catch (e) { /* 同上 */ }
        }
        if (monoFuncs.mono_class_num_methods) {
            try { out.method_count = monoFuncs.mono_class_num_methods(klass).toInt32(); }
            catch (e) { /* 同上 */ }
        }
        return out;
    }

    /** Mono 的类型名。注意与 IL2CPP 的差异：Mono 给的是短名（int / string）。 */
    function monoTypeName(typePtr) {
        if (!typePtr || typePtr.isNull() || !monoFuncs.mono_type_get_name) { return null; }
        var p = null;
        try { p = monoFuncs.mono_type_get_name(typePtr); } catch (e) { return null; }
        var s = cstr(p);
        // 只有实测确认是新分配（两次调用不同址）时才释放；否则可能是缓存串，free 会崩
        if (monoTypeNameAllocates === true && monoFuncs.mono_free && p && !p.isNull()) {
            try { monoFuncs.mono_free(p); } catch (e) { /* 释放失败不影响结果 */ }
        }
        return s;
    }

    /** 只枚举**本类自己声明**的字段（继承字段由 fieldsWithInheritance 负责）。 */
    function monoFieldsOfOwn(klass, wantStatic) {
        if (!monoFuncs.mono_class_get_fields) { return []; }
        var iter = Memory.alloc(Process.pointerSize);
        iter.writePointer(NULL);
        var out = [];
        var guard = 0;
        for (;;) {
            var f = null;
            try { f = monoFuncs.mono_class_get_fields(klass, iter); }
            catch (e) { break; }
            if (!f || f.isNull()) { break; }
            if (++guard > 100000) { break; }
            var flags = null;
            if (monoFuncs.mono_field_get_flags) {
                try { flags = monoFuncs.mono_field_get_flags(f).toUInt32(); } catch (e) { flags = null; }
            }
            // ECMA-335 FieldAttributes：0x10 = Static
            var isStatic = (flags !== null) ? ((flags & 0x10) !== 0) : null;
            if (wantStatic === true && isStatic !== true) { continue; }
            if (wantStatic === false && isStatic === true) { continue; }
            var rec = {
                name: monoFuncs.mono_field_get_name ? cstr(monoFuncs.mono_field_get_name(f)) : null,
                type: (monoFuncs.mono_field_get_type && monoFuncs.mono_type_get_name)
                    ? monoTypeName(monoFuncs.mono_field_get_type(f)) : null,
                token: null,
                flags: (flags !== null) ? hex32(flags) : null,
                is_static: isStatic
            };
            if (monoFuncs.mono_field_get_offset) {
                try {
                    rec.offset = monoFuncs.mono_field_get_offset(f).toInt32();
                    rec.offset_via = 'mono_field_get_offset';
                } catch (e) {
                    rec.offset = null;
                    rec.offset_via = null;
                }
            } else {
                rec.offset = null;
                rec.offset_via = null;
            }
            out.push(rec);
        }
        return out;
    }

    function monoMethodsOf(klass) {
        if (!monoFuncs.mono_class_get_methods) { return []; }
        var iter = Memory.alloc(Process.pointerSize);
        iter.writePointer(NULL);
        var iflags = Memory.alloc(4);   // mono_method_get_flags 的第二个参数是出参
        var out = [];
        var guard = 0;
        for (;;) {
            var m = null;
            try { m = monoFuncs.mono_class_get_methods(klass, iter); }
            catch (e) { break; }
            if (!m || m.isNull()) { break; }
            if (++guard > 100000) { break; }
            var rec = {
                name: monoFuncs.mono_method_get_name ? cstr(monoFuncs.mono_method_get_name(m)) : null,
                method_info: m.toString()
            };
            if (monoFuncs.mono_method_get_token) {
                try { rec.token = hex32(monoFuncs.mono_method_get_token(m).toUInt32()); }
                catch (e) { rec.token = null; }
            } else {
                rec.token = null;
            }
            // 参数个数与返回类型：本版 Mono 没有 mono_method_get_param_count，
            // 一律走签名（mono_method_signature + mono_signature_*）
            var sig = null;
            try { sig = monoFuncs.mono_method_signature(m); } catch (e) { sig = null; }
            if (sig && !sig.isNull()) {
                if (monoFuncs.mono_signature_get_param_count) {
                    try { rec.param_count = monoFuncs.mono_signature_get_param_count(sig).toInt32(); }
                    catch (e) { rec.param_count = null; }
                }
                if (monoFuncs.mono_signature_get_return_type) {
                    try {
                        rec.return_type = monoTypeName(
                            monoFuncs.mono_signature_get_return_type(sig));
                    } catch (e) { rec.return_type = null; }
                }
                if (monoFuncs.mono_signature_is_instance) {
                    try {
                        rec.is_instance = monoFuncs.mono_signature_is_instance(sig).toInt32() !== 0;
                    } catch (e) { /* 交给 flags 兜底 */ }
                }
            }
            if (monoFuncs.mono_method_get_flags) {
                try {
                    var fl = monoFuncs.mono_method_get_flags(m, iflags).toUInt32();
                    rec.flags = hex32(fl);
                    rec.is_static = (fl & 0x0010) !== 0;   // MethodAttributes.Static
                    if (rec.is_instance === undefined) { rec.is_instance = !rec.is_static; }
                } catch (e) { /* 同上 */ }
            }
            // is_generic：本版 Mono 的导出表里没有直接判断泛型的函数
            // （mono_method_get_generic_container 不在导出表），拿不到就给 null，不猜
            rec.is_generic = null;
            // Hook 落点：mono_compile_method 触发 JIT 并给出真实机器码地址
            var mp = null;
            try { mp = monoFuncs.mono_compile_method(m); } catch (e) { mp = null; }
            if (mp && !mp.isNull()) {
                var d = describeAddress(mp);
                rec.pointer = d.address;
                rec.in_module = d.in_module;
                if (d.rva !== undefined) { rec.rva = d.rva; rec.rva_hex = d.rva_hex; }
            } else {
                rec.pointer = null;
                rec.note = '无代码地址（抽象/接口方法或未被 JIT 的方法：' +
                           'mono_compile_method 返回 NULL）';
            }
            out.push(rec);
        }
        return out;
    }

    /** 从托管字符串指针取 JS 字符串（长度偏移在初始化时查出来）。 */
    function monoStringOf(p) {
        if (p === null || p === undefined) { return null; }
        if (!monoStringLayout) { return null; }
        try {
            var q = ptr(p);
            if (q.isNull()) { return null; }
            var len = q.add(monoStringLayout.offset).readS32();
            if (len < 0 || len > 0x100000) { return null; }
            // 字符数组紧跟在长度字段之后（本版 mscorlib：_stringLength @16 → chars @20）
            return q.add(monoStringLayout.offset + 4).readUtf16String(len);
        } catch (e) {
            return null;
        }
    }

    function monoGcControl(disable) {
        // 本版 Mono **没有** mono_gc_disable / mono_gc_enable 导出（四个样本一致核实），
        // 所以做不到「暂停 GC」。如实报不支持 —— 不假装成功。
        return fail('本版 Mono 运行时没有 mono_gc_disable / mono_gc_enable 导出，' +
                    '无法暂停/恢复 GC（mono_gc_collect 只能强制回收一次，' +
                    '起不到「Hook 期间不回收」的作用）', {
            engine: 'mono',
            unsupported: true,
            note: 'Hook 期间如需保护对象，请用 read_memory 立即取走需要的字节，' +
                  '或缩短观察窗口'
        });
    }

    // ================================================================ 实例枚举

    /**
     * 枚举某个类在堆上的**活对象实例**（IL2CPP）。
     *
     * 为什么需要它：`read_field_value` / `set_field_value` 都要 `instance=`，
     * 而在此之前，拿实例的唯一办法是「Hook 一个恰好会收到该对象的函数，
     * 从 args[0] 里捡」—— 那是**碰运气**，与本项目「元数据优先」相悖。
     *
     * 走的是 Unity 自家内存分析器用的 GC liveness 导出：
     *
     *     stopWorld()                        // il2cpp_stop_gc_world
     *     state = livenessAllocateStruct(klass, 0, cb, NULL, reallocCb)
     *     livenessCalculationFromStatics(state)
     *     livenessFinalize(state)
     *     startWorld()                       // il2cpp_start_gc_world
     *     livenessFreeStruct(state)
     *
     * 这是 `frida-il2cpp-bridge` 的 `gc.choose` 的 `allocate_struct` 分支
     * （另一分支用 `liveness_calculation_begin`，**本机样本上不存在**）。
     *
     * 三条实测得来的注意点：
     *
     * 1. **不能对值类型调用**（结构体没有堆实例）。调用方应先查
     *    `il2cpp_class_is_valuetype`，是的话直接返回空而不是硬跑。
     * 2. **stopWorld 之后必须 startWorld**，否则一次异常就把游戏冻住。
     *    所以下面用 try/finally 保证一定恢复。
     * 3. **realloc 回调返回的指针必须被 JS 侧长期持有**，否则会被 GC →
     *    游戏 use-after-free。所以从一个**预先分配、闭包持有**的内存池里
     *    切分，而不是回调里临时 `Memory.alloc`。
     *    另外只在「句柄非空且 size==0」时 free —— 实测第一版对
     *    NULL/0xffff... 句柄调 free 会报 access violation。
     */
    function findInstancesIL2CPP(klass, maxCount) {
        var miss = [];
        var need = ['il2cpp_unity_liveness_allocate_struct',
                    'il2cpp_unity_liveness_calculation_from_statics',
                    'il2cpp_unity_liveness_finalize',
                    'il2cpp_unity_liveness_free_struct',
                    'il2cpp_stop_gc_world', 'il2cpp_start_gc_world'];
        for (var i = 0; i < need.length; i++) {
            if (!funcs[need[i]]) { miss.push(need[i]); }
        }
        if (miss.length) {
            return fail('本版 IL2CPP 运行时缺少实例枚举所需导出：' +
                        miss.join(', ') +
                        '（这是**版本差异**，不是探针缺陷；' +
                        '可改用 eval_script + il2cpp_gc_foreach_heap 兜底）', {
                engine: 'il2cpp', missing: miss, unsupported: true
            });
        }

        // 值类型没有堆实例 —— 明确说明，而不是返回空列表让人以为「没有」
        //
        // ⚠️ 必须 `.toInt32() !== 0`：这些 `is_*` 导出在本探针里统一按
        // 'pointer' 返回类型包装（见 buildFunctions 的说明），于是 C 的
        // `false` 会变成一个**非空但内容为 0** 的 NativePointer 对象，
        // 而 JS 里任何对象都是 truthy —— 直接 `if (fn(k))` 会把
        // **每个类**都判成值类型（实测踩到：`UnityEngine.Transform`
        // 这种明确的引用类型也被拒了）。
        if (funcs.il2cpp_class_is_valuetype) {
            try {
                if (funcs.il2cpp_class_is_valuetype(klass).toInt32() !== 0) {
                    return fail('目标是**值类型**（struct），它没有堆实例，' +
                                '无法枚举实例；请用 read_field_value 直接读持有它的字段', {
                        engine: 'il2cpp', unsupported: true, is_value_type: true
                    });
                }
            } catch (e) { /* 查不出来就按引用类型继续 */ }
        }

        // 目标类的短名，用于区分「实例就是该类本身」还是它的派生类
        var targetShortName = null;
        try {
            if (funcs.il2cpp_class_get_name) {
                targetShortName = cstr(funcs.il2cpp_class_get_name(klass));
            }
        } catch (e) { targetShortName = null; }

        var matches = [];
        var overflow = false;
        var poolExhausted = false;
        var cbError = null;

        var cb = new NativeCallback(function (objects, size) {
            try {
                for (var i = 0; i < size; i++) {
                    if (matches.length >= maxCount) { overflow = true; return; }
                    var p = objects.add(i * Process.pointerSize).readPointer();
                    if (p && !p.isNull()) { matches.push(p); }
                }
            } catch (e) { cbError = String(e.message || e); }
        }, 'void', ['pointer', 'int', 'pointer']);

        // 池：闭包持有 ⇒ 生命周期覆盖整个调用，不会被 GC 掉
        var POOL_SIZE = 8 * 1024 * 1024;
        var pool = Memory.alloc(POOL_SIZE);
        var poolUsed = 0;
        function poolAlloc(size) {
            var w = Number(size);
            if (!w || w < 0) { return NULL; }
            w = (w + 15) & ~15;
            if (poolUsed + w > POOL_SIZE) { poolExhausted = true; return NULL; }
            var p = pool.add(poolUsed);
            poolUsed += w;
            return p;
        }

        var reallocCb = new NativeCallback(function (handle, size) {
            try {
                // 只在「非空句柄 + 明确要求释放(size==0)」时 free。
                // 实测第一版无条件对句柄调 free，会拿 NULL/0xffff… 去解引用，
                // 报 `access violation accessing 0xffffffffffffffff`。
                if (handle && !handle.isNull() && size.compare(0) === 0) {
                    if (funcs.il2cpp_free) { funcs.il2cpp_free(handle); }
                    return NULL;
                }
                return poolAlloc(size);
            } catch (e) { cbError = String(e.message || e); return NULL; }
        }, 'pointer', ['pointer', 'size_t', 'pointer']);

        var state = NULL;
        var stopped = false;
        var startWorldError = null;
        try {
            funcs.il2cpp_stop_gc_world();
            stopped = true;
            state = funcs.il2cpp_unity_liveness_allocate_struct(
                klass, 0, cb, NULL, reallocCb);
            if (!state || state.isNull()) {
                return fail('liveness allocate_struct 返回空 —— 该运行时可能不支持此路径',
                            { engine: 'il2cpp', unsupported: true });
            }
            funcs.il2cpp_unity_liveness_calculation_from_statics(state);
            funcs.il2cpp_unity_liveness_finalize(state);
        } catch (e) {
            return fail('实例枚举失败：' + (e.message || e), {
                engine: 'il2cpp', counted: matches.length
            });
        } finally {
            // **必须**恢复 GC world：否则一次异常就把游戏冻住
            if (stopped) {
                try { funcs.il2cpp_start_gc_world(); }
                catch (e) { startWorldError = String(e.message || e); }
            }
            if (state && !state.isNull()) {
                try { funcs.il2cpp_unity_liveness_free_struct(state); } catch (e) { }
            }
        }

        var out = {
            ok: true,
            engine: 'il2cpp',
            count: matches.length,
            pointers: matches.map(function (p) { return p.toString(); }),
            overflow: overflow
        };

        // 每个实例**真实**的类名。
        //
        // 为什么必须给：liveness 拿的是「该类**及其派生类**」的实例 ——
        // 实测 `find_instances(UnityEngine.Transform)` 返回的全是
        // `RectTransform`（Unity 里 UI 节点都是 RectTransform）。
        // 这不是缺陷（语义如此），但**是个陷阱**：若调用方拿 Transform
        // 的字段偏移去读一个 RectTransform，偏移可能不对。
        // 所以把真实类名与「是否就是目标类本身」一并给出，让调用方能自证。
        if (funcs.il2cpp_object_get_class && funcs.il2cpp_class_get_name) {
            var exact = 0;
            var names = {};
            out.instances = matches.map(function (p) {
                var nm = null;
                try {
                    var k = funcs.il2cpp_object_get_class(p);
                    if (k && !k.isNull()) { nm = cstr(funcs.il2cpp_class_get_name(k)); }
                } catch (e) { nm = null; }
                if (nm) {
                    names[nm] = (names[nm] || 0) + 1;
                    if (nm === targetShortName) { exact++; }
                }
                return { pointer: p.toString(), class_name: nm };
            });
            out.class_name_counts = names;
            out.exact_class_count = exact;
            out.derived_count = matches.length - exact;
            if (out.derived_count > 0) {
                out.note = '返回的实例包含 ' + out.derived_count + ' 个**派生类**实例' +
                           '（见 instances[].class_name）。用目标类的字段偏移去读' +
                           '派生类实例可能出错，请按 class_name 甄别。';
            }
        }
        if (poolExhausted) { out.pool_exhausted = true; }
        if (cbError) { out.callback_error = cbError; }
        if (startWorldError) {
            // 恢复 GC world 失败是**严重**事件（游戏可能卡住），必须报出来
            out.warning = '恢复 GC world 失败：' + startWorldError +
                          '（游戏可能卡住，建议重启后重新注入）';
        }
        return out;
    }

    // ================================================================ 实例枚举

    /**
     * 枚举某个类在堆上的**活对象实例**（Mono）。
     *
     * 与 IL2CPP 不同：Mono **没有**「按类枚举实例」的 liveness 导出。
     * 唯一相关的是 `mono_gc_walk_heap`，而它的官方头文件里写着一条硬限制
     * （原文，注意这里不能写成块注释以免提前闭合）：
     *
     *     「heap walking is only valid in the pre-stop-world event callback」
     *     MONO_API int mono_gc_walk_heap (int flags, MonoGCReferences callback, void *data);
     *
     * 也就是说：**从 Frida 线程直接调它，官方语义下并不保证有效**。
     * 这里如实实现并返回真实结果 —— 能拿到就报数，拿不到就明确说
     * 「本运行时/本时机不可用」，**不假装成功**，也不拿空列表冒充「没有实例」。
     *
     * 回调签名（mono-gc.h）：
     *     typedef int (*MonoGCReferences)(MonoObject *obj, MonoClass *klass,
     *         uintptr_t size, uintptr_t num, MonoObject **refs,
     *         uintptr_t *offsets, void *data);
     * 返回非 0 继续遍历，返回 0 停止。
     */
    function findInstancesMono(klass, maxCount) {
        if (!monoFuncs.mono_gc_walk_heap) {
            return fail('本版 Mono 运行时没有 mono_gc_walk_heap 导出，' +
                        '无法枚举实例（这是**版本差异**，不是探针缺陷）。' +
                        '替代路径：Hook 一个会经过该对象的方法，从 args[0] 取实例',
                        { engine: 'mono', unsupported: true });
        }
        if (!monoFuncs.mono_object_get_class) {
            return fail('缺少 mono_object_get_class，无法按类过滤实例',
                        { engine: 'mono', unsupported: true });
        }

        var matches = [];
        var scanned = 0;
        var overflow = false;
        var cbError = null;
        var target = klass;

        var cb = new NativeCallback(function (obj, k, size, num, refs, offsets, data) {
            try {
                if (!obj || obj.isNull()) { return 1; }
                scanned++;
                if (scanned > 4000000) { return 0; }   // 防御性上限
                var kk = k;
                if (!kk || kk.isNull()) {
                    kk = monoFuncs.mono_object_get_class(obj);
                }
                if (kk && !kk.isNull() && kk.equals(target)) {
                    if (matches.length >= maxCount) { overflow = true; return 0; }
                    matches.push(obj);
                }
            } catch (e) { cbError = String(e.message || e); return 0; }
            return 1;
        }, 'int', ['pointer', 'pointer', 'size_t', 'size_t',
                   'pointer', 'pointer', 'pointer']);

        var rc = null;
        try {
            // 返回类型按 pointer 包装（全文件统一），所以这里显式截成 int
            var rcRaw = monoFuncs.mono_gc_walk_heap(0, cb, NULL);
            rc = (rcRaw && rcRaw.toInt32) ? rcRaw.toInt32() : Number(rcRaw);
        } catch (e) {
            return fail('mono_gc_walk_heap 调用失败：' + (e.message || e), {
                engine: 'mono', scanned: scanned, counted: matches.length
            });
        }

        // rc == 0 且一个对象都没遍历到 ⇒ 官方那条限制生效了（不在
        // pre-stop-world 回调里）。如实报「不可用」，而不是报「0 个实例」。
        if (!matches.length && scanned === 0) {
            return fail('mono_gc_walk_heap 返回 ' + rc + ' 且未遍历到任何对象。' +
                        '按 Mono 官方头文件，堆遍历**只在 pre-stop-world 回调内有效**，' +
                        '从外部线程调用不保证可用 —— 本次记为「本时机不可用」，' +
                        '**不能**据此断定该实例不存在。' +
                        '替代路径：Hook 会经过该对象的方法，从 args[0] 取实例',
                        { engine: 'mono', unsupported: true, rc: rc,
                          scanned: scanned });
        }

        var out = {
            ok: true,
            engine: 'mono',
            count: matches.length,
            scanned_objects: scanned,
            pointers: matches.map(function (p) { return p.toString(); }),
            overflow: overflow
        };
        if (cbError) { out.callback_error = cbError; }
        return out;
    }

    // ================================================================ 后端装配

    /** IL2CPP 的父类指针（fieldsWithInheritance 用）。 */
    function parentOf(klass) {
        if (!funcs.il2cpp_class_get_parent) { return null; }
        try { return funcs.il2cpp_class_get_parent(klass); } catch (e) { return null; }
    }

    /** IL2CPP 的类属性（原 classInfo 里的内联逻辑，抽出来与 Mono 对齐）。 */
    function classAttrsOf(klass) {
        var out = { full_name: fullNameOf(klass) };
        var par = parentOf(klass);
        out.parent = (par && !par.isNull()) ? fullNameOf(par) : null;
        if (funcs.il2cpp_class_get_flags) {
            out.flags = hex32(funcs.il2cpp_class_get_flags(klass).toUInt32());
        }
        if (funcs.il2cpp_class_is_enum) {
            out.is_enum = funcs.il2cpp_class_is_enum(klass).toInt32() !== 0;
        }
        if (funcs.il2cpp_class_is_valuetype) {
            out.is_value_type = funcs.il2cpp_class_is_valuetype(klass).toInt32() !== 0;
        }
        if (funcs.il2cpp_class_is_interface) {
            out.is_interface = funcs.il2cpp_class_is_interface(klass).toInt32() !== 0;
        }
        if (funcs.il2cpp_class_is_abstract) {
            out.is_abstract = funcs.il2cpp_class_is_abstract(klass).toInt32() !== 0;
        }
        if (funcs.il2cpp_class_instance_size) {
            out.instance_size = funcs.il2cpp_class_instance_size(klass).toInt32();
        }
        return out;
    }

    var BE_IL2CPP = {
        kind: 'il2cpp',
        label: 'IL2CPP',
        moduleCandidates: MODULE_CANDIDATES,
        ensureReady: function (pm) { return ensureReady(pm); },
        allImages: function () { return allImages(); },
        resolveClassOrFail: function (img, ns, name) {
            return resolveClassOrFail(img, ns, name);
        },
        classAttrs: classAttrsOf,
        fieldsOfOwn: function (k, wantStatic) { return fieldsOf(k, wantStatic); },
        methodsOf: methodsOf,
        fullNameOf: fullNameOf,
        parentOf: parentOf,
        stringOf: il2cppString,
        gcControl: function (disable) {
            if (disable) {
                if (!funcs.il2cpp_gc_disable) {
                    return fail('il2cpp_gc_disable 不可用，无法暂停 GC');
                }
                funcs.il2cpp_gc_disable();
                gcDisabled = true;
            } else {
                if (!funcs.il2cpp_gc_enable) {
                    return fail('il2cpp_gc_enable 不可用，无法恢复 GC');
                }
                funcs.il2cpp_gc_enable();
                gcDisabled = false;
            }
            return { ok: true, gc_disabled: gcDisabled, engine: 'il2cpp' };
        },
        apiCount: function () { return Object.keys(funcs).length; },
        gcSupported: true,
        findInstances: function (klass, maxCount) {
            return findInstancesIL2CPP(klass, maxCount);
        },
        // 「这个候选能不能 Hook」是**引擎相关**的判断，必须由后端给出：
        // IL2CPP 的机器码一定在 GameAssembly 里，所以不在模块内就是可疑候选
        // （空指针、转发桩）。见 BE_MONO 里的同名方法 —— Mono 恰好相反。
        isHookable: function (c) {
            return !!(c && c.pointer && c.in_module !== false);
        },
        methodTargetNote: '用 candidates[].pointer 直接 Hook；或静态 RVA + module_base 定位'
    };

    var BE_MONO = {
        kind: 'mono',
        label: 'Mono',
        moduleCandidates: MONO_MODULE_CANDIDATES,
        ensureReady: monoEnsureReady,
        allImages: monoImages,
        resolveClassOrFail: monoResolveClassOrFail,
        classAttrs: monoClassAttrs,
        fieldsOfOwn: monoFieldsOfOwn,
        methodsOf: monoMethodsOf,
        fullNameOf: monoClassFullName,
        parentOf: monoParentOf,
        stringOf: monoStringOf,
        gcControl: monoGcControl,
        apiCount: function () { return Object.keys(monoFuncs).length; },
        gcSupported: false,
        findInstances: function (klass, maxCount) {
            return findInstancesMono(klass, maxCount);
        },
        // Mono 的 JIT 代码**一定**在 mono 模块外，所以「不在模块内」完全正常，
        // 不能用它筛掉候选 —— 只要 mono_compile_method 给出了非空地址就能 Hook。
        // 实测踩到的：session.py 曾用 `in_module is not False` 过滤候选，
        // 于是 Mono 上**所有**方法都被判成「没有可 Hook 的原生地址」，
        // 而 find_hook_target 明明刚把地址给了出来（同一个候选）。
        isHookable: function (c) {
            return !!(c && c.pointer && c.pointer !== '0x0');
        },
        methodTargetNote: '用 candidates[].pointer 直接 Hook。' +
                          '注意：Mono 是 JIT，代码地址**不在 mono 模块内**，' +
                          '所以没有 RVA（静态元数据里的 RVA 指向 IL，不是机器码）'
    };

    /**
     * 选定后端：先试 IL2CPP 模块，再试 Mono 模块。
     *
     * 为什么不是「按静态索引的引擎决定」：运行时看到的才算数 ——
     * 静态索引可能来自另一份安装（同目录换版本），而注入的进程里到底加载了
     * 哪个运行时，只有这里能回答。选定后**黏住**，不每次重选。
     */
    function selectBackend(preferredModule) {
        if (BE) { return BE; }
        var m = resolveModule(preferredModule);
        if (m) {
            mod = m;
            BE = BE_IL2CPP;
            return BE;
        }
        var mm = monoResolveModule(preferredModule);
        if (mm) {
            mod = mm;
            BE = BE_MONO;
            return BE;
        }
        return null;
    }

    function backendNotFound() {
        return fail('未找到 IL2CPP 或 Mono 运行时模块', {
            il2cpp_tried: MODULE_CANDIDATES,
            mono_tried: MONO_MODULE_CANDIDATES,
            process: Process.id,
            retryable: true,
            note: '刚 spawn 的进程里运行时模块可能还没加载完，稍后重试即可'
        });
    }

    rpc.exports = {

        /** 健康检查：不依赖 IL2CPP，验证脚本已加载。 */
        ping: function () {
            return {
                ok: true,
                frida: Frida.version,
                runtime: Script.runtime,
                arch: Process.arch,
                pointer_size: Process.pointerSize,
                platform: Process.platform,
                pid: Process.id
            };
        },

        /**
         * 初始化并报告模块 / 导出可用性。
         *
         * 先试 IL2CPP 再试 Mono —— 两个后端共用同一套 RPC，
         * 宿主不需要预先知道进程里是哪个引擎（返回值里的 engine 字段会说明）。
         */
        init: function (preferredModule) {
            var be = selectBackend(preferredModule);
            if (!be) { return backendNotFound(); }
            var res = be.ensureReady(preferredModule);
            if (res && res.ok) { res.engine = be.kind; }
            return res;
        },

        /**
         * 卸载前清理：把探针线程从引擎域里摘出去。
         *
         * 宿主在 `script.unload()` **之前**调用它。不调用的话，IL2CPP 域里
         * 会留下指向已释放 agent 内存的线程记录，游戏会在随后的 GC 中崩
         * （详见 `releaseDomain` 的注释）。
         */
        dispose: function () {
            var out = { ok: true, engine: BE ? BE.kind : null };
            if (BE && BE.kind === 'il2cpp') {
                out.il2cpp = releaseDomain();
            }
            return out;
        },

        /** 运行时总览：模块、域、映像数量。 */
        runtimeInfo: function (preferredModule) {            var be = selectBackend(preferredModule);
            if (!be) { return backendNotFound(); }
            var ready = be.ensureReady(preferredModule);
            if (!ready.ok) { return ready; }
            var imgs = [];
            try {
                imgs = be.allImages();
            } catch (e) {
                return fail('枚举映像失败：' + e.message);
            }
            return {
                ok: true,
                engine: be.kind,
                module: mod.name,
                base: mod.base.toString(),
                size: mod.size,
                image_count: imgs.length,
                images: imgs.map(function (x) {
                    return { name: x.name, class_count: x.class_count };
                }),
                api_available: be.apiCount(),
                api_missing: apiMissing,
                fallbacks: fallbacks,
                gc_disabled: gcDisabled,
                gc_supported: be.gcSupported
            };
        },

        /** 列出全部映像（程序集）。 */
        listImages: function (preferredModule) {
            var be = selectBackend(preferredModule);
            if (!be) { return backendNotFound(); }
            var ready = be.ensureReady(preferredModule);
            if (!ready.ok) { return ready; }
            return { ok: true, engine: be.kind, images: be.allImages() };
        },

        /** 查类：返回运行时字段偏移与方法地址。 */
        classInfo: function (opts, preferredModule) {
            var be = selectBackend(preferredModule);
            if (!be) { return backendNotFound(); }
            var ready = be.ensureReady(preferredModule);
            if (!ready.ok) { return ready; }
            opts = opts || {};
            var found = be.resolveClassOrFail(opts.image, opts.namespace, opts.name);
            if (!found.ok) { return found; }
            var k = found.klass;
            var out = {
                ok: true,
                engine: be.kind,
                image: found.image.name,
                klass: k.toString()
            };
            var attrs = {};
            try { attrs = be.classAttrs(k) || {}; } catch (e) { attrs = {}; }
            for (var key in attrs) {
                if (Object.prototype.hasOwnProperty.call(attrs, key)) { out[key] = attrs[key]; }
            }
            // 字段：默认给出实例字段的真实偏移（含继承链上的，标 inherited_from）
            if (opts.fields !== false) {
                out.fields = fieldsWithInheritance(k, null);
                out.field_count = out.fields.length;
            }
            // 方法：默认给出代码地址，这是 Hook 的落点
            if (opts.methods !== false) {
                var ms = be.methodsOf(k);
                // 支持按名字过滤，便于直接定位 Hook 目标
                if (opts.method_filter) {
                    var f = String(opts.method_filter);
                    ms = ms.filter(function (x) {
                        return x.name && x.name.indexOf(f) >= 0;
                    });
                }
                out.methods = ms;
                out.method_count = ms.length;
            }
            if (fallbacks.length) { out.fallbacks = fallbacks; }
            return out;
        },

        /** 只取某方法的 Hook 落点信息。 */
        methodTarget: function (opts, preferredModule) {
            var be = selectBackend(preferredModule);
            if (!be) { return backendNotFound(); }
            var ready = be.ensureReady(preferredModule);
            if (!ready.ok) { return ready; }
            opts = opts || {};
            var found = be.resolveClassOrFail(opts.image, opts.namespace, opts.name);
            if (!found.ok) { return found; }
            var ms = be.methodsOf(found.klass);
            var hits = ms.filter(function (x) {
                return x.name === opts.method;
            });
            if (!hits.length) {
                return fail('未找到方法 ' + opts.method, {
                    available: ms.map(function (x) { return x.name; }).slice(0, 80)
                });
            }
            // 「能不能 Hook」由后端标注（IL2CPP 看是否在模块内，Mono 只要地址非空），
            // 上层据此筛选，不再自己猜引擎语义。
            hits.forEach(function (c) {
                try { c.hookable = be.isHookable(c); }
                catch (e) { c.hookable = false; }
            });
            return {
                ok: true,
                engine: be.kind,
                class: be.fullNameOf(found.klass),
                image: found.image.name,
                module: mod.name,
                module_base: mod.base.toString(),
                candidates: hits,
                usable_count: hits.filter(function (c) { return c.hookable; }).length,
                note: be.methodTargetNote
            };
        },

        /** 枚举类的字段（可只要静态字段）。 */
        classFields: function (opts, preferredModule) {
            var be = selectBackend(preferredModule);
            if (!be) { return backendNotFound(); }
            var ready = be.ensureReady(preferredModule);
            if (!ready.ok) { return ready; }
            opts = opts || {};
            var found = be.resolveClassOrFail(opts.image, opts.namespace, opts.name);
            if (!found.ok) { return found; }
            var want = (opts.static_only === true) ? true
                     : (opts.instance_only === true) ? false : null;
            var fs = fieldsWithInheritance(found.klass, want);
            return {
                ok: true,
                engine: be.kind,
                class: be.fullNameOf(found.klass),
                fields: fs,
                count: fs.length,
                fallbacks: fallbacks
            };
        },

        /**
         * 枚举某个类的活对象实例（走 GC liveness）。
         *
         * 这是「拿到 instance 指针」的正路 —— 在此之前只能靠 Hook 碰运气。
         * 引擎差异由后端各自处理（IL2CPP 有 liveness 导出；Mono 只有受限的
         * mono_gc_walk_heap，可能如实报「本时机不可用」）。
         */
        findInstances: function (opts, preferredModule) {
            var be = selectBackend(preferredModule);
            if (!be) { return backendNotFound(); }
            var ready = be.ensureReady(preferredModule);
            if (!ready.ok) { return ready; }
            opts = opts || {};
            var found = be.resolveClassOrFail(opts.image, opts.namespace, opts.name);
            if (!found.ok) { return found; }
            if (!be.findInstances) {
                return fail('当前引擎后端不支持实例枚举（' + be.kind + '）', {
                    engine: be.kind, unsupported: true
                });
            }
            var maxCount = opts.max_count;
            if (typeof maxCount !== 'number' || maxCount <= 0) { maxCount = 100; }
            if (maxCount > 100000) { maxCount = 100000; }

            var res;
            try {
                res = be.findInstances(found.klass, maxCount);
            } catch (e) {
                return fail('实例枚举异常：' + (e.message || e), { engine: be.kind });
            }
            if (!res || !res.ok) { return res || fail('实例枚举失败'); }

            // 把类的身份补进去：调用方据此确认「枚举的确实是这个类」
            res.class = be.fullNameOf(found.klass);
            res.image = found.image ? found.image.name : null;
            res.klass = found.klass.toString();
            res.max_count = maxCount;
            if (fallbacks.length) { res.fallbacks = fallbacks; }
            return res;
        },

        /**
         * 安装 Hook。
         *
         * 注意：本接口**不要求 IL2CPP 初始化成功**。给定绝对地址即可挂钩，
         * 与引擎无关 —— 原生游戏的函数、系统 API、甚至 Unity 之外的进程
         * 都能 Hook。若运行时模块（IL2CPP 或 Mono）恰好可用，会顺带解析模块基址，
         * 以便把**落在模块内**的地址换算成 RVA（方便与静态元数据对照）。
         */
        hook: function (spec, preferredModule) {
            if (!mod) {
                // 尽力而为：解析不到就只按绝对地址挂钩，不阻断
                try { selectBackend(preferredModule); } catch (e) { mod = null; }
            }
            return hookAt(spec || {});
        },

        /** 卸载指定 Hook。 */
        unhook: function (id) {
            var h = hooks[id];
            if (!h) { return fail('没有这个 Hook：' + id, { hooks: Object.keys(hooks) }); }
            try {
                h.listener.detach();
            } catch (e) {
                return fail('卸载失败：' + e.message);
            }
            delete hooks[id];
            delete hookCounts[id];
            return { ok: true, hook_id: id, removed: true };
        },

        /** 卸载全部 Hook。 */
        unhookAll: function () {
            var n = 0;
            for (var id in hooks) {
                if (!Object.prototype.hasOwnProperty.call(hooks, id)) { continue; }
                try { hooks[id].listener.detach(); n++; } catch (e) { /* 继续卸其它 */ }
            }
            hooks = {};
            hookCounts = {};
            return { ok: true, removed: n };
        },

        /** 当前 Hook 列表与触发计数。 */
        hookStatus: function () {
            var out = [];
            for (var id in hooks) {
                if (!Object.prototype.hasOwnProperty.call(hooks, id)) { continue; }
                out.push({
                    hook_id: id,
                    label: hooks[id].spec.label || null,
                    target: hooks[id].target.toString(),
                    target_rva: describeAddress(hooks[id].target),
                    enter_count: hookCounts[id] ? hookCounts[id].enter : 0,
                    leave_count: hookCounts[id] ? hookCounts[id].leave : 0
                });
            }
            return { ok: true, hooks: out, count: out.length };
        },

        /** 读内存。 */
        readMemory: function (address, size) {
            try {
                var p = ptr(address);
                var n = parseInt(size, 10);
                if (!(n > 0) || n > 0x1000000) {
                    return fail('size 不合法（需 1..16777216）', { size: size });
                }
                // readByteArray 对**不可读地址返回 null**（不像 readU8 会抛异常），
                // 而 `new Uint8Array(null)` 会得到一个长度 0 的数组 —— 于是老写法
                // 会把「读不到」报成 `{ok:true, size:4, hex:""}`：假成功。
                // 实测 read_memory(address="0x0", size=4) 就命中了这条。
                var raw = p.readByteArray(n);
                if (raw === null || raw === undefined) {
                    return fail('读取失败：地址不可读（' + p.toString() +
                                '，' + n + ' 字节）',
                                { address: p.toString(), size: n });
                }
                var bytes = new Uint8Array(raw);
                if (bytes.length !== n) {
                    return fail('读取不完整：要求 ' + n + ' 字节，实际读到 ' +
                                bytes.length + ' 字节',
                                { address: p.toString(), size: n,
                                  got: bytes.length });
                }
                var hex = [];
                for (var i = 0; i < bytes.length; i++) {
                    hex.push(('0' + bytes[i].toString(16)).slice(-2));
                }
                return { ok: true, address: p.toString(), size: n, hex: hex.join('') };
            } catch (e) {
                return fail('读取失败：' + e.message, { address: String(address) });
            }
        },

        /**
         * 写内存。
         * 警告：写错地址会立刻崩溃游戏，务必先用 readMemory 确认。
         *
         * 实现要点：**先直接写，只有失败时才改页面保护**。
         * 不要无条件 Memory.protect(..., 'rwx')：那会把本不需要动保护的页面
         * （包括 Frida 自己 Memory.alloc 出来的堆）也改成可执行，
         * 既有安全隐患，也可能让运行时的内存管理出问题（实测会导致脚本
         * 在后续某次 RPC 调用时被销毁）。
         */
        writeMemory: function (address, hex) {
            try {
                var p = ptr(address);
                var s = String(hex).replace(/\s+/g, '');
                if (s.length % 2 !== 0) { return fail('hex 长度必须为偶数'); }
                var bytes = [];
                for (var i = 0; i < s.length; i += 2) {
                    var b = parseInt(s.substr(i, 2), 16);
                    if (isNaN(b)) {
                        return fail('hex 含非法字符：' + s.substr(i, 2));
                    }
                    bytes.push(b);
                }
                if (!bytes.length || bytes.length > 0x100000) {
                    return fail('写入长度不合法（需 1..1048576 字节）');
                }

                var usedProtect = false;
                try {
                    p.writeByteArray(bytes);
                } catch (firstErr) {
                    // 只有当直接写确实失败时，才尝试放宽该页面的保护
                    try {
                        Memory.protect(p, bytes.length, 'rw-');
                        usedProtect = true;
                        p.writeByteArray(bytes);
                    } catch (secondErr) {
                        return fail('写入失败：' + secondErr.message, {
                            address: p.toString(),
                            first_error: firstErr.message
                        });
                    }
                }
                return {
                    ok: true,
                    address: p.toString(),
                    written: bytes.length,
                    relaxed_protection: usedProtect
                };
            } catch (e) {
                return fail('写入失败：' + e.message, { address: String(address) });
            }
        },

        /**
         * AOB 扫描（兜底手段：优先用元数据/RVA 定位，仅在符号缺失时使用）。
         *
         * 注意：本接口**不要求 IL2CPP 初始化成功**。AOB 扫描恰恰是
         * 「符号被剥离 / 非 Unity 游戏 / 找不到 il2cpp 导出」时的兜底手段，
         * 若强制依赖 IL2CPP 就自相矛盾。因此这里只要求能确定一个扫描区间。
         *
         * 区间选择优先级：opts.module 指定的模块 → IL2CPP 模块 → 主可执行模块
         */
        scanPattern: function (opts) {
            opts = opts || {};
            var pattern = String(opts.pattern || '');
            if (!pattern) { return fail('缺少 pattern'); }

            // 尽量解析 IL2CPP 模块，但**允许失败**（非 IL2CPP 目标也要能扫）
            if (!mod) { mod = resolveModule(opts.module); }

            var target = null;
            if (opts.module) {
                target = Process.findModuleByName(opts.module);
                if (!target) { return fail('未找到模块 ' + opts.module); }
            } else if (mod) {
                target = mod;
            } else {
                // 退到主可执行模块：原生游戏/工具也能扫
                var mods = Process.enumerateModules();
                target = mods.length ? mods[0] : null;
                if (!target) {
                    return fail('无法确定扫描区间：既没有 IL2CPP 模块，也没有主模块');
                }
            }

            var maxResults = typeof opts.max_results === 'number' ? opts.max_results : 32;
            var results = [];
            var truncated = false;

            // 只扫落在目标模块内的可读段：更快、误报更少
            var ranges = [];
            try {
                var all = Process.enumerateRanges('r--');
                for (var i = 0; i < all.length; i++) {
                    var r = all[i];
                    if (r.base.compare(target.base) >= 0 &&
                        r.base.compare(target.base.add(target.size)) < 0) {
                        ranges.push(r);
                    }
                }
            } catch (e) {
                ranges = [];
            }
            if (!ranges.length) {
                // 枚举失败时退化为整模块扫描
                ranges = [{ base: target.base, size: target.size }];
            }

            // 注意：**必须用 scanSync**。
            // Memory.scan 是异步的：onMatch 回调在本次 RPC 调用返回之后才执行，
            // 因此用 Memory.scan 收集结果永远是空的（实测确认：同一段内存
            // scanSync 命中 1 处，Memory.scan 命中 0 处）。
            for (var k = 0; k < ranges.length; k++) {
                if (results.length >= maxResults) { truncated = true; break; }
                try {
                    var hits = Memory.scanSync(ranges[k].base, ranges[k].size, pattern);
                    for (var h = 0; h < hits.length; h++) {
                        if (results.length >= maxResults) { truncated = true; break; }
                        var d = describeAddress(hits[h].address);
                        results.push({
                            address: hits[h].address.toString(),
                            size: hits[h].size,
                            rva_hex: d ? d.rva_hex : null
                        });
                    }
                } catch (e) {
                    // 单段不可读：跳过该段而不是整体失败
                    continue;
                }
            }

            return {
                ok: true,
                pattern: pattern,
                scanned_module: target.name,
                scanned_ranges: ranges.length,
                matched: results.length,
                truncated: truncated,
                results: results,
                note: 'AOB 扫描是兜底手段；优先用 classInfo/methodTarget 拿到 RVA 后直接定位'
            };
        },

        /**
         * 任意 JS 求值 —— 应急逃生舱。
         * 与 AiMod 的 eval_script 同定位：当预置工具不够用时，
         * 让 Agent 直接下发 JS 探测。返回值必须是可 JSON 序列化的。
         */
        evalJs: function (code) {
            try {
                // 注入的可用对象：mod / api / funcs / hooks（IL2CPP 侧），
                // 外加 monoFuncs 与 BE（Mono 侧与后端选择结果）——
                // 后者是排查 Mono 问题的必需品：没有它就只能靠复刻绑定逻辑猜
                // （实测踩过：想知道哪个 mono_* 没绑上，却看不到 monoFuncs）。
                // eslint-disable-next-line no-new-func
                var fn = new Function('mod', 'api', 'funcs', 'hooks', 'monoFuncs', 'BE',
                                      'return (' + String(code) + ');');
                var v = fn(mod, api, funcs, hooks, monoFuncs, BE);
                return { ok: true, value: v === undefined ? null : v };
            } catch (e) {
                // 表达式求值失败时，按语句块再试一次
                try {
                    // eslint-disable-next-line no-new-func
                    var fn2 = new Function('mod', 'api', 'funcs', 'hooks', 'monoFuncs', 'BE',
                                           String(code));
                    var v2 = fn2(mod, api, funcs, hooks, monoFuncs, BE);
                    return { ok: true, value: v2 === undefined ? null : v2 };
                } catch (e2) {
                    return fail('求值失败：' + e2.message, { first_error: e.message });
                }
            }
        },

        /**
         * GC 控制：Hook 期间建议关 GC，避免对象在回调中被回收。
         *
         * 注意引擎差异：IL2CPP 有 il2cpp_gc_disable/enable，Mono **没有**对应导出
         * （四个样本一致核实），Mono 上这里会如实返回 ok:false。
         */
        gcControl: function (disable) {
            var be = selectBackend();
            if (!be) { return backendNotFound(); }
            var ready = be.ensureReady();
            if (!ready.ok) { return ready; }
            return be.gcControl(disable);
        },

        /**
         * 分配一块**持久**缓冲区。
         *
         * 为什么需要这个：Frida 的 Memory.alloc 返回的内存若在 JS 侧没有引用，
         * 会被脚本 GC 回收（实测：一次 RPC 里 alloc 出的地址，下一次 RPC
         * 去读已经被复用/清零）。这里把指针存进模块级数组，保证在脚本存活
         * 期间稳定有效，供宿主做「写入 → 读回」这类跨调用校验，
         * 也可当作读结构体的临时缓冲区。
         */
        allocBuffer: function (size) {
            var n = parseInt(size, 10);
            if (!(n > 0) || n > 0x1000000) {
                return fail('size 不合法（需 1..16777216）', { size: size });
            }
            try {
                var p = Memory.alloc(n);
                scratch.push({ ptr: p, size: n });
                return { ok: true, address: p.toString(), size: n,
                         note: '该缓冲区在脚本存活期间保持有效' };
            } catch (e) {
                return fail('分配失败：' + e.message);
            }
        },

        /** 释放之前 allocBuffer 出来的缓冲区。 */
        freeBuffer: function (address) {
            var kept = [];
            var removed = 0;
            for (var i = 0; i < scratch.length; i++) {
                if (scratch[i].ptr.toString() === String(address)) {
                    removed++;
                } else {
                    kept.push(scratch[i]);
                }
            }
            scratch = kept;
            return { ok: true, removed: removed };
        },

        /** 列出当前持有的持久缓冲区。 */
        listBuffers: function () {
            return {
                ok: true,
                buffers: scratch.map(function (b) {
                    return { address: b.ptr.toString(), size: b.size };
                })
            };
        }
    };

    /**
     * 每个 RPC 入口先清掉「单次调用内缓存」。
     *
     * 为什么必须有这一层：Mono 的程序集是**懒加载**的，会随着游戏运行**持续变多**。
     * 实测（Archon Soul）：`init` 成功后 0.5 秒枚举到 **13** 个程序集，
     * 且全是 BepInEx 自己那批（MonoMod / 0Harmony / Mono.Cecil / System）；
     * Unity 的 `UnityEngine.CoreModule` 等**还没加载**。若把这份清单永久缓存，
     * 之后 `UnityEngine.Time` 永远查不到 —— 而工具会报「类不存在」，
     * 让 Agent 以为游戏里没有这个类（实际是探针没再看一眼）。
     * 实测那版正是如此：`runtime_class_info("UnityEngine.Time")` 与
     * `find_hook_target(Time.get_frameCount)` 双双失败，detail 里写着
     * `searched_images: 13`。
     *
     * 也不能每次查类都重新枚举：类解析会按「映像 × 名字写法」组合着调
     * `mono_class_from_name`（158 个映像 × 6 种写法），每次都重扫一遍程序集
     * 会慢到不可用。所以粒度取**一次 RPC**：同一 RPC 内复用，跨 RPC 刷新。
     */
    for (var _k in rpc.exports) {
        if (typeof rpc.exports[_k] !== 'function') { continue; }
        rpc.exports[_k] = (function (orig) {
            return function () {
                monoImagesCache = null;
                monoTypeIndexCache = null;
                return orig.apply(null, arguments);
            };
        })(rpc.exports[_k]);
    }

    // 脚本加载即发送一条就绪消息，便于宿主确认注入成功
    try {
        send({ type: 'probe_ready', frida: Frida.version, pid: Process.id,
               arch: Process.arch });
    } catch (e) {
        // 加载期 send 失败不致命
    }

})();
