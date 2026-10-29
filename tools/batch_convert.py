"""Convert a whole DeepCAD JSON tree to a matching Python tree, with resumable logs.

Only parses and emits source; does not execute models or export STEP/images.
"""
from __future__ import annotations

import argparse
import ast
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]


def timestamp():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def atomic_json(path, value):
    temp = path.with_name(path.name+'.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


class RunLock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.file = self.path.open('a+b')
        self.file.seek(0)
        if not self.file.read(1):
            self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError('Another conversion is using this log/output directory') from exc
        return self

    def __exit__(self, *args):
        self.file.close()


def fingerprint(policy, scale, tolerance):
    digest = hashlib.sha256()
    for path in [Path(__file__), ROOT/'tools/json_to_build123d.py', ROOT/'tools/deepcad_codegen.py',
                 ROOT/'tools/deepcad_official.py', *sorted((ROOT/'third_party/DeepCAD/cadlib').glob('*.py')),
                 ROOT/'third_party/DeepCAD/LICENSE']:
        digest.update(path.name.encode()); digest.update(path.read_bytes())
    digest.update(json.dumps([policy,scale,tolerance,sys.version]).encode())
    return digest.hexdigest()


def worker_init(policy):
    os.environ.setdefault('MPLCONFIGDIR', str(ROOT/'tmp/matplotlib'))
    from json_to_build123d import convert_document
    if policy == 'deepcad_compatible':
        import deepcad_official


def classify(error):
    text = str(error)
    if isinstance(error, json.JSONDecodeError):
        return 'invalid_json'
    if 'no extrusion' in text.lower() or 'No usable extrusion' in text:
        return 'no_extrusions'
    if 'Degenerate extent' in text:
        return 'degenerate_extent'
    if 'taper' in text.lower():
        return 'unsupported_taper'
    if isinstance(error, OSError):
        return 'file_io_error'
    return type(error).__name__


def schema_signature(data):
    if not isinstance(data,dict):
        return {'root_type':type(data).__name__}
    entities=data.get('entities',{})
    values=list(entities.values()) if isinstance(entities,dict) else []
    values=[e for e in values if isinstance(e,dict)]
    sketches=[e for e in values if e.get('type')=='Sketch']
    extrudes=[e for e in values if e.get('type')=='ExtrudeFeature']
    curves=set()
    for sketch in sketches:
        profiles=sketch.get('profiles',{})
        profiles=profiles.values() if isinstance(profiles,dict) else profiles if isinstance(profiles,list) else []
        for profile in profiles:
            if not isinstance(profile,dict):continue
            for loop in profile.get('loops',[]):
                if not isinstance(loop,dict):continue
                curves.update(str(c.get('type')) for c in loop.get('profile_curves',[]) if isinstance(c,dict))
    return {'root_type':'dict','root_keys':sorted(data),
            'entities_storage':type(entities).__name__,
            'sequence_storage':type(data.get('sequence')).__name__,
            'entity_types':sorted({str(e.get('type')) for e in values}),
            'sketch_profiles_storage':sorted({type(e.get('profiles')).__name__ for e in sketches}),
            'curve_types':sorted(curves),'extent_types':sorted({str(e.get('extent_type')) for e in extrudes}),
            'operations':sorted({str(e.get('operation')) for e in extrudes})}


def diagnose(data, error, stage):
    evidence={'stage':stage,'exception_type':type(error).__name__}
    category=classify(error)
    if isinstance(data,dict) and isinstance(data.get('entities'),dict):
        extrudes=[e for e in data['entities'].values() if isinstance(e,dict) and e.get('type')=='ExtrudeFeature']
        evidence['extrude_entities']=len(extrudes)
        evidence['empty_profile_extrudes']=sum(not e.get('profiles') for e in extrudes)
        evidence['zero_extent_entities']=[key for key,e in data['entities'].items()
            if isinstance(e,dict) and e.get('type')=='ExtrudeFeature'
            and e.get('extent_one',{}).get('distance',{}).get('value')==0]
        if category=='no_extrusions':
            category='sketch_only' if not extrudes else 'empty_profile_extrudes'
    suggestions={'sketch_only':'No extrusion entities: cannot construct a 3D solid from sketches alone.',
                 'empty_profile_extrudes':'Extrusion entities select no usable profiles; inspect source profile references.',
                 'degenerate_extent':'Zero/tiny extrusion extent is unsupported; inspect source before any repair.',
                 'unsupported_taper':'Nonzero taper requires a supported tapered extrusion implementation.',
                 'invalid_json':'Fix JSON syntax/encoding; do not modify source automatically.',
                 'file_io_error':'Check file permissions, available disk space and locked output files.'}
    evidence.update(category=category,suggestion=suggestions.get(category,'Inspect exception traceback and source entity/profile data; no automatic repair was applied.'))
    return evidence


def convert_chunk(jobs, input_root, output_root, policy, scale, tolerance, signature):
    from json_to_build123d import convert_document
    results = []
    for job in jobs:
        begin = time.perf_counter()
        relative = job['relative']
        source_path = Path(input_root)/relative
        target = Path(output_root)/Path(relative).with_suffix('.py')
        temporary = target.with_name(target.name+f'.tmp-{os.getpid()}')
        result = dict(job, signature=signature, output_bytes=0, output_mtime_ns=0,
                      error_category=None, error=None, updated_at=timestamp(),schema=None,
                      output_relative=Path(relative).with_suffix('.py').as_posix(),geometry_validated=False)
        stage='read_json';data=None
        try:
            raw=source_path.read_bytes()
            result['source_sha256']=hashlib.sha256(raw).hexdigest()
            stage='parse_json'
            data = json.loads(raw.decode('utf-8-sig'))
            stage='inspect_schema'
            result['schema']=schema_signature(data)
            stage='parse_cad_and_generate_python'
            code = convert_document(data, source_id=relative, scale=scale, policy=policy, tolerance=tolerance)
            notes_line=next((line for line in code.splitlines() if line.startswith('CONVERSION_NOTES = ')),None)
            result['conversion_notes']=ast.literal_eval(notes_line.split('=',1)[1].strip()) if notes_line else []
            stage='check_source_unchanged'
            now = source_path.stat()
            if now.st_size != job['input_bytes'] or now.st_mtime_ns != job['input_mtime_ns']:
                raise RuntimeError('Input changed during conversion; rerun this file')
            stage='write_python'
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(code, encoding='utf-8')
            temporary.replace(target)
            stat = target.stat()
            result.update(status='success',output_bytes=stat.st_size,output_mtime_ns=stat.st_mtime_ns,
                          stage='completed',syntax_validated=True)
        except Exception as exc:
            try:details=diagnose(data,exc,stage)
            except Exception:details={'category':classify(exc),'stage':stage,'suggestion':'Inspect source and traceback.'}
            result.update(status='failed',stage=stage,error_category=details['category'],
                          error=f'{type(exc).__name__}: {exc}',diagnosis=details,
                          traceback=traceback.format_exc(limit=12))
            if temporary.exists():
                temporary.unlink()
        result['seconds'] = time.perf_counter()-begin
        results.append(result)
    return results


def open_database(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=NORMAL')
    db.execute('''CREATE TABLE IF NOT EXISTS files (
        relative TEXT PRIMARY KEY, input_bytes INTEGER, input_mtime_ns INTEGER,
        signature TEXT, status TEXT, output_bytes INTEGER, output_mtime_ns INTEGER,
        error_category TEXT, error TEXT, seconds REAL, updated_at TEXT)''')
    if 'schema' not in {r[1] for r in db.execute('PRAGMA table_info(files)')}:
        db.execute('ALTER TABLE files ADD COLUMN schema TEXT')
    db.commit()
    return db


def jobs_to_run(paths, input_root, output_root, db, signature, force, retry_failed, stats):
    for path in paths:
        stat = path.stat()
        relative = path.relative_to(input_root).as_posix()
        prior = db.execute('SELECT input_bytes,input_mtime_ns,signature,status,output_bytes,output_mtime_ns FROM files WHERE relative=?',(relative,)).fetchone()
        if not force and prior and prior[:3] == (stat.st_size,stat.st_mtime_ns,signature):
            if prior[3] == 'success':
                target = output_root/Path(relative).with_suffix('.py')
                if target.exists():
                    output_stat = target.stat()
                    if (output_stat.st_size,output_stat.st_mtime_ns) == (prior[4],prior[5]):
                        stats['skipped_success'] += 1
                        continue
            elif prior[3] == 'failed' and not retry_failed:
                stats['skipped_failed'] += 1
                continue
        yield {'relative':relative,'input_bytes':stat.st_size,'input_mtime_ns':stat.st_mtime_ns}


def batch_convert(input_root, output_root, *, workers=1, policy='deepcad_compatible', scale=1.0,
                  tolerance=1e-7, logs=None, limit=None, force=False, retry_failed=False,
                  chunk_size=64, restart_every=16384, files_from=None):
    input_root, output_root = Path(input_root).resolve(), Path(output_root).resolve()
    if not input_root.is_dir():
        raise ValueError(f'Input directory does not exist: {input_root}')
    if input_root == output_root or input_root in output_root.parents or output_root in input_root.parents:
        raise ValueError('Input and output trees must be separate and not contain each other')
    if not math.isfinite(scale) or scale <= 0 or not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError('scale and tolerance must be finite positive numbers')
    logs = Path(logs).resolve() if logs else output_root.parent/'logs'/output_root.name
    if logs == input_root or input_root in logs.parents:
        raise ValueError('Logs must not be written inside the source dataset')
    logs.mkdir(parents=True,exist_ok=True)
    output_root.mkdir(parents=True,exist_ok=True)
    with RunLock(logs/'run.lock'):
        settings_path = logs/'settings.json'
        if settings_path.exists():
            previous = json.loads(settings_path.read_text(encoding='utf-8'))
            if previous['input_root'] != str(input_root) or previous['output_root'] != str(output_root):
                raise ValueError('This log directory is already bound to another input/output tree')
        signature = fingerprint(policy,scale,tolerance)
        atomic_json(settings_path,{'input_root':str(input_root),'output_root':str(output_root),
                                  'policy':policy,'scale':scale,'tolerance':tolerance,'fingerprint':signature})
        paths = []
        def collect(directory):
            with os.scandir(directory) as iterator:
                entries = sorted(iterator,key=lambda e:e.name)
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    collect(Path(entry.path))
                elif entry.name.lower().endswith('.json'):
                    paths.append(Path(entry.path))
                if limit and len(paths) >= limit:
                    return
        print(f'Scanning JSON tree: {input_root}',flush=True)
        collect(input_root)
        if files_from:
            if limit:
                raise ValueError('files-from and limit cannot be combined')
            selected=json.loads(Path(files_from).read_text(encoding='utf-8-sig'))
            if not isinstance(selected,list) or not all(isinstance(p,str) for p in selected):
                raise ValueError('files-from must be a JSON array of relative path strings')
            normalized={p.replace('\\','/') for p in selected}
            if any(Path(p).is_absolute() or '..' in Path(p).parts for p in normalized):
                raise ValueError('files-from paths must stay within the input root')
            paths=[p for p in paths if p.relative_to(input_root).as_posix() in normalized]
            if len(paths)!=len(normalized):
                raise ValueError('Some files-from entries are missing or not JSON files')
        if not paths:
            raise ValueError('No JSON files found')
        db = open_database(logs/'state.sqlite3')
        stats = {'status':'running','started_at':timestamp(),'input_root':str(input_root),
                 'output_root':str(output_root),'policy':policy,'workers':workers,'total_files':len(paths),
                 'attempted':0,'success':0,'failed':0,'skipped_success':0,'skipped_failed':0,
                 'output_bytes_this_run':0,'elapsed_seconds':0,'log_directory':str(logs)}
        stats['run_id']=datetime.now().strftime('%Y%m%d_%H%M%S_%f')+f'_{os.getpid()}'
        stats['selection_manifest']=str(Path(files_from).resolve()) if files_from else None
        begin = time.perf_counter(); last_print = 0
        jobs = jobs_to_run(paths,input_root,output_root,db,signature,force,retry_failed,stats)
        events = (logs/'events.jsonl').open('a',encoding='utf-8')

        def progress(final=False):
            nonlocal last_print
            elapsed = time.perf_counter()-begin
            stats['elapsed_seconds'] = round(elapsed,3)
            stats['accounted_files'] = stats['attempted']+stats['skipped_success']+stats['skipped_failed']
            stats['remaining_files'] = len(paths)-stats['accounted_files']
            stats['updated_at'] = timestamp()
            stats['files_per_second_this_run'] = round(stats['attempted']/max(elapsed,1e-9),2)
            atomic_json(logs/'status.json',stats)
            if final or elapsed-last_print >= 10:
                print(f"{stats['accounted_files']}/{len(paths)} | success={stats['success']} failed={stats['failed']} "
                      f"resumed={stats['skipped_success']} | {stats['files_per_second_this_run']} files/s | {elapsed/60:.1f} min",flush=True)
                last_print = elapsed

        progress(final=True)
        try:
            while True:
                epoch = list(itertools.islice(jobs,restart_every))
                if not epoch:
                    break
                chunks = iter([epoch[i:i+chunk_size] for i in range(0,len(epoch),chunk_size)])
                with ProcessPoolExecutor(max_workers=workers,initializer=worker_init,initargs=(policy,)) as pool:
                    pending = {}
                    def submit_one():
                        chunk = next(chunks,None)
                        if chunk is None:
                            return False
                        future = pool.submit(convert_chunk,chunk,str(input_root),str(output_root),policy,scale,tolerance,signature)
                        pending[future] = chunk
                        return True
                    for _ in range(workers*2):
                        if not submit_one():
                            break
                    while pending:
                        ready,_ = wait(pending,timeout=1,return_when=FIRST_COMPLETED)
                        for future in ready:
                            pending.pop(future)
                            results = future.result()  # Worker crash interrupts the run; resume restarts missing records.
                            with db:
                                for record in results:
                                    db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                               tuple(record[k] for k in ['relative','input_bytes','input_mtime_ns','signature','status',
                                                                        'output_bytes','output_mtime_ns','error_category','error','seconds','updated_at'])
                                               +(json.dumps(record['schema'],sort_keys=True),))
                                    record['run_id']=stats['run_id']
                                    events.write(json.dumps(record,ensure_ascii=False)+'\n')
                                    stats['attempted'] += 1
                                    stats[record['status'] if record['status']=='success' else 'failed'] += 1
                                    stats['output_bytes_this_run'] += record['output_bytes']
                            events.flush()
                            submit_one()
                        if ready or time.perf_counter()-last_print >= 10:
                            progress()
                # Periodically recreate workers to release native-library/allocator caches.
            stats['status'] = 'completed_with_errors' if stats['failed'] or stats['skipped_failed'] else 'completed'
        except BaseException as exc:
            stats.update(status='interrupted',error=f'{type(exc).__name__}: {exc}')
            raise
        finally:
            progress(final=True)
            # CSV is UTF-8 BOM for convenient inspection in Windows/Excel.
            import csv
            with (logs/'failures.csv').open('w',encoding='utf-8-sig',newline='') as file:
                writer = csv.writer(file)
                writer.writerow(['relative_path','error_category','error','updated_at'])
                writer.writerows(db.execute("SELECT relative,error_category,error,updated_at FROM files WHERE status='failed' ORDER BY relative"))
            counts = dict(db.execute('SELECT status,COUNT(*) FROM files GROUP BY status'))
            stats['database_totals'] = counts
            stats['database_output_bytes'] = db.execute("SELECT COALESCE(SUM(output_bytes),0) FROM files WHERE status='success'").fetchone()[0]
            stats['ended_at'] = timestamp()
            stats['failure_categories']=dict(db.execute("SELECT error_category,COUNT(*) FROM files WHERE status='failed' GROUP BY error_category"))
            schemas=[{'schema':json.loads(schema),'files':count} for schema,count in db.execute('SELECT schema,COUNT(*) FROM files GROUP BY schema') if schema is not None]
            atomic_json(logs/'schema_report.json',{'total_recorded_files':sum(counts.values()),'variants':schemas})
            atomic_json(logs/'status.json',stats)
            atomic_json(logs/'summary.json',stats)
            (logs/'runs').mkdir(exist_ok=True)
            atomic_json(logs/'runs'/(stats['run_id']+'.json'),stats)
            events.close(); db.close()
        return stats


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input_root',type=Path,nargs='?',default=ROOT/'data/cad_json/cad_json')
    parser.add_argument('output_root',type=Path,nargs='?',default=ROOT/'converted_py')
    parser.add_argument('--workers',type=int,default=1)
    parser.add_argument('--policy',choices=['deepcad_compatible','fused_model','strict_single_body'],default='deepcad_compatible')
    parser.add_argument('--scale',type=float,default=1.0)
    parser.add_argument('--tolerance',type=float,default=1e-7)
    parser.add_argument('--logs',type=Path)
    parser.add_argument('--limit',type=int)
    parser.add_argument('--files-from',type=Path,help='JSON array of relative paths for a targeted retry')
    parser.add_argument('--force',action='store_true',help='Regenerate even unchanged successful outputs')
    parser.add_argument('--retry-failed',action='store_true',help='Retry unchanged failures as well')
    parser.add_argument('--chunk-size',type=int,default=64)
    parser.add_argument('--restart-every',type=int,default=16384)
    args=parser.parse_args()
    if args.workers<1 or args.chunk_size<1 or args.restart_every<1 or (args.limit is not None and args.limit<1):
        parser.error('workers, chunk-size, restart-every and limit must be positive')
    summary=batch_convert(**vars(args))
    print('Summary:',Path(summary['log_directory'])/'summary.json',flush=True)
    if summary['failed'] or summary['skipped_failed']:
        raise SystemExit(2)  # All inputs visited; unsupported inputs listed explicitly.


if __name__=='__main__':
    main()
