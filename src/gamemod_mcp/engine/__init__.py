"""引擎子包：运行时识别与元数据提取。

支持的引擎：
- il2cpp  —— Unity IL2CPP（解析 global-metadata.dat + GameAssembly.dll）
- mono    —— Unity Mono（解析 *_Data/Managed/*.dll）
- ue      —— Unreal Engine（解析 .pak / .utoc 资源容器、打包清单、/Script/ 对象）

注意三者的能力边界不同，不要用同一套预期去套：
  · il2cpp / mono 的核心是**类型系统** —— 能查到类、字段、方法、token。
  · ue 的核心是**资源容器** —— 能列出打包了哪些资产，
    但 FName 池 / UObject 属性偏移在静态样本里拿不到（见 `state.UE_CAPABILITIES`）。
"""

from __future__ import annotations

# 注意：这里只列出「按需 import 也不会出问题」的模块。
# ue.py 有 1500 行且 import 链较重，submodule 走显式 import 而不是在这里预加载。
__all__ = ["pe", "discovery", "adapt", "il2cpp_meta", "mono_meta", "ue", "index", "state"]
