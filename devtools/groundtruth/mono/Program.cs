// 独立真值提取器 —— 用微软官方 System.Reflection.Metadata（ECMA-335 官方实现）
// 读取托管程序集并导出 JSON，供 devtools/verify_mono.py 交叉验证自研解析器。
//
// 用法： MonoGroundTruth <assembly.dll> [输出.json]
// 不带输出路径时写到 stdout。

using System;
using System.Collections.Generic;
using System.Collections.Immutable;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Reflection.Metadata;
using System.Reflection.Metadata.Ecma335;
using System.Reflection.PortableExecutable;
using System.Text;
using System.Text.Json;

namespace MonoGroundTruth
{
    /// <summary>把签名里的类型渲染成 BCL 全名，便于与自研解析器逐字比对。</summary>
    internal sealed class NameProvider : ISignatureTypeProvider<string, object>
    {
        private readonly MetadataReader _r;

        public NameProvider(MetadataReader r) { _r = r; }

        public string GetPrimitiveType(PrimitiveTypeCode typeCode)
        {
            switch (typeCode)
            {
                case PrimitiveTypeCode.Void: return "System.Void";
                case PrimitiveTypeCode.Boolean: return "System.Boolean";
                case PrimitiveTypeCode.Char: return "System.Char";
                case PrimitiveTypeCode.SByte: return "System.SByte";
                case PrimitiveTypeCode.Byte: return "System.Byte";
                case PrimitiveTypeCode.Int16: return "System.Int16";
                case PrimitiveTypeCode.UInt16: return "System.UInt16";
                case PrimitiveTypeCode.Int32: return "System.Int32";
                case PrimitiveTypeCode.UInt32: return "System.UInt32";
                case PrimitiveTypeCode.Int64: return "System.Int64";
                case PrimitiveTypeCode.UInt64: return "System.UInt64";
                case PrimitiveTypeCode.Single: return "System.Single";
                case PrimitiveTypeCode.Double: return "System.Double";
                case PrimitiveTypeCode.String: return "System.String";
                case PrimitiveTypeCode.TypedReference: return "System.TypedReference";
                case PrimitiveTypeCode.IntPtr: return "System.IntPtr";
                case PrimitiveTypeCode.UIntPtr: return "System.UIntPtr";
                case PrimitiveTypeCode.Object: return "System.Object";
                default: return "?" + typeCode;
            }
        }

        public string GetTypeFromDefinition(MetadataReader reader, TypeDefinitionHandle handle, byte rawTypeKind)
            => Program.TypeDefFullName(reader, handle);

        public string GetTypeFromReference(MetadataReader reader, TypeReferenceHandle handle, byte rawTypeKind)
            => Program.TypeRefFullName(reader, handle);

        public string GetTypeFromSpecification(MetadataReader reader, object genericContext,
                                               TypeSpecificationHandle handle, byte rawTypeKind)
            => reader.GetTypeSpecification(handle).DecodeSignature(this, genericContext);

        public string GetSZArrayType(string elementType) => elementType + "[]";
        public string GetArrayType(string elementType, ArrayShape shape)
            => elementType + "[" + new string(',', Math.Max(0, shape.Rank - 1)) + "]";
        public string GetByReferenceType(string elementType) => elementType + "&";
        public string GetPointerType(string elementType) => elementType + "*";
        public string GetGenericInstantiation(string genericType, ImmutableArray<string> typeArguments)
            => genericType + "<" + string.Join(", ", typeArguments) + ">";
        public string GetGenericMethodParameter(object genericContext, int index) => "!!" + index;
        public string GetGenericTypeParameter(object genericContext, int index) => "!" + index;
        public string GetFunctionPointerType(MethodSignature<string> signature) => "fnptr";
        public string GetModifiedType(string modifier, string unmodifiedType, bool isRequired)
            => (isRequired ? "modreq(" : "modopt(") + modifier + ") " + unmodifiedType;
        public string GetPinnedType(string elementType) => "pinned " + elementType;
    }

    internal static class Program
    {
        private static MetadataReader R;

        /// <summary>类型定义的全名；嵌套类型用 外层.内层 拼装（与元数据原始短名不同）。</summary>
        public static string TypeDefFullName(MetadataReader reader, TypeDefinitionHandle handle)
        {
            var td = reader.GetTypeDefinition(handle);
            string ns = reader.GetString(td.Namespace);
            string name = reader.GetString(td.Name);

            // Namespace 为空但确实嵌套时，从外层继承命名空间
            var declaring = td.GetDeclaringType();
            if (!declaring.IsNil)
            {
                string outer = TypeDefFullName(reader, declaring);
                return outer + "." + name;
            }
            return string.IsNullOrEmpty(ns) ? name : ns + "." + name;
        }

        public static string TypeRefFullName(MetadataReader reader, TypeReferenceHandle handle)
        {
            var tr = reader.GetTypeReference(handle);
            string ns = reader.GetString(tr.Namespace);
            string name = reader.GetString(tr.Name);
            if (tr.ResolutionScope.Kind == HandleKind.TypeReference)
                return TypeRefFullName(reader, (TypeReferenceHandle)tr.ResolutionScope) + "." + name;
            return string.IsNullOrEmpty(ns) ? name : ns + "." + name;
        }

        /// <summary>把 TypeDefOrRef 编码 token 解析成全名；失败返回 null。</summary>
        private static string ResolveTypeDefOrRef(EntityHandle h)
        {
            if (h.IsNil) return null;
            switch (h.Kind)
            {
                case HandleKind.TypeDefinition: return TypeDefFullName(R, (TypeDefinitionHandle)h);
                case HandleKind.TypeReference: return TypeRefFullName(R, (TypeReferenceHandle)h);
                case HandleKind.TypeSpecification:
                    try { return R.GetTypeSpecification((TypeSpecificationHandle)h).DecodeSignature(new NameProvider(R), null); }
                    catch { return null; }
                default: return null;
            }
        }

        private static int Main(string[] args)
        {
            if (args.Length < 1)
            {
                Console.Error.WriteLine("用法: MonoGroundTruth <assembly.dll> [out.json]");
                return 2;
            }
            string path = args[0];
            if (!File.Exists(path))
            {
                Console.Error.WriteLine("文件不存在: " + path);
                return 2;
            }

            using var fs = File.OpenRead(path);
            using var pe = new PEReader(fs);
            if (!pe.HasMetadata)
            {
                Console.Error.WriteLine("不是托管程序集（无元数据）: " + path);
                return 3;
            }
            R = pe.GetMetadataReader();
            var provider = new NameProvider(R);

            var root = new Dictionary<string, object>();
            // 只记文件名：真值 JSON 是要入库的产物，写绝对路径等于把「生成它的那台机器」
            // 一起提交进去（换台机器这条路径就是死链，且暴露本机目录结构）。
            root["path"] = Path.GetFileName(path);
            root["metadata_version"] = R.MetadataVersion;

            // ---- 各表行数（这是最硬的真值）----
            var counts = new Dictionary<string, object>();
            var tableCounts = new Dictionary<string, int>();
            for (int i = 0; i < 64; i++)
            {
                int n = R.GetTableRowCount((TableIndex)i);
                if (n > 0) tableCounts["0x" + i.ToString("X2")] = n;
            }
            root["table_row_counts"] = tableCounts;

            root["type_defs"] = R.TypeDefinitions.Count;
            root["type_refs"] = R.TypeReferences.Count;
            root["fields"] = R.FieldDefinitions.Count;
            root["methods"] = R.MethodDefinitions.Count;
            root["params"] = R.GetTableRowCount(TableIndex.Param);
            root["nested_classes"] = R.GetTableRowCount(TableIndex.NestedClass);
            root["assembly_refs"] = R.AssemblyReferences.Count;
            root["member_refs"] = R.MemberReferences.Count;
            root["type_specs"] = R.GetTableRowCount(TableIndex.TypeSpec);
            root["generic_params"] = R.GetTableRowCount(TableIndex.GenericParam);
            root["custom_attributes"] = R.GetTableRowCount(TableIndex.CustomAttribute);
            root["method_specs"] = R.GetTableRowCount(TableIndex.MethodSpec);
            root["interface_impls"] = R.GetTableRowCount(TableIndex.InterfaceImpl);
            root["constants"] = R.GetTableRowCount(TableIndex.Constant);

            // ---- 模块 / 程序集 ----
            root["module_name"] = R.GetString(R.GetModuleDefinition().Name);
            if (!R.IsAssembly)
            {
                root["assembly"] = null;
            }
            else
            {
                var ad = R.GetAssemblyDefinition();
                root["assembly"] = new Dictionary<string, object>
                {
                    ["name"] = R.GetString(ad.Name),
                    ["version"] = ad.Version.ToString(),
                    ["culture"] = ad.Culture.IsNil ? "" : R.GetString(ad.Culture),
                };
            }

            // ---- 每个类型：全名、基类、字段与方法签名 ----
            var types = new List<Dictionary<string, object>>();
            foreach (var handle in R.TypeDefinitions)
            {
                var td = R.GetTypeDefinition(handle);
                var item = new Dictionary<string, object>();
                string full = TypeDefFullName(R, handle);
                item["full_name"] = full;
                item["name"] = R.GetString(td.Name);
                item["namespace"] = R.GetString(td.Namespace);
                item["is_nested"] = !td.GetDeclaringType().IsNil;
                item["attributes"] = (int)td.Attributes;
                item["token"] = MetadataTokens.GetToken(handle);

                string baseName = ResolveTypeDefOrRef(td.BaseType);
                item["base_type"] = baseName;

                var fields = new List<Dictionary<string, object>>();
                foreach (var fh in td.GetFields())
                {
                    var fd = R.GetFieldDefinition(fh);
                    string ftype = null;
                    try { ftype = fd.DecodeSignature(provider, null); } catch { }
                    fields.Add(new Dictionary<string, object>
                    {
                        ["name"] = R.GetString(fd.Name),
                        ["type"] = ftype,
                        ["attributes"] = (int)fd.Attributes,
                        ["token"] = MetadataTokens.GetToken(fh),
                    });
                }
                item["fields"] = fields;

                var methods = new List<Dictionary<string, object>>();
                foreach (var mh in td.GetMethods())
                {
                    var md = R.GetMethodDefinition(mh);
                    MethodSignature<string> sig = default;
                    string ret = null;
                    var ps = new List<string>();
                    int genCount = 0;
                    try
                    {
                        sig = md.DecodeSignature(provider, null);
                        ret = sig.ReturnType;
                        foreach (var p in sig.ParameterTypes) ps.Add(p);
                        genCount = sig.GenericParameterCount;
                    }
                    catch { }
                    methods.Add(new Dictionary<string, object>
                    {
                        ["name"] = R.GetString(md.Name),
                        ["return_type"] = ret,
                        ["param_types"] = ps,
                        ["generic_param_count"] = genCount,
                        ["attributes"] = (int)md.Attributes,
                        ["impl_attributes"] = (int)md.ImplAttributes,
                        ["rva"] = md.RelativeVirtualAddress,
                        ["token"] = MetadataTokens.GetToken(mh),
                    });
                }
                item["methods"] = methods;
                types.Add(item);
            }
            root["types"] = types;

            // ---- 嵌套类真值 ----
            var nested = new List<Dictionary<string, object>>();
            foreach (var handle in R.TypeDefinitions)
            {
                var td = R.GetTypeDefinition(handle);
                var dec = td.GetDeclaringType();
                if (dec.IsNil) continue;
                nested.Add(new Dictionary<string, object>
                {
                    ["nested"] = TypeDefFullName(R, handle),
                    ["enclosing"] = TypeDefFullName(R, dec),
                });
            }
            root["nested"] = nested;

            // ---- 堆偏移的无损真值：每条字段/方法签名的原始 blob 字节 ----
            // 自研解析器解出的类型名若与此处的官方解码一致，即为强证据。
            string json = JsonSerializer.Serialize(root, new JsonSerializerOptions { WriteIndented = false });
            if (args.Length >= 2) File.WriteAllText(args[1], json, new UTF8Encoding(false));
            else Console.Out.Write(json);
            return 0;
        }
    }
}
