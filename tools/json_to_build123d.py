"""Convert DeepCAD cad_json to self-contained build123d 0.13 Python source.

Strict/fused generation uses only stdlib; deepcad_compatible uses the checked-out
official parser and local adapter. Generated scripts use build123d (compatible
mode also uses its installed OCP dependency). Source units are preserved by default.
"""
from __future__ import annotations

import argparse
import json
import math
from copy import deepcopy
from pathlib import Path


class ConversionError(ValueError):
    pass


def number(value, path):
    if isinstance(value, bool):
        raise ConversionError(f'{path}: boolean is not a number')
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConversionError(f'{path}: expected a number') from exc
    if not math.isfinite(result):
        raise ConversionError(f'{path}: expected a finite number')
    return result


def xyz(obj, path):
    if not isinstance(obj, dict):
        raise ConversionError(f'{path}: expected coordinate object')
    return tuple(number(obj.get(k), f'{path}.{k}') for k in 'xyz')


def xy(obj, path):
    if not isinstance(obj, dict):
        raise ConversionError(f'{path}: expected coordinate object')
    if abs(number(obj.get('z', 0), path + '.z')) > 1e-7:
        raise ConversionError(f'{path}: nonzero local z is unsupported')
    return tuple(number(obj.get(k), f'{path}.{k}') for k in 'xy')


def dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def plane(sketch, path):
    t = sketch['transform']
    o, x, y, n = (xyz(t[k], path + '.transform.' + k)
                  for k in ('origin', 'x_axis', 'y_axis', 'z_axis'))
    if any(abs(dot(a, a)-1) > 1e-4 for a in (x, y, n)):
        raise ConversionError(f'{path}: non-unit sketch axis')
    if any(abs(dot(a, b)) > 1e-4 for a, b in ((x, y), (x, n), (y, n))):
        raise ConversionError(f'{path}: non-orthogonal sketch axes')
    if math.dist(cross(n, x), y) > 1e-4:
        raise ConversionError(f'{path}: left-handed sketch plane')
    return o, x, y, n


def ordered_curves(loop, path, tolerance):
    curves = deepcopy(loop.get('profile_curves', []))
    if not curves:
        raise ConversionError(f'{path}: empty loop')
    if any(c.get('type') == 'Circle3D' for c in curves):
        if len(curves) != 1:
            raise ConversionError(f'{path}: full circle mixed with other edges')
        return curves
    # Preserve adjacency and try both possible directions of the first edge.
    for reverse_first in (False, True):
        trial = deepcopy(curves)
        if reverse_first:
            trial[0]['start_point'], trial[0]['end_point'] = trial[0]['end_point'], trial[0]['start_point']
        success = True
        for i in range(1, len(trial)):
            end = xy(trial[i-1]['end_point'], path)
            if math.dist(end, xy(trial[i]['start_point'], path)) <= tolerance:
                continue
            if math.dist(end, xy(trial[i]['end_point'], path)) <= tolerance:
                trial[i]['start_point'], trial[i]['end_point'] = trial[i]['end_point'], trial[i]['start_point']
            else:
                success = False
                break
        if success and math.dist(xy(trial[-1]['end_point'], path), xy(trial[0]['start_point'], path)) <= tolerance:
            return trial
    raise ConversionError(f'{path}: curve chain is open or adjacency is unsupported')


RUNTIME = '''from pathlib import Path
from build123d import Edge, Wire, Face, Plane, Solid, Part, export_step


def _solids(value):
    if value is None:
        return []
    if hasattr(value, "solids"):
        solids = list(value.solids())
        if not solids:
            raise ValueError("Boolean produced only lower-dimensional geometry")
        return solids
    return [s for item in value for s in _solids(item)]


def _union(shapes):
    if not shapes:
        raise ValueError("No nonzero extrusion segments")
    value = shapes[0]
    for shape in shapes[1:]:
        value = Part(_solids(value.fuse(shape)))
    return Part(_solids(value))


def _face(outer, holes, tolerance):
    if not all(w.is_closed and w.is_valid for w in [outer, *holes]):
        raise ValueError("Invalid or open profile wire")
    outer_face = Face(outer)
    hole_faces = [Face(w) for w in holes]
    for i, hole in enumerate(hole_faces):
        # Compare planar areas; unlike bbox sorting this checks containment.
        outside = hole.cut(outer_face)
        if outside.area > tolerance**2:
            raise ValueError("Hole is not inside outer wire")
        if hole.distance_to(outer) <= tolerance:
            raise ValueError("Hole touches outer wire")
        for prior in hole_faces[:i]:
            if hole.distance_to(prior) <= tolerance:
                raise ValueError("Holes overlap, nest or touch")
    face = Face(outer, holes)
    expected = outer_face.area - sum(h.area for h in hole_faces)
    if not face.is_valid or face.area <= tolerance**2:
        raise ValueError("Invalid or degenerate profile face")
    if abs(face.area - expected) > max(tolerance**2, abs(expected)*1e-7):
        raise ValueError("Profile area mismatch")
    return face


def _apply(body, tool, operation, policy):
    if operation == "NewBodyFeatureOperation":
        if body is None:
            return tool
        if policy == "strict_single_body":
            raise ValueError("Later NewBody requires explicit multibody policy")
        return _union([body, tool])
    if body is None:
        raise ValueError("Boolean feature has no existing target")
    if operation == "JoinFeatureOperation":
        raw = body.fuse(tool)
    elif operation == "CutFeatureOperation":
        raw = body.cut(tool)
    elif operation == "IntersectFeatureOperation":
        raw = body.intersect(tool)
    else:
        raise ValueError("Unsupported operation: " + operation)
    solids = _solids(raw)
    if not solids:
        raise ValueError("Feature removed all solids")
    return Part(solids)
'''


def convert_document(data, *, source_id='unknown', scale=1.0,
                     policy='strict_single_body', tolerance=1e-7):
    scale = number(scale, 'scale')
    tolerance = number(tolerance, 'tolerance')
    if scale <= 0 or tolerance <= 0:
        raise ConversionError('scale and tolerance must be positive')
    if policy == 'deepcad_compatible':
        from deepcad_codegen import convert_official_document
        return convert_official_document(data, source_id=source_id, scale=scale, tolerance=tolerance)
    if policy not in ('strict_single_body', 'fused_model'):
        raise ConversionError('Unsupported body policy')
    if not isinstance(data, dict) or not isinstance(data.get('entities'), dict) or not isinstance(data.get('sequence'), list):
        raise ConversionError('Expected original cad_json entities and sequence')
    entities = data['entities']
    out = [f'# Generated from {source_id!r}; source units unknown; build123d 0.13.0.',
           RUNTIME, f'SOURCE_ID = {source_id!r}', f'SCALE = {scale!r}',
           f'POLICY = {policy!r}', f'TOLERANCE = {tolerance!r}',
           '\ndef build_model():', '    body = None']
    feature_count = 0
    for i, item in enumerate(data['sequence']):
        if not isinstance(item, dict) or not isinstance(item.get('entity'), str):
            raise ConversionError(f'sequence[{i}]: expected an entity reference')
        eid = item['entity']
        path = f'sequence[{i}] -> entities[{eid!r}]'
        try:
            e = entities[eid]
            if item['type'] != e['type']:
                raise ConversionError(f'{path}: entity type mismatch')
            if e['type'] == 'Sketch':
                continue
            if e['type'] != 'ExtrudeFeature':
                raise ConversionError(f'{path}: unsupported feature {e["type"]!r}')
            if e['start_extent']['type'] != 'ProfilePlaneStartDefinition':
                raise ConversionError(f'{path}: unsupported start extent')
            operation = e['operation']
            if operation not in ('NewBodyFeatureOperation', 'JoinFeatureOperation', 'CutFeatureOperation', 'IntersectFeatureOperation'):
                raise ConversionError(f'{path}: unsupported operation')
            extent = e['extent_type']
            if extent not in ('OneSideFeatureExtentType', 'SymmetricFeatureExtentType', 'TwoSidesFeatureExtentType'):
                raise ConversionError(f'{path}: unsupported extent type')
            distances = []
            for key in ('extent_one', 'extent_two'):
                side = e.get(key, {})
                if key == 'extent_one' or extent == 'TwoSidesFeatureExtentType':
                    if side.get('type', 'DistanceExtentDefinition') != 'DistanceExtentDefinition':
                        raise ConversionError(f'{path}.{key}: unsupported distance definition')
                    distances.append(number(side['distance']['value'], path + '.' + key)*scale)
                else:
                    distances.append(0.0)
                if number(side.get('taper_angle', {}).get('value', 0), path + '.' + key + '.taper_angle') != 0:
                    raise ConversionError(f'{path}: nonzero taper unsupported')
            if not e['profiles']:
                raise ConversionError(f'{path}: empty profile list')
            prefix = f'f{i:04d}'
            out.extend([f'    # sequence={i}, entity={eid!r}, operation={operation}', '    segments = []'])
            for j, ref in enumerate(e['profiles']):
                spath = path + f'.profiles[{j}]'
                sketch = entities[ref['sketch']]
                if sketch.get('type') != 'Sketch':
                    raise ConversionError(f'{spath}: reference is not a Sketch')
                profiles = sketch['profiles']
                profile_key = ref['profile']
                if isinstance(profiles, list) and (type(profile_key) is not int or not 0 <= profile_key < len(profiles)):
                    raise ConversionError(f'{spath}: invalid profile index')
                profile = profiles[profile_key]
                o, x, y, n = plane(sketch, spath)
                def global_point(p):
                    return tuple(scale*(o[k]+p[0]*x[k]+p[1]*y[k]) for k in range(3))
                loops = profile['loops']
                if sum(loop.get('is_outer') is True for loop in loops) != 1 or any(type(loop.get('is_outer')) is not bool for loop in loops):
                    raise ConversionError(f'{spath}: require exactly one marked outer loop')
                outer = None
                holes = []
                for k, loop in enumerate(loops):
                    lpath = spath + f'.loops[{k}]'
                    curves = ordered_curves(loop, lpath, tolerance/scale)
                    edge_vars = []
                    for q, c in enumerate(curves):
                        cpath = lpath + f'.curves[{q}]'
                        name = f'{prefix}_p{j}_l{k}_e{q}'
                        kind = c['type']
                        if kind in ('Circle3D', 'Arc3D') and 'normal' in c:
                            if math.dist(xyz(c['normal'], cpath+'.normal'), (0, 0, 1)) > 1e-4:
                                raise ConversionError(f'{cpath}: unsupported local normal')
                        if kind == 'Line3D':
                            a, b = (global_point(xy(c[key], cpath+'.'+key)) for key in ('start_point', 'end_point'))
                            if math.dist(a, b) <= tolerance:
                                raise ConversionError(f'{cpath}: degenerate line')
                            expr = f'Edge.make_line({a!r}, {b!r})'
                        elif kind in ('Circle3D', 'Arc3D'):
                            center = xy(c['center_point'], cpath+'.center_point')
                            radius = number(c['radius'], cpath+'.radius')
                            if radius*scale <= tolerance:
                                raise ConversionError(f'{cpath}: nonpositive or tiny radius')
                            if kind == 'Circle3D':
                                expr = f'Edge.make_circle({radius*scale!r}, plane=Plane(origin={global_point(center)!r}, x_dir={x!r}, z_dir={n!r}))'
                            else:
                                refv = xy(c['reference_vector'], cpath+'.reference_vector')
                                if abs(dot(refv, refv)-1) > 1e-4:
                                    raise ConversionError(f'{cpath}: non-unit reference vector')
                                a0, a1 = (number(c[key], cpath+'.'+key) for key in ('start_angle', 'end_angle'))
                                if not 1e-9 < abs(a1-a0) < 2*math.pi-1e-9:
                                    raise ConversionError(f'{cpath}: unsupported arc sweep')
                                def at(angle):
                                    return (center[0]+radius*(refv[0]*math.cos(angle)-refv[1]*math.sin(angle)), center[1]+radius*(refv[0]*math.sin(angle)+refv[1]*math.cos(angle)))
                                start, end = xy(c['start_point'], cpath), xy(c['end_point'], cpath)
                                predicted = (at(a0), at(a1))
                                error = min(max(math.dist(start,predicted[0]),math.dist(end,predicted[1])),max(math.dist(start,predicted[1]),math.dist(end,predicted[0])))
                                if error*scale > max(tolerance, radius*scale*1e-5):
                                    raise ConversionError(f'{cpath}: arc endpoints disagree with angles')
                                expr = f'Edge.make_three_point_arc({global_point(start)!r}, {global_point(at((a0+a1)/2))!r}, {global_point(end)!r})'
                        else:
                            raise ConversionError(f'{cpath}: unsupported curve {kind!r}')
                        out.append(f'    {name} = {expr}')
                        edge_vars.append(name)
                    wire = f'{prefix}_p{j}_l{k}_wire'
                    out.append(f'    {wire} = Wire([{", ".join(edge_vars)}], sequenced=True)')
                    if loop['is_outer']:
                        outer = wire
                    else:
                        holes.append(wire)
                face = f'{prefix}_p{j}_face'
                out.append(f'    {face} = _face({outer}, [{", ".join(holes)}], TOLERANCE)')
                d1, d2 = distances
                lengths = [d1] + ([-d1] if extent == 'SymmetricFeatureExtentType' else [-d2] if extent == 'TwoSidesFeatureExtentType' else [])
                for distance in lengths:
                    if abs(distance) > tolerance:
                        vector = tuple(distance*v for v in n)
                        out.append(f'    segments.append(Solid.extrude({face}, direction={vector!r}))')
            out.extend(['    tool = _union(segments)', f'    tool.label = {eid!r}',
                        f'    body = _apply(body, tool, {operation!r}, POLICY)',
                        '    if not body.is_valid:', f'        raise ValueError({("Invalid geometry after " + path)!r})',
                        '    if POLICY == "strict_single_body" and len(body.solids()) != 1:',
                        '        raise ValueError("Strict single-body policy violated")'])
            feature_count += 1
        except (KeyError, TypeError, IndexError) as exc:
            raise ConversionError(f'{path}: missing or invalid structure: {exc}') from exc
    if not feature_count:
        raise ConversionError('No usable extrusion features')
    return finish_source(out, source_id)


def finish_source(out, source_id):
    out.extend(['    return body', '', 'result = build_model()', '',
                'if __name__ == "__main__":',
                '    import argparse', '    import os', '    parser = argparse.ArgumentParser()',
                '    parser.add_argument("--view", action="store_true", help="Preview in VS Code OCP CAD Viewer")',
                '    parser.add_argument("--port", type=int, default=int(os.environ.get("CAD_VIEWER_PORT", "3940")))',
                '    args = parser.parse_args()',
                '    destination = Path(__file__).with_suffix(".step")',
                '    if not export_step(result, destination):',
                '        raise RuntimeError("STEP export failed")',
                '    print(f"Valid={result.is_valid}; solids={len(result.solids())}; volume={result.volume:.12g}")',
                '    print(destination)',
                '    if args.view or os.environ.get("CAD_VSCODE_PREVIEW") == "1":',
                '        import socket',
                '        try:',
                '            with socket.create_connection(("127.0.0.1", args.port), timeout=2):',
                '                pass',
                '        except OSError as exc:',
                '            raise SystemExit(f"Model exported successfully, but viewer port {args.port} is not listening. In VS Code run: OCP CAD Viewer: Open viewer; confirm RUNNING and port, then run again with --view --port PORT.") from exc',
                '        from ocp_vscode import Camera, set_port, show',
                '        set_port(args.port)',
                '        show(result, names=[SOURCE_ID], reset_camera=Camera.ISO)',
                '        print(f"Preview sent to VS Code OCP CAD Viewer on port {args.port}")', ''])
    source = '\n'.join(out)
    compile(source, source_id, 'exec')
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--scale', type=float, default=1.0)
    parser.add_argument('--tolerance', type=float, default=1e-7, help='Length tolerance in output units')
    parser.add_argument('--policy', choices=['strict_single_body', 'fused_model', 'deepcad_compatible'], default=None,
                        help='Default: deepcad_compatible for a directory, strict_single_body for a file')
    parser.add_argument('--workers', type=int, default=1, help='Persistent workers for directory conversion')
    parser.add_argument('--retry-failed', action='store_true', help='Retry unchanged failures in directory mode')
    parser.add_argument('--force', action='store_true', help='Regenerate unchanged outputs in directory mode')
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error('Input and output paths must differ')
    if args.input.is_dir():
        if args.workers < 1:
            parser.error('workers must be positive')
        from batch_convert import batch_convert
        summary = batch_convert(args.input, args.output, workers=args.workers,
                                policy=args.policy or 'deepcad_compatible', scale=args.scale,
                                tolerance=args.tolerance, force=args.force, retry_failed=args.retry_failed)
        print('Summary:', Path(summary['log_directory'])/'summary.json')
        if summary['failed'] or summary['skipped_failed']:
            raise SystemExit(2)
        return
    try:
        data = json.loads(args.input.read_text(encoding='utf-8-sig'))
        source = convert_document(data, source_id=args.input.name, scale=args.scale, policy=args.policy or 'strict_single_body', tolerance=args.tolerance)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(source, encoding='utf-8')
    except (ConversionError, json.JSONDecodeError) as exc:
        parser.exit(1, f'Conversion failed: {exc}\n')
    print(args.output.resolve())


if __name__ == '__main__':
    main()
