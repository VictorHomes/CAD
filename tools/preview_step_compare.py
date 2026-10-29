"""Load STEP/generated Python and show matched objects in VS Code OCP CAD Viewer."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT/'output'/'step_sample_100_final_20261028'


def resolve_paths(args):
    manual=any((args.step,args.py,args.exact_py))
    if manual and args.sample:
        raise ValueError('Use --sample OR manual --step/--py/--exact-py paths')
    if manual:
        return {'step':args.step,'py':args.py,'exact':args.exact_py}
    sample=(args.sample or '00000544').zfill(8)
    report=getattr(args,'report_dir',REPORT)
    records=json.loads((report/'results.json').read_text(encoding='utf-8'))
    selected={r['policy']:r for r in records if Path(r['source']).parent.name==sample}
    if not selected:
        raise ValueError(f'Sample {sample} is not in this 100-file report. Supply manual paths instead.')
    first=next(iter(selected.values()))
    result={'step':Path(first['source'])}
    for key,policy in [('py',args.policy),('exact','brep_exact')]:
        rec=selected.get(policy)
        result[key]=Path(rec['output']) if rec and rec.get('technical_success') else None
        result[key+'_failure']=None if rec is None else (rec.get('reason') or rec.get('error_code'))
    return result


def require(paths,key):
    path=paths.get(key)
    if path is None:
        raise ValueError(f'No usable {key} file. Conversion reason: {paths.get(key+"_failure")}. '
                         'Use sample 00000544, --policy brep_exact, or supply a successful file.')
    path=Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def load_python(path):
    # Only use Python code you trust: this executes the selected script.
    namespace=runpy.run_path(str(path),run_name='preview_generated_model')
    if 'result' not in namespace:
        raise ValueError(f'{path}: expected a build123d variable named result')
    return namespace['result']


def prepare(args):
    from build123d import import_step,Location
    paths=resolve_paths(args)
    objects=[];names=[];colors=[];loaded=[]
    if args.mode in {'step','overlay','side-by-side','triple'}:
        path=require(paths,'step')
        objects.append(import_step(path));names.append('Source_STEP');colors.append('#98a6b8');loaded.append(str(path))
    if args.mode=='triple':
        path=require(paths,'exact')
        objects.append(load_python(path));names.append('Exact_Python');colors.append('#3b82f6');loaded.append(str(path))
    if args.mode in {'py','overlay','side-by-side','triple'}:
        path=require(paths,'py')
        objects.append(load_python(path));names.append('Converted_Python');colors.append('#f59e0b');loaded.append(str(path))
    info=[]
    for name,obj,path in zip(names,objects,loaded):
        box=obj.bounding_box()
        info.append({'name':name,'file':path,'valid':bool(obj.is_valid),'solids':len(obj.solids()),
                     'volume_mm3':obj.volume,
                     'bbox_mm':[box.min.X,box.min.Y,box.min.Z,box.max.X,box.max.Y,box.max.Z]})
    if not all(r['valid'] for r in info):
        raise ValueError('A loaded object failed build123d validity checks')
    offsets=[0.0]*len(objects)
    if args.mode in {'side-by-side','triple'}:
        size=max(max(r['bbox_mm'][j+3]-r['bbox_mm'][j] for j in range(3)) for r in info)
        spacing=max(size*2.2,1.)
        offsets=[i*spacing for i in range(len(objects))]
        objects=[obj.moved(Location((offset,0,0))) for obj,offset in zip(objects,offsets)]
    for record,offset in zip(info,offsets):
        record['display_only_x_offset_mm']=offset
    alphas=[0.4,0.7] if args.mode=='overlay' else [1.0]*len(objects)
    return objects,names,colors,alphas,info


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample',help='Sample directory ID from the completed 100-file experiment')
    parser.add_argument('--report-dir',type=Path,default=REPORT,help='Conversion report directory containing results.json')
    parser.add_argument('--step',type=Path);parser.add_argument('--py',type=Path)
    parser.add_argument('--exact-py',type=Path)
    parser.add_argument('--policy',choices=['brep_exact','length_grid_0p5_mm'],default='length_grid_0p5_mm')
    parser.add_argument('--mode',choices=['step','py','overlay','side-by-side','triple'],default='triple')
    parser.add_argument('--port',type=int,default=3939)
    parser.add_argument('--camera',choices=['iso','keep','front','top','right'],default='iso')
    parser.add_argument('--check',action='store_true',help='Rebuild/check without connecting to the viewer')
    parser.add_argument('--screenshot',type=Path,help='Save viewer PNG after show; requires a running viewer')
    args=parser.parse_args()
    if not 1<=args.port<=65535:
        parser.error('--port must be an integer between 1 and 65535')
    objects,names,colors,alphas,info=prepare(args)
    print(json.dumps({'mode':args.mode,'port':args.port,'objects':info},ensure_ascii=False,indent=2))
    if args.check:
        print('CHECK OK: model loading/reconstruction verified; viewer not contacted.')
        return 0
    try:
        from ocp_vscode import show,set_port,Camera,save_screenshot
    except ImportError as exc:
        raise RuntimeError('Install matching viewer client with D:\\CAD\\.venv\\Scripts\\python.exe -m pip install ocp_vscode==4.2.0') from exc
    camera={'iso':Camera.ISO,'keep':Camera.KEEP,'front':Camera.FRONT,'top':Camera.TOP,'right':Camera.RIGHT}[args.camera]
    set_port(args.port)
    print(f'Sending to OCP CAD Viewer on 127.0.0.1:{args.port}. Open the viewer in VS Code first.',flush=True)
    show(*objects,names=names,colors=colors,alphas=alphas,reset_camera=camera,
         ortho=True,axes=True,axes0=True,show_locals=False,
         deviation=0.05,angular_tolerance=0.1)
    if args.screenshot:
        args.screenshot.parent.mkdir(parents=True,exist_ok=True)
        save_screenshot(str(args.screenshot.resolve()),port=args.port,timeout_s=10)
    return 0


if __name__=='__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'PREVIEW FAILED [{type(exc).__name__}]: {exc}',file=sys.stderr)
        print('See D:\\CAD\\VSCode中STEP与Python重建及渲染对比使用指南.md',file=sys.stderr)
        sys.exit(1)
