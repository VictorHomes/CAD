"""Additional metric charts and matched source/exact/grid sample renders."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import os
from collections import Counter


def setup():
    os.environ.setdefault('MPLCONFIGDIR',str(Path(__file__).resolve().parents[1]/'output'/'matplotlib_cache'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans']
    plt.rcParams['axes.unicode_minus']=False
    return plt


def chart(root,records):
    import numpy as np
    plt=setup()
    labels={'QUANTIZATION_RECONSTRUCTION_UNSUPPORTED':'量化模板未覆盖',
            'NO_SOLID':'输入无实体','TIMEOUT':'比较超时',
            'MEMORY_LIMIT':'比较内存超限','SURFACE_TESSELLATION_FAILED':'基准网格化失败',
            'IOU_BOOLEAN_FAILED':'IoU 布尔检查失败','COMPARISON_MISMATCH':'回归门槛未通过'}
    fig,axes=plt.subplots(1,3,figsize=(15,4.8),layout='constrained',gridspec_kw={'width_ratios':[1.1,1,1.1]})
    n=json.loads((root/'manifest.json').read_text(encoding='utf-8'))['count']
    counts=[sum(bool(r.get('technical_success')) for r in records if r['policy']==p) for p in ['brep_exact','length_grid_0p5_mm']]
    bars=axes[0].bar(['不舍入','0.5 mm 舍入'],counts,color=['#3b82f6','#f59e0b'],width=.6)
    axes[0].set_ylim(0,n*1.14);axes[0].set_ylabel(f'有效重建数量 / {n}')
    axes[0].set_title('技术重建成功率')
    for bar,value in zip(bars,counts):
        axes[0].text(bar.get_x()+bar.get_width()/2,value+1,f'{value}/{n} ({value/n:.0%})',ha='center')
    rebuild_failures=Counter(r.get('error_code','UNKNOWN') for r in records if not r.get('technical_success'))
    comparison_failures=Counter(r.get('comparison_error',{}).get('code','UNKNOWN') for r in records if r.get('comparison_status')=='failed')
    comparison_failures['COMPARISON_MISMATCH']=sum(r.get('status')=='comparison_mismatch' for r in records)
    for a,counter,title in zip(axes[1:],[rebuild_failures,comparison_failures],['重建失败原因（两策略任务合计）','比较未完成或未达门槛（任务数）']):
        items=[(key,count) for key,count in counter.most_common() if count]
        if items:
            positions=list(range(len(items)))
            bars=a.barh(positions,[v for k,v in items],color='#ef4444',height=.6)
            a.set_yticks(positions,[labels.get(k,k) for k,v in items]);a.invert_yaxis()
            a.set_xlim(0,max(v for k,v in items)*1.2)
            for bar,(_,value) in zip(bars,items):
                a.text(value+.1,bar.get_y()+bar.get_height()/2,str(value),va='center')
        a.set_title(title);a.set_xlabel('任务数');a.grid(axis='x',alpha=.15)
    fig.suptitle('同一批 100 文件分别测试两种策略；技术重建成功不等于完整保真验证')
    fig.savefig(root/'success_and_failures.png',dpi=170);plt.close(fig)
    fig,ax=plt.subplots(1,2,figsize=(12,4.5),layout='constrained')
    for policy,label,color in [('brep_exact','不舍入','#3b82f6'),('length_grid_0p5_mm','0.5 mm 舍入','#f59e0b')]:
        rr=sorted([r for r in records if r['policy']==policy and r.get('comparison_status')=='measured'],key=lambda r:r['source'])
        x=np.arange(1,len(rr)+1)
        ax[0].scatter(x,[r['comparison']['iou'] for r in rr],label=f'{label}，n={len(rr)}',s=22,color=color,alpha=.8)
        ax[1].scatter(x,[r['comparison']['cd_l1_mm'] for r in rr],label=f'{label}，n={len(rr)}',s=22,color=color,alpha=.8)
    ax[0].set_title('已完成比较的模型：配对实体 IoU')
    ax[0].set_ylim(-.02,1.04); ax[0].set_ylabel('IoU，越接近 1 越好')
    ax[1].set_title('已完成比较的模型：近似 CD-L1')
    ax[1].set_yscale('symlog',linthresh=1e-5); ax[1].set_ylabel('CD-L1（mm），越小越好')
    for a in ax:
        a.set_xlabel('各策略内已测模型序号（按路径排序）');a.grid(alpha=.2);a.legend()
    fig.suptitle('失败样例保留在成功率分母；此图仅展示已完成的指标')
    fig.savefig(root/'iou_cd_comparison.png',dpi=170);plt.close(fig)


def render(root,source):
    import numpy as np
    import runpy
    from build123d import Shape
    from step_to_build123d import read_step
    from evaluate_step_conversion import metrics
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    plt=setup()
    all_records=json.loads((root/'results.json').read_text(encoding='utf-8'))
    records={r['policy']:r for r in all_records if r['source']==source}
    baseline=read_step(Path(source))[0]
    bbox=metrics(baseline)['bbox_mm']; diagonal=metrics(baseline)['diagonal_mm']
    shapes=[Shape.cast(baseline),runpy.run_path(records['brep_exact']['output'])['result'],
            runpy.run_path(records['length_grid_0p5_mm']['output'])['result']]
    fig=plt.figure(figsize=(13,4.5),layout='constrained')
    for i,(shape,title,color) in enumerate(zip(shapes,['原 STEP 导入','不舍入独立 Python','0.5 mm 舍入独立 Python'],['#9aa9bc','#3b82f6','#f59e0b']),1):
        a=fig.add_subplot(1,3,i,projection='3d')
        v,t=shape.tessellate(max(diagonal/2000,1e-5),.1)
        xyz=np.array([[p.X,p.Y,p.Z] for p in v]); tri=xyz[np.array(t,dtype=int)]
        collection=Poly3DCollection(tri,facecolors=color,edgecolors='#334155',linewidths=.15,alpha=.95)
        a.add_collection3d(collection)
        center=(np.array(bbox[:3])+np.array(bbox[3:]))/2
        radius=max(np.array(bbox[3:])-np.array(bbox[:3]))*.55
        a.set_xlim(center[0]-radius,center[0]+radius)
        a.set_ylim(center[1]-radius,center[1]+radius)
        a.set_zlim(center[2]-radius,center[2]+radius)
        a.set_box_aspect((1,1,1));a.view_init(25,-55);a.set_axis_off();a.set_title(title)
        if i>1:
            policy='brep_exact' if i==2 else 'length_grid_0p5_mm'
            c=records[policy]['comparison']
            a.text2D(.05,.03,f"IoU={c['iou']:.8f}\nCD-L1={c['cd_l1_mm']:.6g} mm",transform=a.transAxes)
    sample_id=Path(source).parent.name
    fig.suptitle(f'样例 {sample_id}：相同世界位置、视角与显示范围')
    destination=root/f'sample_{sample_id}_comparison.png'
    fig.savefig(destination,dpi=170);plt.close(fig)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('report_dir',type=Path);p.add_argument('--source')
    args=p.parse_args();root=args.report_dir.resolve()
    if args.source:
        render(root,args.source);return
    rr=json.loads((root/'results.json').read_text(encoding='utf-8'))
    chart(root,rr)
    good=sorted([r for r in rr if r['policy']=='length_grid_0p5_mm' and r.get('comparison_status')=='measured'],key=lambda r:r['comparison']['iou'])
    paired={r['source'] for r in rr if r['policy']=='brep_exact' and r.get('comparison_status')=='measured'}
    good=[r for r in good if r['source'] in paired]
    if good:
        choices=list(dict.fromkeys(r['source'] for r in [good[0],good[len(good)//2],good[-1]]))
        results=[]
        for source in choices:
            try:
                result=subprocess.run([sys.executable,str(Path(__file__).resolve()),str(root),'--source',source],
                                      capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=90)
                results.append({'source':source,'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
            except subprocess.TimeoutExpired:
                results.append({'source':source,'error':'RENDER_TIMEOUT'})
        (root/'render_results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
