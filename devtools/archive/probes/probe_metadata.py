# IL2CPP global-metadata.dat 头部探测：确认 v31 表布局与记录大小
# 用已知地面真值（20072 types / 1805 enums）反推记录尺寸
import struct
import sys

META = r'D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat'

data = open(META, 'rb').read()
print(f'文件大小: {len(data):,}')

SANITY, VERSION = struct.unpack_from('<Ii', data, 0)
print(f'sanity = 0x{SANITY:08X}  (期望 0xFAB11BAF)')
print(f'version = {VERSION}')
assert SANITY == 0xFAB11BAF, 'sanity 校验失败'

# v31 头部：从偏移 8 开始是一串 (offset, size) 对
# 先读出 40 对，看看哪个位置开始不再是有效偏移
print('\n=== 头部 (offset,size) 对 ===')
pairs = []
for i in range(40):
    off, size = struct.unpack_from('<II', data, 8 + i * 8)
    pairs.append((off, size))
    valid = 'OK ' if (off == 0 or (0 < off < len(data))) else 'BAD'
    print(f'  [{i:2}] off=0x{off:08X} size={size:>10,}  {valid}')

# 已知 v31 标准表序（Il2CppDumper / Il2CppInspector 顺序）
NAMES = [
    'stringLiteral', 'stringLiteralData', 'string', 'events', 'properties',
    'methods', 'parameterDefaultValues', 'fieldDefaultValues',
    'fieldAndParameterDefaultValueData', 'fieldMarshaledSizes', 'parameters',
    'fields', 'genericParameters', 'genericParameterConstraints',
    'genericContainers', 'nestedTypes', 'interfaces', 'vtableMethods',
    'interfaceOffsets', 'typeDefinitions', 'images', 'assemblies',
    'fieldRefs', 'referencedAssemblies', 'attributeTypes', 'attributeData',
    'unresolvedVirtualCallParameterTypes', 'unresolvedVirtualCallParameterRanges',
    'windowsRuntimeTypeNames', 'windowsRuntimeStrings',
    'exportedTypeDefinitions',
]

# 已知 v31 记录大小（字节）
SIZES = {
    'stringLiteral': 8, 'events': 8, 'properties': 8, 'methods': 32,
    'parameterDefaultValues': 12, 'fieldDefaultValues': 12,
    'fieldMarshaledSizes': 8, 'parameters': 12, 'fields': 12,
    'genericParameters': 16, 'genericParameterConstraints': 4,
    'genericContainers': 16, 'nestedTypes': 4, 'interfaces': 4,
    'vtableMethods': 4, 'interfaceOffsets': 8, 'typeDefinitions': 88,
    'images': 40, 'assemblies': 64, 'fieldRefs': 8, 'referencedAssemblies': 4,
    'attributeTypes': 8, 'attributeData': 4,
}

print('\n=== 用已知记录大小反推元素个数 ===')
print(f'{"表名":<36}{"size":>12}{"rec":>5}{"count":>10}  整除')
for i, name in enumerate(NAMES):
    if i >= len(pairs):
        break
    off, size = pairs[i]
    rec = SIZES.get(name)
    if rec is None or size == 0:
        continue
    div, rem = divmod(size, rec)
    mark = '是' if rem == 0 else f'否(余{rem})'
    print(f'  {name:<34}{size:>12,}{rec:>5}{div:>10,}  {mark}')
