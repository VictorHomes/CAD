"""Chinese explanation of observed failures; does not relabel unknown root causes."""
import argparse
from collections import Counter
import json
from pathlib import Path


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('report_dir',type=Path)
    root=p.parse_args().report_dir.resolve()
    records=json.loads((root/'results.json').read_text(encoding='utf-8'))
    summary=json.loads((root/'summary.json').read_text(encoding='utf-8'))
    lines=['# 失败原因与指标说明','',
           '本说明区分“输入不满足实体范围”“转换模板不支持”“指标计算失败”和“指标门槛未通过”。没有把未知原因归为数据损坏，也没有删去失败文件后重算分母。','',
           '## 转换与重建','',
           '| 策略 | 100 个样本中有效重建 | 失败原因 |','|---|---:|---|']
    for policy,s in summary.items():
        lines.append(f"| {policy} | {s['technical_success']} | {json.dumps(s['failure_reasons'],ensure_ascii=False)} |")
    lines += ['', '### NO_SOLID：导入结果没有实体','',
              '读取和传输完成后，导入图中没有 TopAbs_SOLID。它可能是曲面或壳，并不自动表示 STEP 格式损坏。当前体积 IoU 和实体成功率的范围要求存在实体，故两种策略都明确拒绝；不自动把开放壳封口。涉及文件：','']
    paths=sorted({r['source'] for r in records if r.get('error_code')=='NO_SOLID'})
    lines += ['- '+str(Path(path)) for path in paths]
    lines += ['', '### QUANTIZATION_RECONSTRUCTION_UNSUPPORTED：本版量化重建模板未覆盖','',
              '0.5 mm 策略只实现了平面直边多面体。碰到圆弧、样条、圆柱面等，需要同步改变顶点、3D 曲线、支撑曲面和 UV 参数曲线，本版没有相应模板，因此失败。这属于实现覆盖限制；不是 STEP 原模型必然无效，也不能偷偷输出不舍入模型冒充量化成功。','',
              '下面按遇到的第一个不支持表示分类，每个失败任务只列一个首要原因；不是统计文件内所有曲面。矩形裁剪面可能以平面为基面，本版未递归开放该量化模板，所以不能把此类型一概叫作非平面。','',
              '| 首个不支持几何类型 | 文件数 |','|---|---:|']
    types=Counter(r.get('context',{}).get('geometry_type','放置或端点模板限制') for r in records
                  if r.get('error_code')=='QUANTIZATION_RECONSTRUCTION_UNSUPPORTED')
    lines += [f'| {typ} | {n} |' for typ,n in types.most_common()]
    lines += ['', '## 指标计算未完成','',
              '以下模型可已完成有效重建，比较失败不会抹去重建结果。已经算出的 CD 保留于 comparison 字段；IoU 不存在时保持空值，不能填 0 或 1。','',
              '| 文件编号 | 子阶段 | 错误码 | 具体含义/原始消息 |','|---|---|---|---|']
    explanations={
        'SURFACE_TESSELLATION_FAILED':'表面网格化后缺少可用三角网格，采样器无法取点。请结合子阶段判断发生在原导入模型还是重建模型；不能直接归因于转换。',
        'IOU_BOOLEAN_FAILED':'实体交集没有构建完成，或交集结果未通过 BRepCheck；属于比较阶段检查失败。',
        'TIMEOUT':'整个任务达到 90 秒墙钟上限，停在记录的比较子阶段；不等于证明永远无法计算。',
        'MEMORY_LIMIT':'启动器及真实 Python 子进程的 RSS 合计超过 2048 MiB，完整任务进程树被终止。轮询可能造成少量超限。',
    }
    for r in sorted(records,key=lambda r:r['source']):
        if r.get('comparison_status')!='failed':continue
        e=r.get('comparison_error',{});code=e.get('code','UNKNOWN')
        reason=(explanations.get(code,'原因仍需定位。')+' '+e.get('reason','')).replace('|','/').replace('\n',' ')
        lines.append(f"| {Path(r['source']).parent.name} | {r.get('comparison_stage',r.get('stage',''))} | {code} | {reason} |")
    lines += ['', '## 已完成比较但未达到不舍入回归门槛','',
              '本次门槛为 IoU ≥ 0.99999、体积相对误差 ≤ 1e-7、归一化 CD² ≤ 1e-8。门槛失败是观测结果，不自动等于已定位转换代码错误。','']
    for r in records:
        if r.get('status')=='comparison_mismatch':
            c=r['comparison']
            lines += [f"- {Path(r['source']).name}：IoU={c['iou']:.12g}；体积相对误差={c['volume_relative_error']:.12g}；归一化 CD²={c['cd_l2_normalized']:.12g}。"]
    lines += ['', '样例 00002325 的补充诊断：原模型与自身、重建模型与自身的 IoU 均为 1，原模型与重建模型的交叉 IoU 约 0.999892；已探测的顶点、3D 曲线参数点、曲面参数点偏差均为 0。有限采样不能证明几何恒等；这些证据提示需进一步排查布尔数值敏感性或未覆盖的表示差异，目前根因仍未确认，不将它改记为通过。详细诊断若存在，见 diagnostics/。','',
              '## 成功率与精度的解释','',
              '98% 或其他技术重建比例仅是本次 100 文件和当前模板/资源配置下的结果，不是全部 ABC 数据的严格保真成功率。不舍入与量化不能混为一个成功率，且量化策略尚未建立局部特征保护和误差预算认证。','',
              'CD-L1 是双向点云最近点平均距离，CD-L2 是平均平方距离。本次点云由三角网格采样，原模型和重建模型使用相同随机种子。相同三角网格可能得到 CD=0；不同三角网格的采样分布又会带来额外点距。因此数值不能直接解释为全模型解析误差上界，CD-L1 也可能大于单个坐标的 0.25 mm 舍入偏移。','',
              'IoU 是对应实体按体积加权的交并比，不是重叠实体整体并集的 IoU；积分和布尔误差未校准。','']
    (root/'失败原因与指标说明.md').write_text('\n'.join(lines),encoding='utf-8')


if __name__=='__main__':
    main()
