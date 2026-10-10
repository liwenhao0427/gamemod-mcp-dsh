# Unreal 引擎结构提取器实现

- **时间**：2026-09-29 16:46
- **角色**：ue-structure-extractor（子 Agent）
- **结果**：✅ 完成，`verify_ue.py` exit 0 —— 28 项已验证可用 / 17 项明确标注未实现

---

## 一、交付物

| 文件 | 大小 | 说明 |
|---|---|---|
| `src/gamemod_mcp/engine/ue.py` | 61,736 B（1,524 行） | 主模块，UE 结构提取器 |
| `devtools/verify_ue.py` | 36,264 B（700 行） | 真实验证脚本（对两个样本 + 打包清单真值） |

两个文件均为 UTF-8 无 BOM。

---

## 二、委派 Prompt（原文）

> 为 GameMod MCP 项目实现 Unreal 引擎（UE4/UE5）的结构提取模块
> `src/gamemod_mcp/engine/ue.py`，并写配套验证脚本
> `devtools/verify_ue.py`。
>
> 项目背景与风格约定：
> - 纯标准库优先，不引入新依赖（不要用 pefile/lief，PE 解析用项目已有的
>   `engine/pe.py`）
> - 文件编码 UTF-8 无 BOM；注释用中文，标识符用英文
> - **绝对不要编造**：任何拿不到、没验证过的能力，必须明确标注为未实现，
>   并写出原因。宁可少一项能力，也不要给出猜测的数据。
> - 对外 API 尽量与 `il2cpp_meta.py` / `mono_meta.py` 对称，便于 `adapt.py` 适配
>
> 可用的真实样本（只读，不要修改）：
> - `D:\Projects\CatapultHero\CatapultHero\Binaries\Win64\CatapultHero-Win64-Shipping.exe`
>   （pak v12，251,936,396 B；utoc v8，980,429 B）
> - `D:\Projects\Dinoblade\DinobladeGame\Binaries\Win64\DinobladeGame-Win64-Shipping.exe`
>   （pak v11，59,328,010 B；utoc v8，6,539,397 B）
> - 两个样本各自带 `Manifest_UFSFiles_Win64.txt` 打包清单，可作交叉校验真值
> - 负例：`C:\Windows\System32\notepad.exe`
>
> 需要实现的能力：
> 1. `detect_ue_game(exe_path)` —— 识别是否 UE 游戏，给出可核查证据与置信度
> 2. `read_pak_index` / `list_pak_entries` —— .pak 索引与文件清单
> 3. `read_iostore_index` / `list_iostore_entries` —— .utoc（IoStore）目录索引
> 4. `parse_usmap` —— .usmap 解析（若样本中不存在，标注未实现）
> 5. `read_ue_config` / `list_config_files` —— .ini 配置
> 6. `read_manifest` / `find_manifests` —— 打包清单
> 7. `list_script_objects` / `get_class_info` —— exe 内 /Script/ 对象
> 8. `extract_ue_profile(exe_path, output_path)` —— 汇总
>
> 关键要求：
> - **所有字段偏移必须自己从字节里反推并验证**，不要照抄网上抄来抄去的
>   偏移表（那些表常互相矛盾）。反推后要用「独立求出的常量」去交叉确认。
> - 解析结果要**对打包清单交叉校验**，报告真实命中率。
> - 拿不到的能力（FName 池、UObject 属性偏移、UE 版本号、.usmap）
>   写进 `capabilities_not_implemented` 并说明原因。
>
> 完成后报告：实现的函数、实测数字（含交叉校验命中率）、未实现项及原因。

---

## 三、子 Agent 产出摘要（原文）

### 实测发现

**1. `detect_ue_game` 不能依赖运行时判定。**
两个样本的 `pe.runtime_kind` 都是 `native`，不是 .NET/IL2CPP/Mono，
所以项目现有的运行时判定对 UE 完全无效。
改用三类证据：文件名命名模式（`-Win64-Shipping.exe`）、目录布局
（`Engine/` + `Content/Paks/`）、exe 内 `/Script/` 字符串。

**2. UE 版本号拿不到，返回空串。**
搜索 `UE5\.\d+` / `UE4\.\d+` / `+UE\d+\.\d+` / `UnrealEngine[/\\][\d.]+`
全部 0 命中；`Engine/Build/Build.version` 也不存在。
因此 `ue_version` 返回空串，并把尝试过的路径写进 `ue_version_reason`。

**3. pak magic 在文件尾部，且后面还有零填充。**
`FPakInfo` magic `0x5A6F12E1` 位于 **EOF-204**，
magic 之后还有零填充，所以**不能**只读最后 4 或 8 字节，
必须在尾部窗口内重新扫描。

**4. pak 索引头 110 字节，v11/v12 布局相同。**
字段布局是**通过搜索独立求出的常量反推**的，不是照抄偏移表。

**5. FDI 记录格式有版本差异。**
FDI 记录从 `FDI+14` 开始，格式为 `[i32 len][name][i32 value]`。
**v11 的名字长度包含结尾 NUL，v12 的文件条目不包含**（但目录条目包含）。

**6. UTF-16 中文路径是真实存在的，必须支持。**
pak 里有 25 个非 ASCII 条目，utoc 里有 6,331 个 ——
实测路径含 `字库`、`精灵片`、`角色`。
原先的「ASCII 可打印性检查」只应用于 ASCII 分支。

**7. utoc 目录索引字段顺序是暴力枚举出来的。**
对所有排列做了穷举，只有 `Name / FirstSibling / FirstChild / FirstFile`
这一种能重建出真实路径。

**8. 比对必须大小写不敏感。**
CatapultHero：敏感 7,141 / 不敏感 7,178；
Dinoblade：敏感 17,567 / 不敏感 18,956。
清单会规范化大小写，敏感比对会少几十到上千条。

### 交叉校验结果（对 `Manifest_UFSFiles_Win64.txt`）

| 样本 | 容器 | 命中 | 总数 | 命中率 |
|---|---|---|---|---|
| CatapultHero | pak | 4,556 | 4,556 | **100.00%** |
| CatapultHero | utoc | 7,178 | 7,178 | **100.00%** |
| Dinoblade | pak | 4,565 | 4,565 | **100.00%** |
| Dinoblade | utoc | 18,956 | 18,956 | **100.00%** |

### 其他实测

- exe `/Script/` 提取：CatapultHero 214 模块 / 279 对象路径，
  Dinoblade 237 / 272
- 负例 `notepad.exe` 与 `DinobladeGame.exe`（游戏目录下的启动器）
  均正确判定 `is_ue=False`
- `.ini` 解析正常：`steam_emu.ini` 103 行 / 4 个 section；
  两个 `StagedBuild_*.ini` 是空的（3 字节 BOM）

### 未实现项（已写入 `capabilities_not_implemented`）

| 能力 | 原因 |
|---|---|
| `fname_pool` | 样本中无法定位 FName 池（无有效 FNameEntry 头） |
| `usmap` | 样本中不存在 .usmap 文件，无可依据的真实格式 |
| `ue_version` | 样本中无可信版本来源，返回空值而非猜测 |
| `uobject_property_offsets` | 需运行时（Frida）读取，静态样本无法验证 |

### 子 Agent 主动提出的待确认事项（原文）

> 1. 我没有改动 `engine/__init__.py` 的 `__all__`（当前是 `["pe"]`）。
>    是否要把 `ue` 加进去，请你决定。
> 2. 我没有把 `ue.py` 接到 `discovery.detect_engine` / `state.py` / 工具层。
>    目前它只能通过 `devtools/verify_ue.py` 直接调用。
>    接入方式请你确认后再做，我不想擅自改动别人写的模块。
> 3. `get_class_info` 目前只能给出从 exe 字符串提取的类名，
>    **给不出父类链和属性偏移**。如果你的下游需要这些，需要另做运行时方案。

---

## 四、主 Agent 的独立复核

**不采信子 Agent 的自述，重新跑了一遍：**

```
python devtools\verify_ue.py 2>&1
→ exit code 0，660 行输出
→ 已验证可用项：28 条
→ 未实现/未验证项：17 条
→ 未实现的能力（.usmap、FName 池、UE 版本、属性偏移）均明确标注，未编造任何数据。
```

**复核后发现的真实问题（子 Agent 未发现，因为只验了模块、没验工具层）：**

1. **`ue.ready` 是假的能力声明。** `get_diagnostics()` 报
   `ue.ready = true`，但 `ue_profile` / `ue_list_entries` / `detect_ue`
   这些工具**在 `server.py` 里根本不存在** ——
   能力没接上，诊断却说可用。这是最糟的状态：Agent 以为能用，一调就撞墙。
   → 已新增 5 个工具（`detect_ue` / `ue_profile` / `ue_list_entries` /
   `ue_script_objects` / `ue_manifest`），并把 UE 能力边界写进诊断的
   `engines.ue.capabilities`（9 项，其中 4 项明确标注「未实现」）。

2. **非 pak 文件被报成「解析成功但没条目」。**
   `list_pak_entries` 在「解析失败」和「容器真的空」两种情况下**都返回空列表**，
   所以一个 exe 传进去会得到 `ok=true` + `note="容器解析成功，但里面没有任何条目"`，
   而真实原因是 `尾部 4096 字节内未找到 pak magic`。
   → 工具层改为先调 `read_pak_index` / `read_iostore_index` 看 `ok`/`reason`，
   失败就明确报错。**假成功是最容易骗到 Agent 的一类结果。**

3. **翻页翻过头时静默给空页。** → 已加说明。

**复核结论**：子 Agent 的模块实现质量高、诚实（未实现项全部标注，未编造数据），
交叉校验 100% 可信。但它**只验了模块、没验工具层**，
而「模块能跑」和「Agent 调得到」是两件事 —— 这是委派时应当明确要求的。
教训：给子 Agent 的任务书里必须包含「接入工具层并端到端验证」这一项。

---

## 五、接入后的最终状态

- UE 工具已注册进 MCP：工具总数 **36 → 41**
- `devtools/verify_mcp_ue.py`（主 Agent 新写）：**60 项通过 / 0 失败**
  - 覆盖工具注册、诊断诚实性、正负例识别、容器解析与翻页、
    清单解析、脚本对象、以及**不干扰 IL2CPP 会话**
- `engine/__init__.py` 的 `__all__` 已更新为
  `["pe", "discovery", "adapt", "il2cpp_meta", "mono_meta", "ue", "index", "state"]`
- `skills/gamemod-mod/SKILL.md` 已加「先判断引擎」章节与 UE 配方 0 ——
  原文的「元数据优先」理念对 UE 不适用，必须一开始就分流
