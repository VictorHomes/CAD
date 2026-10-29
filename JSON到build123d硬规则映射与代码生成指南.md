# JSON 到 build123d 硬规则映射与代码生成指南

> 实现更新：已增加官方 DeepCAD 解析和重建适配入口，以及 `deepcad_compatible` 转换模式。该模式按官方顺序拆分并执行多个 profile；本文推荐的“同特征工具先合并”仍属于原严格模式，不是官方多 profile 求交规则。使用方法与实测结果见 [DeepCAD官方重建与对比使用说明](DeepCAD官方重建与对比使用说明.md)。

> 日期：2026-10-20。目标环境：`D:\CAD\.venv\Scripts\python.exe`，build123d **0.13.0**。
> 本文用于设计确定性的 JSON → Python 转换器。覆盖此前抽样中观察到的字段和本地转换器支持的特征，不声称穷尽全量数据中所有可能的扩展。
> 基础说明见 [DATA_JSON建模规则说明.md](DATA_JSON建模规则说明.md)。本文中的“建议规则”是转换器契约，不能替代尚未确认的原始数据语义。

## 1. 先确定输出和支持范围

输出 `.py` 是能被 Python 执行、导入的 build123d 建模脚本，通常暴露 `build_model()` 和 `result`。build123d 是 Python 库，不需要单独的“导入 Python 文件”格式；在使用同一环境的脚本中执行 `from generated_model import result` 即可取得模型对象。

第一版建议支持：

- 原始 `cad_json`，不接受 `cad_vec`。
- `Sketch` 和 `ExtrudeFeature`。
- `Line3D`、`Circle3D`、`Arc3D`。
- 从草图平面开始、零拔模角的单侧、对称、双侧直拉伸。
- 外环与明确属于该外环的内环。
- 显式配置的实体管理策略和四种 operation。

不支持或无法唯一解释的输入应报告 JSON 路径和原因，不要悄悄忽略并输出看似成功的模型。当前参考转换器有简化行为，不能直接当作严格转换规范。

## 2. 推荐的 build123d 构造路线

```text
JSON 局部点 + transform
→ 全局三维点
→ Edge.make_line / make_circle / make_three_point_arc
→ Wire(edges, sequenced=True)
→ Face(outer_wire, inner_wires)
→ Solid.extrude(face, signed_vector)
→ fuse / cut / intersect
→ Part(最终实体列表)
```

选择 Direct API 的原因是方向、截面和运算对象都可以显式指定。无需依赖 Builder 上下文中“当前待处理草图”等隐式状态。

本文所有具体签名都以本地 0.13.0 为准。官方概念及类定义可查 [Direct API Reference](https://build123d.readthedocs.io/en/stable/direct_api_reference.html)。在线 stable 内容可能更新，本地签名与实际执行结果优先。

## 3. 顶层与序列字段逐项映射

约定：`E = entities[entity_id]`；`S` 表示 Sketch；`F` 表示 ExtrudeFeature；`C` 表示某条曲线。

| JSON 路径 | 转换动作 | build123d 对应 | 必须检查 |
| --- | --- | --- | --- |
| `entities` | 建立 ID 查找表 | 无直接几何对象 | 必须为对象 |
| `entities.<ID>` | 按 type 分派 | 草图描述或特征描述 | 不按字典顺序生成 |
| `sequence` | 按数组顺序遍历 | 按序执行生成语句 | 必须为列表 |
| `sequence[i].index` | 保存用于一致性检查 | 源码注释/metadata | 不擅自按它重排；异常时报告 |
| `sequence[i].type` | 判断 Sketch / ExtrudeFeature | 调度解析器 | 未支持类型拒绝 |
| `sequence[i].entity` | 查找实体 | 取得建模输入 | ID 必须存在 |
| `E.type` | 与序列类型核对 | 类型分派 | 不一致时拒绝 |
| `E.name` | 保存显示名称 | `.label` 或 metadata | 不作为唯一键，不作为原始 Python 标识符 |
| 顶层 `properties` | 保存验证信息 | metadata | 不生成额外实体 |
| `properties.bounding_box.type` | 检查为 `BoundingBox3D` | 无直接构造 | 未知类型报告 |
| `properties.bounding_box.min_point.{x,y,z}` | 读取预期最小点 | `result.bounding_box().min` | 同尺度比较 |
| `properties.bounding_box.max_point.{x,y,z}` | 读取预期最大点 | `result.bounding_box().max` | 同尺度比较 |

`sequence` 中的 Sketch 可先登记，直到 ExtrudeFeature 引用它时才生成截面。相同草图可以被多个特征重复引用，不能使用一次就删除。

## 4. Sketch 字段逐项映射

| JSON 路径 | 对应规则 / Python 表达 | 说明 |
| --- | --- | --- |
| `S.type = Sketch` | 分派至草图解析器 | 草图本身不生成 Solid |
| `S.transform.origin.{x,y,z}` | `O = (ox, oy, oz)` | 长度量，乘统一 scale |
| `S.transform.x_axis.{x,y,z}` | `X = (xx, xy, xz)` | 单位方向，不乘 scale |
| `S.transform.y_axis.{x,y,z}` | `Y = (yx, yy, yz)` | 校验 `Y ≈ N × X` |
| `S.transform.z_axis.{x,y,z}` | `N = (nx, ny, nz)` | 拉伸方向基准 |
| 整个 transform | `Plane(origin=O, x_dir=X, z_dir=N)` | Plane 不接收 y_dir；先验证 Y |
| `S.profiles` | 区域查找表 | 真实样例为字符串 ID 字典 |
| `S.profiles.<profile_id>` | 构造一个区域 | 对应 Face 或区域描述 |
| `...loops[]` | 构造各闭合线框 | 每个 loop 对应 Wire |
| `...loops[j].is_outer` | 外环/孔洞分类 | 决定 Face 的参数位置 |
| `...loops[j].profile_curves[]` | 构造有序曲线 | 对应 Edge 列表 |
| `S.profiles.<id>.properties` | 保存附加数据 | 抽样为空对象；非空未知项报告 |
| `S.reference_plane` | 保存源信息 | 抽样为空对象；本方案以 transform 为平面依据，非空未知内容需核对 |

### 4.1 唯一长度尺度

定义 `SCALE` 为“原始数值到输出长度数值”的倍数。原始单位尚未确认时用 `1.0`，并在输出 metadata 中写 `source_units = 'unknown'`。

```python
O = tuple(SCALE * value for value in raw_origin)
u, v = SCALE * raw_u, SCALE * raw_v
P = tuple(O[i] + u * X[i] + v * Y[i] for i in range(3))
```

坐标只缩放一次。半径、两侧距离、包围盒也乘 SCALE；角度、轴向量不缩放。明确要求 SCALE 为有限正数。

不能仅因数据值类似 `0.0254` 就自动推断单位并乘 1000。STEP 的输出单位设置与源数据单位识别也是两件事。

### 4.2 坐标系规则

先检查 X、Y、N 长度接近 1、互相正交、`Y ≈ N × X`，再构造 Plane。不要依赖 Plane 自动正交化来掩盖无效输入。

局部点转全局点统一使用：

```python
def point_global(u, v, origin, x_axis, y_axis):
    return tuple(origin[i] + u*x_axis[i] + v*y_axis[i] for i in range(3))
```

本路线中，Edge 已经在全局坐标构造，后续不要再给 Wire 或 Face 应用同一个草图变换。

## 5. 所有已观察曲线字段的映射

### 5.1 通用字段

| JSON 字段 | 处理方式 |
| --- | --- |
| `C.type` | 分派到 Line3D / Circle3D / Arc3D；未知类型拒绝 |
| `C.curve` | 源曲线 ID，保留在 metadata；不能自动当作另一条曲线引用去建模 |
| `C.start_point.{x,y}` | 局部起点，缩放后映射到全局 |
| `C.end_point.{x,y}` | 局部终点，缩放后映射到全局 |
| `C.center_point.{x,y}` | 局部圆心，缩放后映射到全局 |
| 上述点的 `z` | 当前数据解释采用草图局部 XY；严格方案要求 z 缺省或接近 0，异常拒绝，不能无条件丢弃非零 z |
| `C.normal.{x,y,z}` | 按草图局部法向信息验证；第一版要求接近局部 +Z，异常拒绝，不能与全局 N 直接混比 |
| `C.radius` | `radius = SCALE * raw_radius`，要求有限且大于 0 |
| `C.reference_vector.{x,y}` | 圆弧零角度方向，不作长度缩放，要求为单位向量 |
| `C.reference_vector.z` | 第一版要求缺省或接近 0 |
| `C.start_angle / end_angle` | 以弧度用于 sin/cos；不直接传给接受角度制的 API |

局部 z 与 normal 的严格拒绝策略是本方案的支持边界。若全量数据出现其他合法约定，应扩展解析规则并验证，不能直接断言样本错误。

### 5.2 Line3D

```python
p0 = point_global(start_u, start_v, O, X, Y)
p1 = point_global(end_u, end_v, O, X, Y)
edge = Edge.make_line(p0, p1)
```

先拒绝长度小于几何容差的退化线段。

### 5.3 Circle3D

```python
center = point_global(center_u, center_v, O, X, Y)
circle_plane = Plane(origin=center, x_dir=X, z_dir=N)
edge = Edge.make_circle(radius, plane=circle_plane)
wire = Wire([edge])
```

完整圆不需要起终点和角度参数。本方案拒绝负半径；这与参考转换器取绝对值的宽松行为不同。一个完整圆独占一个 loop。

### 5.4 Arc3D

保留原始角区间，计算局部中间点：

```python
from math import sin, cos

angle_mid = (start_angle + end_angle) / 2
mid_u = center_u + radius * (ref_x*cos(angle_mid) - ref_y*sin(angle_mid))
mid_v = center_v + radius * (ref_x*sin(angle_mid) + ref_y*cos(angle_mid))

p0 = point_global(start_u, start_v, O, X, Y)
pm = point_global(mid_u, mid_v, O, X, Y)
p1 = point_global(end_u, end_v, O, X, Y)
edge = Edge.make_three_point_arc(p0, pm, p1)
```

这里 center、radius 和端点都应已经统一缩放，ref 不缩放。必须检查 JSON 端点与圆心、角区间推算结果一致；方向修复翻转端点时保留同一条几何弧上的 pm，不要只交换角度造成另一个弧段。

第一版将近零扫角、近整圆扫角、绝对扫角超过一周、三点共线等情况作为显式不支持项。完整圆优先由 Circle3D 表达。若要扩展这些情况，需另外定义分段规则。

## 6. loop → Wire → Face 的硬规则

1. 每个 loop 至少有一条曲线。
2. 单条 Circle3D 可以独立闭合；Circle3D 不与其他曲线混用。
3. 其余曲线必须按相邻端点连通。允许翻转边方向，但不允许任意填补缺口。
4. 建议先按源顺序修复方向；仍不连通时拒绝。若实现拓扑重排，端点候选必须唯一，分叉时拒绝。
5. 连接容差使用转换后坐标的长度单位，并在配置中记录；不要用固定容差同时处理所有尺度。
6. 通过边链检查后构造 `Wire(edges, sequenced=True)`，继续检查 `wire.is_closed`、`wire.is_valid`。
7. 第一版每个 profile 要求恰好一个外环，零个或多个内环。内环必须位于外环内部，互不相交且不与外环相交。
8. 多个外环或更深嵌套区域在没有明确分组策略时拒绝；不要按包围盒排序猜测。

对应的核心代码是：

```python
outer_wire = Wire(outer_edges, sequenced=True)
inner_wires = [Wire(edges, sequenced=True) for edges in hole_edge_lists]
face = Face(outer_wire=outer_wire, inner_wires=inner_wires)
if not face.is_valid or face.area <= AREA_TOL:
    raise ValueError('Invalid or degenerate profile face')
```

`Face` 负责按内线框构造孔洞。本地已验证同方向构造的两个同心圆可以形成正确的环形面，因此不要照搬 CadQuery 输出中的 `.wrapped.Reversed()`；应检查最终面面积和孔洞拓扑。

本节是规则说明，`outer_edges、hole_edge_lists、AREA_TOL` 来自解析器，不是可独立运行的完整脚本。

## 7. ExtrudeFeature 字段逐项映射

| JSON 路径 | 转换为 | 处理规则 |
| --- | --- | --- |
| `F.type` | 拉伸特征分派 | 必须为 ExtrudeFeature |
| `F.profiles[]` | 本特征的截面列表 | 处理全部引用 |
| `F.profiles[k].sketch` | 查找 S | 检查对象存在且为 Sketch |
| `F.profiles[k].profile` | 查找 S.profiles 中的区域 | 字符串字典键；兼容列表时显式判断整数下标 |
| `F.start_extent.type` | 起始位置 | 仅接受 ProfilePlaneStartDefinition |
| `F.extent_type` | 选择向量集合 | 见下一节 |
| `F.extent_one.type` | 距离定义检查 | 第一版仅接受 DistanceExtentDefinition |
| `F.extent_two.type` | 第二侧距离定义检查 | 双侧使用时必须受支持 |
| `F.extent_one.distance.value` | `d1` | 有限浮点数，乘 SCALE，保留符号 |
| `F.extent_two.distance.value` | `d2` | 双侧必须读取，乘 SCALE，保留符号 |
| `...distance.type` | 参数元数据校验 | 已观察为 ModelParameter |
| `...distance.role` | 保存原字段 | AlongDistance / AgainstDistance 不替代符号计算 |
| `...distance.name` | 保存原字段 | 可为 none；不能直接作为 Python 变量名 |
| `...taper_angle.value` | 拔模角 | 第一版要求两侧均为 0；非零拒绝 |
| `...taper_angle.type / role / name` | 保存元数据 | 不生成额外几何 |
| `F.operation` | 操作分派 | 见实体管理规则 |

对于未参与当前范围计算的 `extent_two`，仍可保留元数据。若存在非零拔模角或未知定义，不应因“当前侧不用”就自动认定不影响语义；严格模式应报告异常并明确处理策略。

空 profiles 建议第一版默认拒绝；如果需要与本地参考转换器对齐，可以显式设置 `empty_profile_policy='skip_with_warning'`，记录跳过的特征。不能默默跳过。

## 8. 拉伸规则到 build123d 的精确对应

统一使用：

```python
solid = Solid.extrude(face, direction=vector)
```

这里 `vector` 同时指定方向和长度，不需要再传一个 amount。避免依赖 Face 的隐含法向决定正负。

| JSON extent_type | 生成向量列表 |
| --- | --- |
| OneSideFeatureExtentType | `[d1 * N]` |
| SymmetricFeatureExtentType | `[d1 * N, -d1 * N]` |
| TwoSidesFeatureExtentType | `[d1 * N, -d2 * N]` |

```python
def extent_vectors(extent_type, d1, d2, normal):
    mul = lambda d: tuple(d * v for v in normal)
    if extent_type == 'OneSideFeatureExtentType':
        return [mul(d1)]
    if extent_type == 'SymmetricFeatureExtentType':
        return [mul(d1), mul(-d1)]
    if extent_type == 'TwoSidesFeatureExtentType':
        return [mul(d1), mul(-d2)]
    raise ValueError(f'Unsupported extent_type: {extent_type}')
```

向量生成规则与当前本地参考转换器一致。对称时 d1 是每侧长度，不是总长的一半待再次计算。双侧负距离可能使两段落在同一侧。

处理零长度段时，明确使用策略：省略长度在容差内的段；如果所有段都为零则拒绝特征。两侧生成的几何必须先求并，再作为一次完整特征应用 operation。

不要使用 `abs(d1)` 替代有符号距离，也不要只根据 `face.normal_at()` 推导源草图的 N。

### 8.1 与 Builder 模式的概念对应

| JSON 含义 | Builder 概念 | 本指南实际采用 |
| --- | --- | --- |
| 草图平面 | BuildSketch(Plane(...)) | 全局坐标 Edge + Face |
| 线段、圆弧、圆 | Line / ThreePointArc / Circle | Edge.make_* |
| 截面 | make_face / Sketch | Face(outer, holes) |
| 直拉伸 | extrude | Solid.extrude(face, vector) |
| 合并/切除/求交 | Mode.ADD / SUBTRACT / INTERSECT | 显式 fuse / cut / intersect |

Builder 路线可实现同样的几何，但不要将两条路线混用导致重复坐标变换或重复累计。官方 [BuildPart 文档](https://build123d.readthedocs.io/en/stable/build_part.html) 也说明了直接用 Face 拉伸时法向可能反向的问题，因此这里固定使用显式向量。

## 9. 布尔返回值必须归一化

本地 0.13.0 实测：`fuse()` 和 `cut()` 可以返回 Solid 或 Compound；`intersect()` 返回 ShapeList 或 None。不能假设 `a.intersect(b)` 仍然是一个 Solid。

下面的辅助函数统一收集三维实体，并拒绝非空但只有面、边或点的结果：

```python
from build123d import Part

def as_solids(value):
    if value is None:
        return []
    if hasattr(value, 'solids'):
        solids = list(value.solids())
        if not solids:
            raise ValueError('Expected 3D solids, got lower-dimensional geometry')
        return solids
    solids = []
    for item in value:
        solids.extend(as_solids(item))
    return solids

def boolean_part(left, right, operation):
    if operation == 'join':
        raw = left.fuse(right)
    elif operation == 'cut':
        raw = left.cut(right)
    elif operation == 'intersect':
        raw = left.intersect(right)
    else:
        raise ValueError(operation)
    solids = as_solids(raw)
    return Part(solids) if solids else None
```

空结果与运算失败不同，应定义策略：允许几何上合法的空交集，但模型最终为空时是否允许输出需要显式配置；异常应附加特征 ID 后向上报告，不能当作空结果吞掉。

## 10. 特征合并与实体管理策略

### 10.1 一个特征引用多个 profile

推荐的转换契约：先生成本次所有 profile、所有侧向的拉伸，再组合成完整工具集合，最后应用一次特征 operation。

```text
feature_segments = 所有 profile 的所有非零拉伸段
feature_tool = feature_segments 的几何并集
model_next = operation(model_previous, feature_tool)
```

对 Join 和 Cut，部分逐个运算的情况可以得到相同结果；对 Intersect，逐个相交会变成 `body ∩ A ∩ B`，通常不等于 `body ∩ (A ∪ B)`。不要原样复制参考转换器的逐 profile 求交方式。

这里的“一次完整工具”是本转换器的明确约定，尚未用官方重建器验证所有多 profile 求交样本。

### 10.2 选择一个显式的操作模式

| 模式 | NewBody | Join/Cut/Intersect | 适用场景 |
| --- | --- | --- | --- |
| `reference_compatible` | 后续新建也并入累计 body | 对累计 body 运算；首个非空特征直接初始化 | 对齐当前本地参考转换器的总体简化行为 |
| `strict_single_body` | 仅允许初始化；后续新建拒绝 | 必须已有目标，结果若分裂为多实体则报告或按策略拒绝 | 第一版规则清晰的单实体转换 |
| `explicit_multibody` | 保存新实体组，不自动融合已有实体 | 按外部提供的目标组/实体映射操作 | 需要保留独立实体语义 |

**建议默认 strict_single_body。** 选择 reference_compatible 时，必须在输出中标记其语义简化，不能称为完整保留 NewBody。即使选择该模式，也应注明多 profile 特征工具聚合与原参考实现的差异。

已观察字段中没有明确、通用的目标实体 ID 列表。explicit_multibody 需要额外的目标配置或进一步核对来源规则；不能按“最近实体”“全部实体”“第一个相交实体”自动猜测而不记录策略。

纯显示多个独立实体可以使用 `Part(solids)` 或 Compound。它们是包装容器，不等于调用 fuse，也不代表保存了可在任意 CAD 软件中编辑的完整特征树。

## 11. 可直接运行的生成结果示例

以下代码可作为 `0000/00000007.json` 的目标输出。保留原始长度数值，没有推断单位；参数集中定义，便于修改。

```python
"""Generated from 0000/00000007.json; source units unknown."""
from pathlib import Path
from build123d import Edge, Wire, Face, Plane, Solid, Part, export_step

SCALE = 1.0
SOURCE_ENTITY = 'FI4bCL9y0XvsF52'
feature_001_radius = 0.09104851 * SCALE
feature_001_extent_one = 0.0254 * SCALE

def build_model():
    # Sketch FQgWGf8WhgalpUy / profile JGC
    plane = Plane(origin=(0, 0, 0), x_dir=(1, 0, 0), z_dir=(0, 0, 1))
    edge = Edge.make_circle(feature_001_radius, plane=plane)
    wire = Wire([edge])
    face = Face(outer_wire=wire, inner_wires=[])
    solid = Solid.extrude(face, direction=(0, 0, feature_001_extent_one))
    solid.label = SOURCE_ENTITY
    model = Part([solid])
    if not model.is_valid or len(model.solids()) != 1:
        raise ValueError('Generated model is invalid')
    return model

result = build_model()

if __name__ == '__main__':
    destination = Path(__file__).with_suffix('.step')
    if not export_step(result, destination):
        raise RuntimeError('STEP export failed')
    print(destination)
```

将代码保存为 `generated_model.py` 后执行：

```powershell
D:\CAD\.venv\Scripts\python.exe D:\CAD\generated_model.py
```

或者在同目录其他 Python 文件中：

```python
from generated_model import result
print(result.volume)
```

执行脚本会导出同名 STEP；导入模块会构建 result，但不会触发 `__main__` 中的导出。STEP 只是几何交换结果，不保留这份 Python 的参数逻辑。源单位未确认时，导出的 STEP 物理尺度仍需用户确认。

## 12. 代码生成器的分层与伪代码

```text
parse_document(json)
    校验 entities、sequence、所有 ID 与已支持类型
    建立有序特征 IR，不立即写零散源码

parse_profile(sketch_id, profile_id)
    校验 transform 与长度尺度
    解析曲线，检查曲线参数一致性
    校验环连接、内外环及包含关系
    返回 PlaneIR + LoopIR[]

parse_feature(entity_id)
    解析所有 profile 引用
    校验起始类型、距离类型、零拔模角
    保留有符号距离和 operation
    返回 FeatureIR

emit_python(model_ir, settings)
    写固定 import、SCALE、辅助函数
    生成 deterministic 参数名与变量名
    每个特征生成完整截面和拉伸工具
    按显式实体策略应用 operation
    写 build_model、result、可选导出入口

validate_generated_python(path)
    检查 Python 语法
    在固定环境执行
    检查实体有效性、数量、尺寸和体积
    检查 STEP 导出再导入
```

IR 建议字段：

```text
ModelIR: source_id, scale, source_units, policy, features[], expected_bbox
FeatureIR: sequence_index, entity_id, name, operation, extent_type, d1, d2, profiles[]
ProfileIR: sketch_id, profile_id, origin, X, Y, N, loops[]
LoopIR: is_outer, curves[]
CurveIR: type, source_curve_id, start, end, center, radius, ref, angle0, angle1
```

### 12.1 源码生成约束

- 用 `feature_003_extent_one`、`feature_003_profile_001_wire` 等确定性变量名。
- 原 ID 只放在字符串、label、注释或 metadata，不直接拼为 Python 标识符。
- 字符串使用可靠的 Python 字面量转义，例如 `repr(value)`；数值先验证有限性再输出 `repr(float(value))`。
- 保留足够精度，不将坐标统一截断成六位小数。
- 相同输入与配置必须生成相同的几何参数和命名。
- 输出文件标注 build123d 版本、转换策略、原始样本 ID 和单位状态。
- 生成结果应自包含；若依赖通用 runtime，必须同时交付 runtime 并固定其版本。

## 13. 支持边界与错误契约

| 触发条件 | 建议处理 |
| --- | --- |
| 缺少必需字段或引用 | 拒绝，定位到 JSON 路径 |
| 未知特征/曲线/起始定义 | 拒绝，不替换为相近操作 |
| 非正半径、非有限值 | 拒绝 |
| 非正交坐标系或非平面曲线 | 拒绝或进入经过验证的扩展解析器 |
| 开放环、自交环、孔洞越界 | 拒绝，不填补缺口 |
| 多外环、嵌套归属不明 | 第一版拒绝，后续实现分组策略 |
| 非零 taper_angle | 第一版拒绝，不生成直拉伸冒充 |
| 空 profiles | 默认拒绝；兼容模式可警告并跳过 |
| 所有方向长度为零 | 拒绝退化特征 |
| 首个 Cut / Intersect | 严格模式拒绝，无目标实体 |
| 目标实体不明确 | 要求配置，不默猜 |
| CAD 内核抛出异常 | 加上样本 ID、sequence、entity、profile、loop 后报告 |
| 出现未知非空字段 | 报告；确认是元数据后加入允许列表 |

错误示例：

```text
sample=0097/00970012
sequence=3
entity=...
path=entities.<sketch>.profiles.<profile>.loops[0].profile_curves[2]
code=OPEN_WIRE
message=Previous edge endpoint does not match this edge start within tolerance
```

已知元数据可以保存而不参与建模，但不能把“没有直接 API 对应”一概等同于“可忽略”。例如包围盒用于验证，curve ID 用于追踪，拔模角则直接改变几何。

## 14. 验证清单与本次实测

转换器的最小几何验收应包括：

1. `is_valid` 为真，实体数量符合所选策略。
2. 带孔截面的面积和拉伸体积正确。
3. 负距离、对称和双侧拉伸的端点范围正确。
4. 倾斜平面的法向、位置与 JSON transform 一致。
5. 同一特征多 profile 的运算边界正确，尤其是 Intersect。
6. 结果包围盒与 JSON 的缩放后包围盒在容差内一致。
7. STEP 导出再读取后，有效性、体积和包围盒稳定。

包围盒一致不足以证明孔洞或局部细节一致，有参考几何时需额外比较。

本次已在本地 build123d 0.13.0 上运行以下针对性检查：

| 检查 | 结果 |
| --- | --- |
| 同心圆外环 r=4、内环 r=1 构造 Face | 成功 |
| 上述面双侧各拉伸 2 后求并 | 有效，体积等于 `π × (4²−1²) × 4` |
| 三点半圆弧加直线封闭成面 | 面积等于 `π/2` |
| 倾斜平面圆面沿全局 Y 拉伸 | 生成有效实体 |
| fuse / cut 返回类型 | 样例返回 Solid |
| intersect 返回类型 | 样例返回包含 Solid 的 ShapeList |

另外，已执行第 11 节完整示例：实体有效，体积为 `0.0006614991269175131`（原始数值单位的三次方），与理论圆柱体积一致；Z 最大值为 `0.0254`；STEP 导出再读取后仍有效且体积一致。第 9 节布尔辅助函数也已验证 Join、Cut、Intersect 的有效性及预期体积；文档中 11 个 Python 代码块均通过语法编译。局部片段仍需解析器提供其中的输入变量，不能作为独立脚本直接执行。

这些检查验证 API 和代表性几何组合，不等于已实现全量 JSON 转换器，也不代表全量样本已通过几何验证。验证产生的示例脚本位于 `tmp/json_build123d_doc_check/generated_model.py`，对应 STEP 位于同目录。

## 15. 参考来源

- [本地规则说明](DATA_JSON建模规则说明.md)：数据结构与 500 个抽样文件的观察结果。
- [本地参考转换器](DeepCAD_to_build123d_SimpleCADAPI_handoff_with_dataset/backend/deepcad_workbench/converter.py)：现有解析与 CadQuery 输出行为。
- [本地移植说明](DeepCAD_to_build123d_SimpleCADAPI_handoff_with_dataset/docs/PORTING_GUIDE.md)：后端划分与已知限制。
- [build123d Direct API Reference](https://build123d.readthedocs.io/en/stable/direct_api_reference.html)：Plane、Edge、Wire、Face、Solid 等 API 定义。
- [build123d BuildPart](https://build123d.readthedocs.io/en/stable/build_part.html)：Builder 模式与拉伸方向说明。

具体签名、布尔返回值和本指南的代表性几何检查，以当前安装的 0.13.0 实测为依据。升级 build123d 后应重新核对这些接口和行为。
