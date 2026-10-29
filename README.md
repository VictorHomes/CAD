# CAD to build123d Python

两套实验转换器：DeepCAD JSON → 独立 build123d Python，以及 STEP → 显式 OCP 几何/拓扑构造 Python。

## 环境

已测试 Python 3.13、build123d 0.13.0、cadquery-ocp-novtk 8.0.1.1.0。安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-step-conversion-tested.txt
```

可选 VS Code 预览依赖：`pip install -r requirements-viewer.txt`。

## DeepCAD JSON 转换

```powershell
.\.venv\Scripts\python.exe tools/json_to_build123d.py input.json output/model.py --policy deepcad_compatible
```

官方兼容路径需要保留 third_party/DeepCAD/cadlib。批处理入口为 tools/batch_convert.py。JSON 必须符合 DeepCAD cad_json 模式，不接受任意 JSON。生成文件变量为 result。

## STEP 转换

```powershell
.\.venv\Scripts\python.exe tools/step_to_build123d.py input.step output/model.py --policy brep_exact
.\.venv\Scripts\python.exe output/model.py
```

生成器 0.1.2。brep_exact 保留受支持的导入后几何与拓扑，使用 OCP 构造，最后包装为 build123d 对象；生成文件无需读取原 STEP。不能恢复原始建模特征历史。默认策略 length_grid_0p5_mm 是近似策略，目前仅支持受限的平面直边多面体，建议首次使用显式指定 brep_exact。未知几何类型、非法重建及未覆盖模板明确失败。

转换命令只确认代码生成/编译，日志 code_generated_unverified 不代表完整保真通过。每次 CLI 尝试记录相邻 conversion_logs；生成文件运行后执行 BRepCheck。完整评估入口 tools/evaluate_step_conversion.py 提供新进程执行、拓扑计数、体积、近似 CD/IoU 和失败记录；技术重建成功不等于完整几何保真认证。

## 测试

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tools -p "test_step*.py" -v
```

当前 STEP 回归测试不需要 ABC 数据。资源测试针对 Windows venv 启动器行为。本发布包未重新进行 DeepCAD 全量回归，因为原 JSON 数据已不在本地。

## 可移植性与历史说明

不包含数据集、论文 PDF、虚拟环境或历史转换产物。仓库中的规则文档保留了开发历史、本机 D:\CAD 路径和实验结果，这些不是可用数据或新的性能保证。preview_step_compare.py 的样例简写依赖本机历史报告；跨机器请使用 --step/--py 手动路径。visualize_step_experiment.py 使用 Microsoft YaHei 字体，其他系统可能需要替换字体。

## 第三方来源和许可

third_party/DeepCAD 的代码来自 DeepCAD，保留原 MIT LICENSE 和版权声明。项目自有代码尚未指定开源许可证；仓库可见性和项目许可证由所有者决定。
