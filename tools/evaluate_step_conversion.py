"""Reproducible paired STEP->Python experiment, isolated worker per file/policy."""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time
import traceback

EVALUATOR_VERSION = '0.2.1'


def error_code(exc):
    prefix=str(exc).split(':',1)[0]
    if not prefix.replace('_','').isalnum() or prefix.upper()!=prefix:
        prefix=type(exc).__name__
    return getattr(exc,'code',prefix)


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def metrics(shape):
    import math
    from build123d import Shape
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.BRepBndLib import BRepBndLib
    from OCP.Bnd import Bnd_Box
    from OCP.TopAbs import TopAbs_VERTEX, TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID, TopAbs_SHELL
    from OCP.TopExp import TopExp
    from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher
    counts = {}
    for name, typ in [('vertices',TopAbs_VERTEX),('edges',TopAbs_EDGE),('faces',TopAbs_FACE),
                      ('shells',TopAbs_SHELL),('solids',TopAbs_SOLID)]:
        m = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
        TopExp.MapShapes_s(shape, typ, m)
        counts[name] = m.Extent()
    solids = Shape.cast(shape).solids()
    volumes = []
    for solid in solids:
        prop = GProp_GProps()
        BRepGProp.VolumeProperties_s(solid.wrapped, prop, True, False, False)
        volumes.append(prop.Mass())
    if not volumes or any(not math.isfinite(v) or v <= 0 for v in volumes):
        raise ValueError('INVALID_SOLID_VOLUME: Expected finite positive oriented volume')
    bounds = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, bounds, False, False)
    low, high = bounds.CornerMin(), bounds.CornerMax()
    bbox = [low.X(),low.Y(),low.Z(),high.X(),high.Y(),high.Z()]
    return {'counts': counts, 'volume_mm3': sum(volumes), 'solid_volumes_mm3': volumes,
            'bbox_mm': bbox, 'diagonal_mm': math.dist(bbox[:3],bbox[3:])}


def sample_surface(shape, count, seed, diagonal):
    import numpy as np
    from build123d import Shape
    try:
        vertices, triangles = Shape.cast(shape).tessellate(max(diagonal/2000., 1e-5), 0.1)
    except Exception as exc:
        raise ValueError('SURFACE_TESSELLATION_FAILED: '+str(exc)) from exc
    xyz = np.array([[p.X,p.Y,p.Z] for p in vertices])
    tri = xyz[np.array(triangles,dtype=np.int64)]
    area = np.linalg.norm(np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]),axis=1)/2
    keep = area > 0
    tri, area = tri[keep], area[keep]
    if not len(area) or not np.isfinite(area).all():
        raise ValueError('INVALID_SURFACE_SAMPLING: No finite positive triangle areas')
    rng = np.random.default_rng(seed)
    chosen = tri[rng.choice(len(tri),count,p=area/area.sum())]
    uv = rng.random((count,2))
    sqrt_u = np.sqrt(uv[:,0])
    return ((1-sqrt_u)[:,None]*chosen[:,0] + (sqrt_u*(1-uv[:,1]))[:,None]*chosen[:,1]
            + (sqrt_u*uv[:,1])[:,None]*chosen[:,2])


def compare(a,b,ma,mb,progress=lambda name,partial=None: None):
    import math
    import numpy as np
    from scipy.spatial import cKDTree
    from build123d import Shape
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    diagonal = ma['diagonal_mm']
    progress('baseline_surface_sampling')
    pa = sample_surface(a,8192,20261028,diagonal)
    progress('rebuilt_surface_sampling')
    pb = sample_surface(b,8192,20261028,diagonal)
    dab = cKDTree(pb).query(pa)[0]
    dba = cKDTree(pa).query(pb)[0]
    result = {'cd_l1_mm': float((dab.mean()+dba.mean())/2),
              'cd_l2_mm2': float(((dab*dab).mean()+(dba*dba).mean())/2),
              'cd_l2_normalized': float(((dab*dab).mean()+(dba*dba).mean())/(2*diagonal**2)),
              'sample_points_per_shape':8192, 'sampling_seed':20261028,
              'tessellation_linear_deflection_mm': max(diagonal/2000.,1e-5),
              'cd_method':'area-weighted triangle sampling + point-cloud KDTree; same seed; approximate',
              'volume_relative_error': abs(mb['volume_mm3']-ma['volume_mm3'])/ma['volume_mm3']}
    progress('cd_complete',result)
    aa,bb = Shape.cast(a).solids(), Shape.cast(b).solids()
    if len(aa) != len(bb):
        raise ValueError('SOLID_COUNT_MISMATCH')
    intersections = []
    for index,(sa,sb) in enumerate(zip(aa,bb)):
        progress(f'iou_boolean_solid_{index}')
        common = BRepAlgoAPI_Common(sa.wrapped,sb.wrapped)
        common.SetRunParallel(False)
        common.Build()
        if not common.IsDone() or not BRepCheck_Analyzer(common.Shape(),True).IsValid():
            raise ValueError('IOU_BOOLEAN_FAILED')
        prop = GProp_GProps()
        BRepGProp.VolumeProperties_s(common.Shape(), prop, True, False, False)
        v = prop.Mass()
        if not math.isfinite(v) or v < -1e-8:
            raise ValueError('IOU_INTERSECTION_VOLUME_INVALID')
        intersections.append(max(0.,v))
    inter = sum(intersections)
    union = ma['volume_mm3'] + mb['volume_mm3'] - inter
    if not math.isfinite(union) or union <= 0:
        raise ValueError('IOU_DENOMINATOR_INVALID')
    iou = inter/union
    if iou < -1e-8 or iou > 1+1e-8:
        raise ValueError('IOU_OUT_OF_RANGE')
    result.update(iou_raw=iou, iou=max(0.,min(1.,iou)),
                  iou_domain='sum of paired solid occurrences in traversal order; not union of all bodies',
                  iou_status='measured_uncalibrated', numerical_uncertainty_calibrated=False)
    return result


def worker(args):
    from step_to_build123d import read_step, Generator, VERSION
    from OCP.BRepCheck import BRepCheck_Analyzer
    import runpy
    start = time.monotonic()
    dest = args.output
    record = {'source':str(args.source),'output':str(dest),'policy':args.policy,
              'generator_version':VERSION,'status':'running','technical_success':False,
              'evaluator_version':EVALUATOR_VERSION,
              'fidelity_status':'unverified','stage':'initialize','worker_pid':os.getpid()}
    def stage(name):
        record['stage'] = name
        save(args.record,record)
    try:
        stage('read_source')
        record['source_sha256'] = hashlib.sha256(args.source.read_bytes()).hexdigest()
        baseline,metadata = read_step(args.source)
        record['import'] = metadata
        record['baseline'] = metrics(baseline)
        stage('extract_and_generate')
        generator = Generator(baseline,args.policy)
        try:
            code = generator.generate()
        finally:
            record['quantization_records'] = generator.changes
            record['geometry_types'] = generator.count_types
        stage('compile_python')
        compile(code,str(dest),'exec')
        dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_text(code,encoding='utf-8')
        record['code_generated'] = True
        stage('execute_independent_python')
        # All generated imports are installed packages; source/serialized CAD reads
        # during execution are forbidden. File is also inspected in the self-tests.
        blocked = [True]
        def audit(event, values):
            if blocked[0] and event == 'open':
                filename = values[0]
                if isinstance(filename,(str,bytes,os.PathLike)) and Path(os.fsdecode(filename)).suffix.lower() in {'.step','.stp','.brep','.brp','.json','.7z'}:
                    raise PermissionError('GENERATED_SOURCE_ACCESS_FORBIDDEN: '+str(filename))
        sys.addaudithook(audit)
        try:
            rebuilt = runpy.run_path(str(dest))['result'].wrapped
        finally:
            blocked[0] = False
        if not BRepCheck_Analyzer(rebuilt,True).IsValid():
            raise ValueError('REBUILT_BREP_INVALID')
        stage('check_topology_and_volume')
        record['rebuilt'] = metrics(rebuilt)
        if record['baseline']['counts'] != record['rebuilt']['counts']:
            raise ValueError('TOPOLOGY_COUNTS_CHANGED')
        if hashlib.sha256(args.source.read_bytes()).hexdigest() != record['source_sha256']:
            raise ValueError('SOURCE_CHANGED_DURING_CONVERSION')
        record['technical_success'] = True
        record['status'] = 'reconstructed_valid'
        stage('compare_cd_iou')
        def comparison_progress(name,partial=None):
            record['comparison_stage']=name
            if partial is not None:
                record['comparison']=partial
                record['comparison_status']='partial'
            save(args.record,record)
        try:
            record['comparison'] = compare(baseline,rebuilt,record['baseline'],record['rebuilt'],comparison_progress)
            record['comparison_status'] = 'measured'
            if args.policy == 'brep_exact':
                record['regression_gate_pass'] = (record['comparison']['volume_relative_error'] <= 1e-7
                    and record['comparison']['iou'] >= 0.99999
                    and record['comparison']['cd_l2_normalized'] <= 1e-8)
                if not record['regression_gate_pass']:
                    record['status']='comparison_mismatch'
        except Exception as exc:
            record['comparison_status']='failed'
            record['comparison_error']={'code':error_code(exc),'reason':str(exc),
                                        'traceback':traceback.format_exc()}
        record['fidelity_status']='unverified_full_contract'
        record['stage']='complete'
    except Exception as exc:
        record.update(status='failed',error_code=error_code(exc),
                      reason=str(exc),context=getattr(exc,'context',{}),traceback=traceback.format_exc())
    record['elapsed_s'] = time.monotonic()-start
    import psutil
    mi = psutil.Process().memory_info()
    record['worker_peak_rss_mb'] = getattr(mi,'peak_wset',mi.rss)/1024**2
    save(args.record,record)
    return 0 if record['technical_success'] else 1


def run_one(job,timeout,memory_mb):
    import psutil
    source,policy,dest,record_path = job
    log = record_path.with_suffix('.log')
    progress_path = record_path.with_suffix('.worker.json')
    cmd = [sys.executable,str(Path(__file__).resolve()),'--worker','--source',str(source),
           '--policy',policy,'--output',str(dest),'--record',str(progress_path)]
    start = time.monotonic()
    peak = 0
    termination = None
    record_path.parent.mkdir(parents=True,exist_ok=True)
    with log.open('wb') as output:
        process = subprocess.Popen(cmd,stdout=output,stderr=subprocess.STDOUT)
        known_children = {}
        while process.poll() is None:
            try:
                launcher = psutil.Process(process.pid)
                descendants = launcher.children(recursive=True)
                for child in descendants:
                    known_children[child.pid] = child
                resident = sum(p.memory_info().rss for p in [launcher]+descendants if p.is_running())
                peak=max(peak,resident)
                if resident > memory_mb*1024**2:
                    termination='MEMORY_LIMIT'
            except psutil.Error:
                pass
            if time.monotonic()-start > timeout:
                termination='TIMEOUT'
            if termination:
                # Windows venv python.exe is a small launcher: kill descendants
                # before the launcher, and retain process identities against PID reuse.
                for child in reversed(list(known_children.values())):
                    try:
                        child.kill()
                    except psutil.Error:
                        pass
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
                psutil.wait_procs(list(known_children.values()),timeout=5)
                break
            time.sleep(0.2)
    try:
        record = json.loads(progress_path.read_text(encoding='utf-8'))
    except (OSError,ValueError):
        record = {'source':str(source),'policy':policy,'stage':'worker_start'}
    if termination or record.get('status') == 'running' or 'status' not in record:
        reason=(f'Worker exceeded {timeout}s' if termination == 'TIMEOUT' else
                f'Worker exceeded {memory_mb} MiB' if termination == 'MEMORY_LIMIT' else
                f'Worker exited unexpectedly, code={process.returncode}')
        if record.get('technical_success') and record.get('stage') == 'compare_cd_iou':
            record.update(status='reconstructed_valid_comparison_failed',comparison_status='failed',
                          comparison_error={'code':termination or 'WORKER_CRASH','reason':reason})
        else:
            record.update(status='failed',error_code=termination or 'WORKER_CRASH',reason=reason,
                          technical_success=False)
    record.update(worker_exit_code=process.returncode,peak_rss_mb=peak/1024**2,
                  wall_s=time.monotonic()-start,log=str(log))
    save(record_path,record)
    return record


def experiment(args):
    root = args.input.resolve()
    out = args.report_dir.resolve()
    if out.exists():
        raise ValueError('Report directory already exists; use a new directory to preserve each run')
    if root == out or root in out.parents:
        raise ValueError('Report directory must be outside the source dataset')
    population = sorted(p for p in root.rglob('*') if p.suffix.lower() in {'.step','.stp'})
    if args.count <= 0 or args.count > len(population):
        raise ValueError('Invalid sample count')
    rng = random.Random(args.seed)
    selected = population[:args.count] if args.selection == 'first' else sorted(rng.sample(population,args.count))
    out.mkdir(parents=True)
    manifest={'created_at':datetime.now().astimezone().isoformat(),'input':str(root),
              'evaluator_version':EVALUATOR_VERSION,'memory_scope':'sum RSS of launcher and recursive descendants',
              'population_count':len(population),'count':args.count,'seed':args.seed,
              'method':('first N paths in sorted order' if args.selection == 'first' else 'uniform random without replacement over sorted paths'),
              'timeout_seconds_per_policy':args.timeout,'memory_limit_mb_per_worker':args.memory_mb,
              'workers':args.workers,'policies':['brep_exact','length_grid_0p5_mm'],
              'files':[{'relative_path':str(p.relative_to(root)),'bytes':p.stat().st_size,
                        'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in selected]}
    save(out/'manifest.json',manifest)
    import importlib.metadata
    import platform
    names=['build123d','cadquery-ocp-novtk','cadquery-ocp-proxy','numpy','scipy','matplotlib','psutil']
    scripts=[Path(__file__).resolve(),Path(__file__).with_name('step_to_build123d.py'),Path(__file__).with_name('step_length_quantization.py')]
    save(out/'environment.json',{'python':sys.version,'platform':platform.platform(),
                               'packages':{p:importlib.metadata.version(p) for p in names},
                               'scripts':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in scripts}})
    import shutil
    snapshot=out/'source_snapshot';snapshot.mkdir()
    for path in scripts:
        shutil.copy2(path,snapshot/path.name)
    jobs=[]
    for source in selected:
        rel = source.relative_to(root)
        for policy in manifest['policies']:
            dest = out/'models'/policy/rel.with_suffix('.py')
            record = out/'records'/policy/rel.with_suffix('.json')
            jobs.append((source,policy,dest,record))
    records=[]
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures=[executor.submit(run_one,j,args.timeout,args.memory_mb) for j in jobs]
        for future in as_completed(futures):
            r=future.result(); records.append(r)
            with (out/'results.jsonl').open('a',encoding='utf-8') as f:
                f.write(json.dumps(r,ensure_ascii=False,allow_nan=False)+'\n')
            print(f"[{len(records)}/{len(jobs)}] {r['policy']} {Path(r['source']).parent.name}: {r['status']} {r.get('error_code','')} {r.get('reason','')}",flush=True)
    save(out/'results.json',records)
    make_report(out,manifest,records)


def make_report(out,manifest,records):
    import csv
    import statistics
    os.environ.setdefault('MPLCONFIGDIR',str(out/'matplotlib_cache'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans']
    plt.rcParams['axes.unicode_minus']=False
    n=manifest['count']
    summary={}
    for policy in manifest['policies']:
        rr=[r for r in records if r['policy']==policy]
        measured=[r['comparison'] for r in rr if r.get('comparison_status')=='measured']
        summary[policy]={
            'sample_count':n,'code_generated':sum(bool(r.get('code_generated')) for r in rr),
            'technical_success':sum(bool(r.get('technical_success')) for r in rr),
            'technical_success_rate':sum(bool(r.get('technical_success')) for r in rr)/n,
            'comparison_measured':len(measured),
            'regression_gate_pass':sum(bool(r.get('regression_gate_pass')) for r in rr),
            'failure_reasons':dict(Counter(r.get('error_code','UNKNOWN') for r in rr if not r.get('technical_success'))),
            'comparison_failures':sum(r.get('comparison_status')=='failed' for r in rr),
            'median_iou':statistics.median(c['iou'] for c in measured) if measured else None,
            'median_cd_l1_mm':statistics.median(c['cd_l1_mm'] for c in measured) if measured else None,
            'median_cd_l2_normalized':statistics.median(c['cd_l2_normalized'] for c in measured) if measured else None,
            'wall_seconds_sum':sum(r.get('wall_s',0) for r in rr),
            'peak_worker_rss_mb':max(r.get('peak_rss_mb',0) for r in rr)}
    save(out/'summary.json',summary)
    columns=['source','policy','status','stage','technical_success','error_code','reason','context',
             'comparison_status','iou','cd_l1_mm','cd_l2_normalized','wall_s','peak_rss_mb','log']
    with (out/'results.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=columns); writer.writeheader()
        for r in sorted(records,key=lambda x:(x['source'],x['policy'])):
            row={k:r.get(k,'') for k in columns}
            row.update({k:r.get('comparison',{}).get(k,'') for k in ['iou','cd_l1_mm','cd_l2_normalized']})
            row['context']=json.dumps(r.get('context',{}),ensure_ascii=False)
            if r.get('comparison_error'):
                row['reason']=row['reason'] or r['comparison_error']['reason']
            writer.writerow(row)
    policies=manifest['policies']
    labels=['不舍入重建','0.5 mm 舍入']
    fig,axes=plt.subplots(1,2,figsize=(13,5),layout='constrained')
    counts=[summary[p]['technical_success'] for p in policies]
    bars=axes[0].bar(labels,counts,color=['#3b82f6','#f59e0b'])
    axes[0].set_ylim(0,n*1.15); axes[0].set_ylabel(f'技术重建成功数 / {n}'); axes[0].set_title('同一批样本的有效重建成功率')
    for bar,count in zip(bars,counts):
        axes[0].text(bar.get_x()+bar.get_width()/2,count+1,f'{count}/{n} ({count/n:.1%})',ha='center')
    failure=Counter(r.get('error_code','UNKNOWN') for r in records if not r.get('technical_success'))
    if failure:
        items=failure.most_common()
        axes[1].barh([k for k,v in items],[v for k,v in items],color='#ef4444')
        axes[1].invert_yaxis(); axes[1].set_title('两种策略的失败原因（按任务统计）')
        axes[1].set_xlabel('任务数')
    else:
        axes[1].text(.5,.5,'无技术重建失败',ha='center')
    fig.savefig(out/'success_and_failures.png',dpi=160); plt.close(fig)
    lines=[f'# STEP → 独立 build123d Python：{n} 文件配对转换实验','',
           f"样本数 {n}，总体 {manifest['population_count']}，随机种子 {manifest['seed']}。每个文件分别运行两个策略，失败不替换样本。",'',
           '| 策略 | 生成代码 | 有效重建 | 成功率 | CD/IoU 完成 | 精确回归门槛通过 |',
           '|---|---:|---:|---:|---:|---:|']
    for p in policies:
        s=summary[p]
        lines.append(f"| {p} | {s['code_generated']} | {s['technical_success']} | {s['technical_success_rate']:.1%} | {s['comparison_measured']} | {s['regression_gate_pass'] if p=='brep_exact' else '不适用'} |")
    lines += ['', '技术成功指：显式 Python 在禁止读取外部 CAD 数据的审计范围内执行、BRepCheck 几何检查有效、实体体积为正、顶点/边/面/壳/实体计数与基准相同、源文件未改。不是全部规范验收或局部特征保真的证明。',
              '', '不舍入策略的实验回归门槛：体积相对误差 ≤ 1e-7、配对实体 IoU ≥ 0.99999、归一化 CD² ≤ 1e-8。尚未标定积分/布尔误差，不构成严格保真认证。',
              '', '0.5 mm 策略首版仅支持无非单位放置的平面直边多面体：顶点坐标为独立量化参数，直线、平面和 UV 为派生量。曲面/样条和其他放置明确失败，不回退为不舍入成功。量化阈值未标定，重建成功仍为 fidelity unverified。',
              '', 'CD 使用每模型 8192 个面积加权三角采样点、相同随机种子、双向最近点平均距离及平方距离；是离散近似。相同种子可能在相同三角网格上产生相同点，不代表解析误差绝对为零。',
              '', 'IoU 采用遍历顺序对应的实体逐对求交，交体积累加后计算比率，保留重复实例权重；不是多个相交实体整体并集的 IoU。未校准数值不确定度。',
              '', '当前覆盖限制：默认导入配置未逐项验证警告/有效参数；不保留产品装配语义；未完成自交、有限域、全部参数/拓扑身份与局部小特征保护检查；不支持的偏置等类型明确失败。生成文件和成功率属于实验原型。',
              '', '## 指标及失败分布', '']
    for p in policies:
        s=summary[p]
        lines += [f'### {p}', '', f"IoU 中位数：{s['median_iou']}；CD-L1 中位数（mm）：{s['median_cd_l1_mm']}；归一化 CD² 中位数：{s['median_cd_l2_normalized']}。",'',
                  f"失败原因：{json.dumps(s['failure_reasons'],ensure_ascii=False)}；比较阶段失败 {s['comparison_failures']}。",'']
    lines += ['## 逐文件失败清单','', '| 文件 | 策略 | 阶段 | 错误码 | 原因与对象 |','|---|---|---|---|---|']
    for r in sorted(records,key=lambda x:(x['source'],x['policy'])):
        if not r.get('technical_success') or r.get('comparison_status')=='failed' or r.get('status')=='comparison_mismatch':
            error=r.get('comparison_error',{})
            reason=(r.get('reason') or error.get('reason') or '实验回归门槛未通过').replace('|','/').replace('\n',' ')
            lines.append(f"| {Path(r['source']).name} | {r['policy']} | {r['stage']} | {r.get('error_code') or error.get('code','COMPARISON_MISMATCH')} | {reason} {json.dumps(r.get('context',{}),ensure_ascii=False)} |")
    lines += ['', '完整记录见 results.csv、results.json 和 records/；每个任务保存阶段、具体原因、对象上下文、异常堆栈、内核日志、耗时与轮询峰值 RSS。TIMEOUT/MEMORY_LIMIT 是资源上限，不等同于模型不可转换。', '', '![成功率与失败原因](success_and_failures.png)','']
    (out/'report.md').write_text('\n'.join(lines),encoding='utf-8')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--worker',action='store_true')
    p.add_argument('--source',type=Path);p.add_argument('--output',type=Path);p.add_argument('--record',type=Path)
    p.add_argument('--policy',choices=['brep_exact','length_grid_0p5_mm'])
    p.add_argument('--input',type=Path,default=Path('ABCdata/step/0000'))
    p.add_argument('--report-dir',type=Path,default=Path('output/step_sample_100'))
    p.add_argument('--count',type=int,default=100); p.add_argument('--seed',type=int,default=20261028)
    p.add_argument('--selection',choices=['random','first'],default='random')
    p.add_argument('--timeout',type=float,default=90);p.add_argument('--memory-mb',type=int,default=2048)
    p.add_argument('--workers',type=int,default=2)
    args=p.parse_args()
    if args.worker:
        return worker(args)
    experiment(args)
    return 0


if __name__=='__main__':
    sys.exit(main())
