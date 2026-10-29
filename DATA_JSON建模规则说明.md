# data 文件夹 JSON 建模规则说明

> 整理日期：2026-10-20。依据：本地实际 JSON、本地 `converter.py` 和移植说明。
> 本文区分“数据表达的含义”“本地转换器的具体行为”和“后续实现建议”。尚未确认的事项不作为数据集保证。

## 1. 核心结论

这些 JSON 保存了 CAD 模型的构建历史。重建模型的主要过程是：读取草图，恢复二维区域及其三维位置，将区域拉伸成实体，再按照建模顺序执行新建、合并、切除或求交。

```text
sequence：确定操作顺序
  ↓ 按 entity ID 查找
entities：取得 Sketch / ExtrudeFeature
  ↓ 读取拉伸的 profiles 引用
指定草图中的指定 profile
  ↓
loops → profile_curves → 闭合边界 → 带孔或无孔的面
  ↓ 应用草图 transform
在正确的三维平面上构造截面
  ↓ 应用 extent_type 和有符号距离
生成本次拉伸实体
  ↓ 应用 operation
更新模型，继续执行下一个特征
```

一个 JSON 通常对应一个模型样本，但一个模型可以包含多个草图、多个拉伸特征和多个实体。不能默认“一份 JSON 只有一次拉伸”或“一个草图只包含一个区域”。

## 2. 文件位置与数据划分

| 路径 | 用途 |
| --- | --- |
| `data/cad_json/cad_json/0000/*.json` 等编号目录 | CAD 模型及建模历史 |
| `data/train_val_test_split.json` | 训练、验证和测试样本 ID 划分 |

当前本地模型目录共有 **215,093 个 JSON 文件**，分布在 100 个编号子目录中。

划分文件的统计如下：

| 集合 | ID 数量 |
| --- | ---: |
| train | 161,240 |
| validation | 8,946 |
| test | 8,052 |
| 合计 | 178,238 |

例如 ID `0067/00675619` 对应 `data/cad_json/cad_json/0067/00675619.json`。

目录文件数与划分 ID 总数不相等。进行训练或评估时，应使用相应划分中的 ID，不应自动将目录中的全部模型当作训练集。这里未进一步审计划分 ID 的重复、交集或文件缺失情况。

## 3. 顶层结构与执行顺序

```json
{
  "entities": {},
  "properties": {},
  "sequence": []
}
```

| 字段 | 内容 | 建模用途 |
| --- | --- | --- |
| `entities` | 实体 ID 到对象的映射 | 存储草图和拉伸定义 |
| `sequence` | 有序操作记录 | 决定重建顺序 |
| `properties` | 附加属性，例如 `bounding_box` | 结果参考和校验 |

### 3.1 sequence

真实样例 `0000/00000007.json` 的操作顺序：

```json
[
  {"index": 0, "type": "Sketch", "entity": "FQgWGf8WhgalpUy"},
  {"index": 1, "type": "ExtrudeFeature", "entity": "FI4bCL9y0XvsF52"}
]
```

规则：

1. 按 `sequence` 数组顺序解释建模历史。本地转换器按数组遍历，并不依据 `index` 重新排序。
2. `entity` 是查找 `entities` 的键，不是几何参数。
3. `entities` 的键排列顺序不代表建模顺序。
4. 草图定义截面，本身不会生成三维实体。
5. 本地转换器遇到 `Sketch` 记录会跳过实体生成，在拉伸引用该草图时才读取其几何。
6. `name` 是可读名称；解析引用时应使用 ID，不能依赖名称唯一或连续编号。

## 4. 草图 Sketch 的结构

```text
Sketch
├── type / name
├── transform
│   ├── origin
│   ├── x_axis
│   ├── y_axis
│   └── z_axis
├── profiles
│   └── profile ID
│       ├── loops[]
│       │   ├── is_outer
│       │   └── profile_curves[]
│       └── properties
└── reference_plane
```

| 概念 | 含义 |
| --- | --- |
| Sketch | 一个草图，可包含多个二维区域 |
| Profile | 可被拉伸引用的二维区域 |
| Loop | 区域的一条闭合边界，可为外环或孔洞边界 |
| Curve | 组成闭合边界的一段直线、圆弧或完整圆 |

真实数据中，`Sketch.profiles` 通常为以字符串 ID 为键的字典。本地转换器也兼容列表形式，但列表要求使用合法的整数下标。不能将真实数据的字符串 profile ID 强制转换为整数。

## 5. 草图局部坐标与三维变换

### 5.1 坐标公式

设局部点为 `(u, v)`，草图原点为 `O`，三个方向轴分别为 `X、Y、N`：

```text
P_global = O + u × X + v × Y
N = transform.z_axis
```

`origin` 决定平面位置；`x_axis、y_axis` 决定平面内方向；`z_axis` 是法向，也是拉伸方向的基准。

例如：

```text
O = (5, 6, 7)
X = (1, 0, 0)
Y = (0, 0, -1)
N = (0, 1, 0)

局部点 (2, 3) → 全局点 (7, 6, 4)
```

虽然曲线类型名带有 `3D`，本地转换器读取曲线点的局部 `x、y`，再利用草图变换生成三维点。不要在已应用草图平面的情况下再次变换同一个点。

### 5.2 本地转换器的坐标校验

- 坐标轴必须为单位向量，长度偏差容差为 `1e-4`。
- 轴之间必须正交，点积偏差容差为 `1e-4`。
- 必须满足右手系：`Y = N × X`，向量差容差为 `1e-4`。
- 数字必须能转换为有限浮点数，拒绝无穷大和 NaN。

这些容差是当前实现的选择，不代表 JSON 格式规定的统一精度标准。

## 6. 曲线规则

### 6.1 Line3D：直线段

主要字段：`start_point、end_point、type`。

```json
{
  "type": "Line3D",
  "start_point": {"x": 0, "y": 0, "z": 0},
  "end_point": {"x": 1, "y": 0, "z": 0}
}
```

将两个局部端点转换到三维，连接为边。曲线方向影响相邻边的首尾连接。

### 6.2 Circle3D：完整圆

主要字段：`center_point、radius、normal、type`。

完整圆可以单独构成一个闭合 loop，不需要起点和终点。本地转换器使用草图法向构造三维圆，使用 `abs(radius)` 并拒绝零半径。严格数据校验也可以选择拒绝负半径，而不是自动纠正。

本地转换器不允许完整圆和其他曲线混在同一个 loop 中。

### 6.3 Arc3D：圆弧

主要字段：

| 字段 | 用途 |
| --- | --- |
| `start_point / end_point` | 弧段端点 |
| `center_point` | 圆心 |
| `radius` | 半径 |
| `reference_vector` | 定义角度零位的参考方向 |
| `start_angle / end_angle` | 角度区间 |
| `normal` | 曲线法向信息 |

本地转换器按弧度计算，令 `a = (start_angle + end_angle) / 2`，参考向量为 `(rx, ry)`，圆心为 `(cx, cy)`，则：

```text
mid_x = cx + radius × (rx × cos(a) − ry × sin(a))
mid_y = cy + radius × (rx × sin(a) + ry × cos(a))
```

随后将起点、中间点和终点变换到三维，用三点圆弧构造边。角度区间不能随意取绝对值、交换或对 2π 取模，这些操作可能改变弧段经过的一侧或长短弧选择。

当前实现没有完整验证端点、半径、参考向量和角度是否互相一致；严格解析器应额外检查。

## 7. 闭合环、区域与孔洞

### 7.1 闭合条件

普通曲线环要求前一条曲线的终点与后一条的起点连接，最后一条曲线的终点回到第一条曲线的起点。完整圆可直接闭合。

本地转换器会尝试翻转部分边的端点方向，再检查连接：方向修复使用 `1e-6` 距离容差，连接和闭合检查使用 `1e-5`。它保留原有曲线的相邻关系，只循环移动起始边，不会将任意乱序曲线重新搜索排列成闭环。

### 7.2 外环与内环

`is_outer: true` 表示外轮廓，`is_outer: false` 表示内轮廓。典型区域为：

```text
profile = 外环围成的区域 − 内环围成的孔洞
```

例如矩形外环加圆形内环，形成带圆孔的矩形截面；拉伸这个截面可直接得到带孔零件。

### 7.3 当前实现的限制

当前转换器没有按 `is_outer` 直接分类，而是按 loop 包围盒的最小坐标排序，将第一个环作为外环，其余作为内环，并在输出中反转内环方向。

这是排序启发式，不是通用的包含关系算法。复杂、嵌套或异常轮廓可能不满足其假设。后续实现应结合 `is_outer`、轮廓包含关系及目标 CAD 内核的面构造规则进行校验。

## 8. ExtrudeFeature：截面引用与起始位置

```json
{
  "type": "ExtrudeFeature",
  "profiles": [{"sketch": "草图ID", "profile": "区域ID"}],
  "start_extent": {"type": "ProfilePlaneStartDefinition"},
  "extent_type": "OneSideFeatureExtentType",
  "extent_one": {"distance": {"value": 2.0}},
  "extent_two": {"distance": {"value": 0.0}},
  "operation": "NewBodyFeatureOperation"
}
```

上面的结构用于展示关键字段，省略了实际文件可能包含的参数元数据和拔模角。

### 8.1 引用解析

```python
extrude = entities[sequence_item["entity"]]
for ref in extrude["profiles"]:
    sketch = entities[ref["sketch"]]
    profile = sketch["profiles"][ref["profile"]]
```

一次拉伸可以引用多个区域，应处理完整的引用列表。未被拉伸引用的草图或区域不会仅凭存在于 `entities` 中就自动生成实体。

### 8.2 起始方式

本地转换器仅支持 `ProfilePlaneStartDefinition`，即从草图平面开始拉伸。其他起始定义会被拒绝，不能直接当作零偏移处理。

### 8.3 参数值与元数据

距离通常位于 `extent_one.distance.value` 和 `extent_two.distance.value`。包裹对象还可能包含 `type: ModelParameter`、`role`、`name` 等元数据。参与当前几何计算的是 `value`，元数据可以保留用于解释和追踪。

## 9. 三种拉伸范围及正负号

设草图法向为 `N`，第一侧距离为 `d1`，第二侧距离为 `d2`。以下是本地转换器明确实现的规则。

| 类型 | 第一段拉伸向量 | 第二段拉伸向量 | 构造方式 |
| --- | --- | --- | --- |
| `OneSideFeatureExtentType` | `d1 × N` | 无 | 生成单段实体 |
| `SymmetricFeatureExtentType` | `d1 × N` | `−d1 × N` | 两段实体求并 |
| `TwoSidesFeatureExtentType` | `d1 × N` | `−d2 × N` | 两段实体求并 |

### 9.1 单侧拉伸

`d1 = 3` 表示沿法向拉伸 3；`d1 = −3` 表示沿反法向拉伸 3。不能预先取绝对值。

### 9.2 对称拉伸

按当前实现，`d1` 是每一侧的距离。例如 `d1 = 2` 时覆盖法向坐标 `[-2, 2]`，总厚度为 4。`extent_two` 不参与当前对称拉伸计算。

### 9.3 双侧拉伸

`d1 = 3、d2 = 1` 时覆盖 `[-1, 3]`。若距离带有负号，两段拉伸可能落在草图平面的同一侧。

例如真实样例 `0008/00080001.json` 中有一次双侧切除：`d1 = −0.025、d2 = 0.003`。当前实现生成的两个向量分别是 `−0.025N` 与 `−0.003N`；两段都位于反法向侧，不能用两者绝对值之和表示工具实体厚度。

### 9.4 拔模角

实际文件可包含 `extent_one.taper_angle.value` 和 `extent_two.taper_angle.value`。本地转换器未读取这些角度，始终生成直拉伸。本次抽样的拔模角均为 0，不代表全量文件均为 0。遇到非零拔模角时，应实现相应几何或明确报错。

## 10. 布尔操作与实体管理

| operation | 数据表达的含义 | 单个累计结果下的常见计算 |
| --- | --- | --- |
| `NewBodyFeatureOperation` | 新建实体 | 创建并保存一个新实体 |
| `JoinFeatureOperation` | 合并 | `body ∪ tool` |
| `CutFeatureOperation` | 切除 | `body − tool` |
| `IntersectFeatureOperation` | 求交 | `body ∩ tool` |

孔洞既可以来自截面中的内环，也可以来自后续切除特征。这两种方式可以得到相近的最终外形，但建模历史不同。

当前转换器的具体行为：

1. 第一个有效的拉伸结果直接赋给 `body`，没有按首个特征的 operation 分别处理。
2. 后续 `NewBodyFeatureOperation` 和 `JoinFeatureOperation` 都执行 `fuse`。
3. `CutFeatureOperation` 执行 `cut`，`IntersectFeatureOperation` 执行 `intersect`。
4. 一个特征引用多个 profile 时，会逐个生成并应用操作；新建操作中第二个及之后的 profile 在特征摘要中记为 Join。

因此，当前输出没有完整保留独立新实体的管理语义。要重建可编辑的多实体模型，应维护实体集合，并明确布尔运算的目标实体。JSON 中已观察到的字段是否足以无歧义恢复所有目标关联，仍需进一步核对，不能直接假设。

## 11. 单位、精度和包围盒

### 11.1 单位

所检查的示例没有显式长度单位字段，当前转换器原样使用数值。工作区入门示例采用毫米，不能据此证明数据集也采用毫米。

在确认单位之前保留原始数值；需要转换单位时，统一缩放原点、局部点坐标、半径和拉伸距离。方向轴和角度不随长度单位缩放。

### 11.2 精度

保留浮点数，不要在建模前随意四舍五入。当前转换器的环排序会使用六位小数，但它不代表所有几何坐标都应截断到六位。

容差与数值尺度有关；如果改变长度尺度，也应重新评估闭合和连接容差。

### 11.3 包围盒

`properties.bounding_box` 包含 `min_point、max_point`，可与重建模型对比：

```text
尺寸 = max_point − min_point
```

包围盒可用于检查方向、尺度和整体尺寸，但不能单独证明模型一致：孔洞错误或局部缺失可能不改变外包围盒。

## 12. 真实示例：00000007.json

源文件：[查看 JSON](data/cad_json/cad_json/0000/00000007.json)。

| 项目 | 值 |
| --- | --- |
| 草图 ID | `FQgWGf8WhgalpUy` |
| 拉伸 ID | `FI4bCL9y0XvsF52` |
| profile ID | `JGC` |
| 草图原点 | `(0, 0, 0)` |
| 草图坐标轴 | 标准 XYZ 方向 |
| 曲线 | 一个完整圆 |
| 圆心 | `(0, 0)` |
| 半径 | `0.09104851` |
| 拉伸类型 | `OneSideFeatureExtentType` |
| 拉伸距离 | `0.0254` |
| 操作 | `NewBodyFeatureOperation` |

重建步骤：

1. 读取 sequence 中的草图记录和拉伸记录。
2. 根据拉伸的引用找到草图中的 `JGC` 区域。
3. 用其中的圆构造闭合边界和圆形面。
4. 按 transform 放在 XY 平面原点。
5. 沿正 Z 方向拉伸 `0.0254`。
6. 保存为初始实体。

结果是圆柱，直径为 `0.18209702`，高度为 `0.0254`，Z 范围为 `[0, 0.0254]`。JSON 包围盒与此基本吻合，存在很小的数值差异。以上均为原始数值单位。

其他可用于理解规则的真实文件：

| 文件 | 可观察的内容 |
| --- | --- |
| [00000061.json](data/cad_json/cad_json/0000/00000061.json) | 直线与圆弧 |
| [00000062.json](data/cad_json/cad_json/0000/00000062.json) | 对称拉伸、多个 profile、负距离 |
| [00010001.json](data/cad_json/cad_json/0001/00010001.json) | 新建、合并和切除的顺序 |
| [00080001.json](data/cad_json/cad_json/0008/00080001.json) | 双侧拉伸与负距离 |

## 13. 推荐的重建实现流程

以下为后续实现建议，不代表工作区已有完整实现。

```text
读取 JSON
→ 校验顶层结构、引用及数值
→ 按 sequence 遍历
→ 解析拉伸引用的草图与区域
→ 校验草图坐标系
→ 构造曲线并检查端点连接
→ 分类外环、内环并构造面
→ 按范围类型生成完整拉伸工具
→ 应用特征操作并维护实体集合
→ 检查最终几何
→ 导出 STEP 或其他目标格式
```

建议将实现分为三层：

1. **解析层**：读取 DeepCAD JSON 并验证引用、参数和几何条件。
2. **中间模型层**：保存有序特征、草图平面、曲线、内外环、有符号距离和操作类型。
3. **后端层**：通过 build123d、CadQuery 或其他内核生成实体。

解析逻辑与 CAD 后端分离后，可复用同一套规则。中间表示应保留 entity ID、profile ID 和 sequence 位置，方便追踪失败样本。

对于一个特征的多个 profile，可以先收集该特征的几何，再依据目标语义应用操作；尤其是求交，逐个求交与先合并工具再求交并不等价。多区域和多实体情况需要专门验证。

## 14. 错误与边界情况处理

| 情况 | 当前行为或建议 |
| --- | --- |
| 缺少 entities / sequence | 当前转换器拒绝 |
| 输入为 cad_vec | 当前转换器拒绝；它不是原始 cad_json |
| 引用不存在的实体或 profile | 当前转换器拒绝 |
| 未支持的特征或曲线类型 | 当前转换器拒绝 |
| 非有限数值或无效坐标系 | 当前转换器拒绝 |
| loop 无曲线或未闭合 | 当前转换器拒绝 |
| 反向边但邻接关系可修复 | 当前转换器尝试翻转方向 |
| profiles 为空 | 当前转换器跳过并产生 warning |
| 全部特征都无法产生有效拉伸 | 当前转换器报错；错误文本可能写作未找到 ExtrudeFeature |
| 零拉伸距离或退化曲线 | 建议显式校验，不能依赖代码生成成功 |
| 非零拔模角 | 建议实现或拒绝，当前转换器未处理 |
| 首个操作为 Cut / Intersect | 建议检查有效目标实体，当前转换器直接初始化 body |

## 15. 本次抽样与验证范围

抽样方式：对 100 个编号目录，分别选取按文件名排序后的前 5 个 JSON，共 500 个。该抽样是确定性目录抽样，不是随机抽样，不能将统计比例直接推广到整个数据集。

| 观察项 | 数量 |
| --- | ---: |
| JSON 样本 | 500 |
| Sketch 实体 | 908 |
| ExtrudeFeature 实体 | 872 |
| NewBody 操作 | 605 |
| Join 操作 | 136 |
| Cut 操作 | 131 |
| Intersect 操作 | 0 |
| 单侧拉伸 | 794 |
| 对称拉伸 | 58 |
| 双侧拉伸 | 20 |
| Circle3D 曲线记录 | 1,876 |
| Line3D 曲线记录 | 9,182 |
| Arc3D 曲线记录 | 2,379 |
| 负距离字段 | 192 |
| 非零拔模角字段 | 0 |

曲线统计按各 profile 内的曲线记录累计，并非去重后的几何边数量。没有抽到某种操作，不代表全量数据不存在该操作。

将这 500 个文件传入本地转换器后，497 个成功解析并生成 CadQuery 源码，3 个被拒绝：

| 文件 | 转换器报告 |
| --- | --- |
| `0022/00220020.json` | 没有产生有效拉伸结果 |
| `0097/00970012.json` | 曲线链在两条相邻曲线之间未连接 |
| `0098/00980001.json` | 没有产生有效拉伸结果 |

**上述结果仅验证解析和代码生成。尚未通过 CAD 内核批量执行，也未验证实体有效性、STEP 往返或与参考模型的几何一致性。** 被拒绝的样本也不能仅凭该结果判定为数据错误，可能涉及转换器的处理范围。

## 16. 本地代码索引与后续验证

参考文件：

- [converter.py](DeepCAD_to_build123d_SimpleCADAPI_handoff_with_dataset/backend/deepcad_workbench/converter.py)：JSON 校验、几何处理和 CadQuery 源码生成。
- [PORTING_GUIDE.md](DeepCAD_to_build123d_SimpleCADAPI_handoff_with_dataset/docs/PORTING_GUIDE.md)：后端移植说明和已知限制。
- [test_converter.py](DeepCAD_to_build123d_SimpleCADAPI_handoff_with_dataset/tests/test_converter.py)：合成输入测试，检查现有转换行为。
- [README.md](DeepCAD_to_build123d_SimpleCADAPI_handoff_with_dataset/README.md)：交接包范围说明。

| 函数 | 作用 |
| --- | --- |
| `_number / _xy / _xyz` | 数值和坐标读取 |
| `_global_point` | 局部二维到全局三维变换 |
| `_read_transform` | 草图坐标系验证 |
| `_arc_midpoint` | 圆弧中间点计算 |
| `_ordered_loop_curves` | 边方向修复、闭合检查和起始边调整 |
| `_loop_bbox_min` | 当前环排序使用的包围盒启发式 |
| `_curve_source / _loop_source` | 生成 CadQuery 曲线与线框源码 |
| `_distance` | 读取有符号拉伸距离 |
| `convert_cad_json` | 主要遍历、特征解析与源码生成 |
| `load_cad_json` | JSON 文本解析入口 |

该交接包当前输出 CadQuery 代码，尚未提供完整的 build123d 或 SimpleCADAPI 转换后端。

后续接受重建结果前，应检查实体有效性、实体数量、包围盒、体积及 STEP 导出后重新读取的一致性；有参考模型时，再比较几何差异。代码能生成或能执行，不等于模型已经被正确恢复。
