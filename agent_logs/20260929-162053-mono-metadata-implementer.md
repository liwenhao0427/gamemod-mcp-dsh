# Mono (ECMA-335) 元数据解析器实现

- **时间**：2026-09-29 16:20
- **角色**：mono-metadata-implementer（子 Agent）
- **结果**：✅ 完成，全部 150 项断言通过（3 个真实样本 + 官方真值交叉比对）

---

## 一、交付物

| 文件 | 大小 | 说明 |
|---|---|---|
| `src/gamemod_mcp/engine/mono_meta.py` | 101,773 B | 主模块，ECMA-335 解析器 |
| `devtools/verify_mono.py` | 29,541 B | 真实验证脚本（含官方真值交叉比对） |
| `devtools/verify_mono_robust.py` | 5,670 B | 边界/失败路径 + 编码检查 |
| `devtools/verify_mono_coverage.py` | 5,091 B | 覆盖率自检：45 张表 + 13 个 coded index |
| `devtools/probe_mono_paramcount.py` | 4,002 B | 探针：核查签名参数个数与 Param 表区间差异 |

三个验证脚本均 **exit code 0**：
`verify_mono.py` 断言 150 / 失败 0；
`verify_mono_robust.py` 检查项 10 / 失败 0；
`verify_mono_coverage.py` 45 张表全有列定义、13 个 coded index 全实现、无悬空引用。

全部 UTF-8 无 BOM（脚本内自动校验通过）。仅用标准库（`struct` / `pathlib` / `dataclasses`），
复用已有 `engine/pe.py` 做 PE 与 RVA→偏移换算。

---

## 二、真实验证输出（关键数字，原样摘录）

运行：`python devtools\verify_mono.py 2>&1` → **断言总数 150，失败 0**

### 样本 1：Archon Soul / Assembly-CSharp.dll（1,141,760 B）

```
元数据版本字符串 : v4.0.30319      表流版本 : #~ 2.0
heapSizes        : 0x01  (#Strings=4B, #GUID=2B, #Blob=2B)
模块 / 程序集    : Assembly-CSharp.dll  /  Assembly-CSharp
类型数 1,387   方法数 6,611   字段数 6,343   参数数 3,222
属性/事件 1,163/11   引用程序集 22   成员引用 1,760   自定义特性 3,639
[PASS] Σfield_count == Field 表行数        —— 6343 vs 6343
[PASS] Σmethod_count == MethodDef 表行数   —— 6611 vs 6611
[PASS] Σparameter_count == Param 表行数    —— 3222 vs 3222
[PASS] 字段/方法全部有归属类型  —— owned_fields=6343/6343, owned_methods=6611/6611
比对 45 张表的行数，不符 0 张
逐类型比对 1,387 个：全名 0 不符 / 基类 0 不符 / 字段名列表 0 不符 / 方法名列表 0 不符
逐方法签名比对 6,611 个：返回类型 0 不符 / 参数类型 0 不符 / 参数个数 0 不符
逐字段类型比对 6,343 个：0 不符
字段签名解析成功率 6,343/6,343 = 100.0000%
方法签名解析成功率 6,611/6,611 = 100.0000%
find_derived('UnityEngine.MonoBehaviour') → 644 个
```

抽查类型 `BaseCurve`（token 0x02000005）：
```
字段 2：cacheArcLengths : System.Single[]   needsUpdate : System.Boolean
方法：GetPoint(System.Single) -> UnityEngine.Vector3   Param 表参数名 ['t']
      GetPoints(System.Int32) -> UnityEngine.Vector3[] Param 表参数名 ['divisions']
嵌套类型示例：TextDictionary.<>c / Dice.<>c__DisplayClass48_0 / Dice.<AdditiveRuneRoutine>d__53
```

### 样本 2：Kinny and the Cosmic Cauldron / Assembly-CSharp.dll（921,088 B）

```
类型数 689   方法数 5,221   字段数 4,620   参数数 3,557
Σ 三项自洽全 True；45 张表行数不符 0；类型 689 全名/基类/字段/方法 0 不符
方法签名 5,221 个：0 不符      字段类型 4,620 个：0 不符
```

### 样本 3：mscorlib.dll（4,632,064 B）—— 基础类库，压规模边界

```
heapSizes        : 0x05  (#Strings=4B, #GUID=2B, #Blob=4B)   ← 与两个游戏样本不同
类型数 3,023   方法数 27,495   字段数 15,986   参数数 35,896
Σ 三项自洽全 True；30 张表非空（比游戏样本多出 FieldMarshal/DeclSecurity/
                                ModuleRef/ImplMap/ManifestResource 等）
45 张表行数不符 0；类型 3,023 全名/基类/字段/方法 0 不符
方法签名 27,495 个：0 不符     字段类型 15,986 个：0 不符
嵌套名示例：Interop.Kernel32.WIN32_FIND_DATA.<_cFileName>e__FixedBuffer
```

### 边界/失败路径（`python devtools\verify_mono_robust.py`，10 项全过）

```
[OK] 不存在的路径            -> MetadataError
[OK] 非 PE 文件              -> MetadataError: 不是有效的 PE 文件（缺少 MZ 头）
[OK] 损坏的 PE               -> MetadataError: NT 头签名无效
[OK] 原生非托管 DLL GameAssembly.dll (85 MB)
                             -> MetadataError: 不是托管程序集（无 CLR Runtime Header）
[OK] 截断在元数据末尾前 1000 字节 -> MetadataError: 流 #Blob 声明大小 63,720 字节，但只剩 62,720 字节
[OK] 截断在元数据中点        -> MetadataError: 流 #~ 声明大小 308,008 字节，但只剩 265,314 字节
[OK] 元数据之后截断（元数据完整）-> 解析成功，符合预期
[OK] 三个源文件 UTF-8 无 BOM
```

---

## 三、实现的表（诚实清单）

**全 45 张 ECMA-335 表全部实现**（Column 顺序逐张核对 II.22），
其中 30 张在真实样本中被实际解析到并**与官方真值行数逐张比对一致**。

实测出现过的表（样本 1 共 27 张 / 样本 3 共 30 张）：
Module、TypeRef、TypeDef、Field、MethodDef、Param、InterfaceImpl、MemberRef、
Constant、CustomAttribute、FieldMarshal、DeclSecurity、ClassLayout、FieldLayout、
StandAloneSig、EventMap、Event、PropertyMap、Property、MethodSemantics、MethodImpl、
ModuleRef、TypeSpec、ImplMap、FieldRVA、Assembly、AssemblyRef、ManifestResource、
NestedClass、GenericParam、MethodSpec、GenericParamConstraint。

规范完整实现、但真实样本里**未出现**故未被实测覆盖的：
FieldPtr、MethodPtr、ParamPtr、EventPtr、PropertyPtr、ENCLog、ENCMap、
AssemblyProcessor、AssemblyOS、AssemblyRefProcessor、AssemblyRefOS、File、ExportedType。

**未实现 / 不解析**：
- `#Pdb` 流、`#US` 用户字符串堆（只定位，不解码）
- CustomAttribute 的 blob 内容（只给原始 bytes，不解码构造参数）
- FieldPtr/MethodPtr/ParamPtr/EventPtr/PropertyPtr 的**间接寻址未接线**
  （若这些表非空会输出警告，不静默给出错误归属）
- ELEMENT_TYPE_FNPTR 只标注存在，不展开
- Constant 值解码仅覆盖基本数值/字符串，其余返回原始 bytes

---

## 四、过程中发现并修正的真实 bug（4 个）

1. **嵌套类型名去重错了**（最隐蔽）。
   早期版本拼接后做了「名字已以 namespace 开头就不重复加」的去重，
   把 `UiParticles.UiParticles.Geometry` 压成 `UiParticles.Geometry`，
   导致 1 个类型全名 + 74 个字段类型 + 40 个方法参数类型对不上。
   **修正**：严格按官方语义递归拼接，嵌套类型忽略自身 namespace，不做任何去重。

2. **记忆化前缀丢失**（修 1 时引入的回归）。
   向上去重链撞到已缓存祖先时，把链顶当成根重拼 namespace，退化成只用短名
   （`TextDictionary.<>c` 被错拼成 `<>c`），530 个全名全错。
   **修正**：撞到已缓存祖先时，用其缓存结果作为 seed 前缀。

3. **`MemberRefParent` coded index 缺失** → 补上（3 位 tag，5 表）。

4. **截断文件抛 `struct.error`** 而非 `MetadataError`。
   根因：流表里的 size 是**声明大小**，文件被截断时会大于真实剩余字节。
   **修正**：`_stream_span` 按真实文件长度校验并抛 MetadataError；
   所有 `_u8/_u16/_u32/_u64` 统一加 `_ensure` 越界检查。

### 同时修正的 2 个**测试**错误（不是解析器错误）
- **「签名参数个数 == Param 表行数」这个断言本身是错的**。
  按 ECMA-335 II.22.33，Param 表只为「有名字/有特性/有默认值」的参数建行。
  实测 7/3/1 处不符，逐条打印原始字节核对后**全部是「Param 行数少于签名」**，
  且都是编译器生成的 `<...>g__X|N_M` 局部函数（隐藏参数无名字），
  没有一处「多于」。真正的错误信号是「多于」，已改断言。
- **在 mscorlib 上写死探测 `Player` / `MonoBehaviour` 是错的**：
  mscorlib 的 assembly_refs 是空表（基础类库不引用任何程序集），
  自然不含 Player 也不引用 UnityEngine。已改为按样本配置探测词。

---

## 五、已知局限与未验证部分

1. **两个游戏样本的 PE 是 PE32(x86)**；未在 PE32+ / AnyCPU 程序集上验证
   （RVA→偏移逻辑复用已有 `pe.py`，但本模块路径本身未在 PE32+ 上实测）。
2. **未验证多模块程序集**（File / ExportedType 表非空的情况），
   也未验证 `#-` 未压缩流（代码已支持，真实样本都是 `#~`）。
3. `images` 在 Mono 里是「一个模块」概念（`Assembly-CSharp.dll`），
   不是 IL2CPP 那种多 image 列表，单模块程序集下长度为 1。
4. `parent_of()` 只在基类为**本程序集内 TypeDef** 时返回对象；
   指向 TypeRef（如 `System.Object`、`UnityEngine.MonoBehaviour`）时返回 None，
   需用 `parent_name_of()` 取名字。这是刻意设计，避免伪造跨程序集对象。
5. `search_types` 默认排除编译器生成名（`<` 开头或含 `<>`），
   与 il2cpp 版一致；可用 `exclude_compiler_generated=False` 关闭。
6. 未做多线程/大文件内存优化：整文件读入内存（mscorlib 4.6 MB 无压力，
   但 85 MB 级程序集会占内存）。
7. CustomAttribute 只存 blob 不解码——若上层需要「查某类上的某个特性」，
   需另行实现 blob 解码。
