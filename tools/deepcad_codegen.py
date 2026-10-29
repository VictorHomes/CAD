"""Emit self-contained build123d code from official DeepCAD's parsed sequence.

Preserves upstream profile decomposition, curve reorder, loop order, and booleans.
Global normalization is deliberately skipped to retain original source scale.
"""
from copy import deepcopy
import math
import numpy as np


def convert_official_document(data, *, source_id='unknown', scale=1.0, tolerance=1e-7):
    from deepcad_official import CADSequence, Line, Circle, Arc, UPSTREAM_COMMIT, UPSTREAM
    from json_to_build123d import RUNTIME, finish_source, ConversionError
    sequence = CADSequence.from_dict(data)
    if not sequence.seq:
        raise ConversionError('Official parser produced no extrusion operations')
    for entity in data['entities'].values():
        if entity.get('type') == 'ExtrudeFeature':
            for key in ('extent_one', 'extent_two'):
                if entity.get(key, {}).get('taper_angle', {}).get('value', 0) != 0:
                    raise ConversionError('Nonzero taper: official builder does not implement it')
    operations = ['NewBodyFeatureOperation','JoinFeatureOperation','CutFeatureOperation','IntersectFeatureOperation']
    # Use the same working scale for every feature and restore source scale once.
    # A threshold used for geometric connections must not erase real thin features.
    bbox=sequence.bbox
    coordinate_scale=max(float(np.max(np.abs(bbox)))*scale,float(np.finfo(float).tiny))
    numerical_floor=float(16*np.finfo(float).eps*coordinate_scale)
    notes=[]
    plans=[]
    references=[]
    for sequence_index,item in enumerate(data['sequence']):
        if item['type']=='ExtrudeFeature':
            for ref in data['entities'][item['entity']].get('profiles',[]):
                references.append({'entity':item['entity'],'profile':ref['profile'],
                                   'sketch':ref['sketch'],'sequence_index':sequence_index})
    for index,extrusion in enumerate(sequence.seq):
        distances=[float(extrusion.extent_one)*scale]
        if extrusion.extent_type==1:distances.append(-float(extrusion.extent_one)*scale)
        elif extrusion.extent_type==2:distances.append(-float(extrusion.extent_two)*scale)
        active=[]
        for side,distance in enumerate(distances):
            if not math.isfinite(distance):
                raise ConversionError('Extrusion distance must be finite')
            if distance==0:
                notes.append(dict(kind='zero_side_omitted',decomposed_extrusion=index,side=side,
                                  original_distance=distance,**references[index]))
            elif abs(distance)<=numerical_floor:
                # At this ratio, coordinate arithmetic cannot resolve the thickness.
                notes.append(dict(kind='sub_precision_side_omitted',approximation=True,
                                  decomposed_extrusion=index,side=side,original_distance=distance,
                                  numerical_floor=numerical_floor,**references[index]))
            else:
                active.append(distance)
        if not active:
            notes.append(dict(kind='zero_volume_feature',decomposed_extrusion=index,
                              operation=operations[extrusion.operation],**references[index]))
        plans.append(active)
    active_distances=[abs(d) for plan in plans for d in plan]
    if not active_distances:
        raise ConversionError('All extrusion features have zero/unresolvable volume')
    minimum=min(active_distances)
    working_scale=max(1.0,1e-3/minimum) if minimum<=tolerance else 1.0
    if not math.isfinite(working_scale) or working_scale>1e9:
        raise ConversionError('Required working scale is numerically unsafe')
    if working_scale>1:
        notes.append(dict(kind='uniform_working_scale',factor=working_scale,
                          minimum_nonzero_extent=minimum,restore_source_scale=True))
    precision_helpers = '''from OCP.gp import gp_Pln, gp_Ax3, gp_Ax2, gp_Pnt, gp_Dir, gp_Circ, gp_Vec
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeEdge
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.TopoDS import TopoDS
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse, BRepAlgoAPI_Cut, BRepAlgoAPI_Common
from build123d import Shape


def _official_circle(radius, center, normal):
    # Match upstream's circle seam axis as well as its geometric locus.
    circle = gp_Circ(gp_Ax2(gp_Pnt(*center), gp_Dir(*normal)), radius)
    return Edge(BRepBuilderAPI_MakeEdge(circle).Edge())


def _official_prism(face, normal, distance):
    vector = gp_Vec(gp_Dir(*normal)).Multiplied(distance)
    return Shape.cast(BRepPrimAPI_MakePrism(face.wrapped, vector).Shape())


def _official_face(wires, origin, x_axis, normal):
    # Preserve upstream's explicit plane instead of fitting a plane to wires.
    plane = gp_Pln(gp_Ax3(gp_Pnt(*origin), gp_Dir(*normal), gp_Dir(*x_axis)))
    builder = BRepBuilderAPI_MakeFace(plane, wires[0].wrapped)
    for wire in wires[1:]:
        builder.Add(TopoDS.Wire(wire.wrapped.Reversed()))
    return Face(builder.Face())


def _official_bool(left, right, operation):
    # Use upstream's kernel operation and defaults; avoid automatic cleanup.
    api = {"NewBodyFeatureOperation": BRepAlgoAPI_Fuse,
           "JoinFeatureOperation": BRepAlgoAPI_Fuse,
           "CutFeatureOperation": BRepAlgoAPI_Cut,
           "IntersectFeatureOperation": BRepAlgoAPI_Common}[operation]
    return Shape.cast(api(left.wrapped, right.wrapped).Shape())


def _official_union(shapes):
    value = shapes[0]
    for shape in shapes[1:]:
        value = _official_bool(value, shape, "JoinFeatureOperation")
    return value


def _apply_solid_operation(body, tool, operation):
    # Empty-volume feature semantics; never silently skip an empty intersection.
    if tool is None:
        return None if operation == "IntersectFeatureOperation" else body
    if body is None:
        return tool if operation in ("NewBodyFeatureOperation", "JoinFeatureOperation") else None
    return _official_bool(body, tool, operation)
'''
    license_header = '\n'.join('# '+line for line in (UPSTREAM/'LICENSE').read_text(encoding='utf-8').splitlines())
    out = [f'# DeepCAD-compatible generated model; source={source_id!r}', license_header,
           f'# Official parser commit={UPSTREAM_COMMIT}; OCP/NumPy adaptation in conversion only.',
           '# No global normalization; source units unknown. Runtime uses build123d and its OCP dependency.',
           RUNTIME, precision_helpers, f'SOURCE_ID = {source_id!r}', f'SCALE = {float(scale)!r}',
           "POLICY = 'deepcad_compatible'", f'TOLERANCE = {float(tolerance)!r}',
           f'WORKING_SCALE = {working_scale!r}',f'CONVERSION_NOTES = {notes!r}',
           '\ndef build_model():', '    body = None']
    for index, extrusion in enumerate(sequence.seq):
        if not plans[index]:
            operation=operations[extrusion.operation]
            out.extend([f'    # Omitted zero-volume extrusion {index}: {operation}',
                        f'    body = _apply_solid_operation(body, None, {operation!r})'])
            continue
        profile = deepcopy(extrusion.profile)
        profile.denormalize(extrusion.sketch_size)
        x, y, n = extrusion.sketch_plane.x_axis, extrusion.sketch_plane.y_axis, extrusion.sketch_plane.normal
        def point(p):
            return tuple(float(v)*scale*working_scale for v in p[0]*x + p[1]*y + extrusion.sketch_pos)
        normal = tuple(float(v) for v in n)
        x_axis = tuple(float(v) for v in x)
        out.extend([f'    # Official decomposed extrusion {index}; operation={operations[extrusion.operation]}', '    segments = []'])
        wires = []
        for j, loop in enumerate(profile.children):
            edges = []
            for k, curve in enumerate(loop.children):
                name = f'f{index}_l{j}_e{k}'
                if isinstance(curve, Line):
                    if np.allclose(curve.start_point, curve.end_point):
                        continue  # Exact upstream omitted-line rule, in original units.
                    expr = f'Edge.make_line({point(curve.start_point)!r}, {point(curve.end_point)!r})'
                elif isinstance(curve, Circle):
                    expr = f'_official_circle({abs(float(curve.radius))*scale*working_scale!r}, {point(curve.center)!r}, {normal!r})'
                elif isinstance(curve, Arc):
                    expr = f'Edge.make_three_point_arc({point(curve.start_point)!r}, {point(curve.mid_point)!r}, {point(curve.end_point)!r})'
                else:
                    raise ConversionError(f'Unsupported parsed curve {type(curve).__name__}')
                out.append(f'    {name} = {expr}')
                edges.append(name)
            wire = f'f{index}_l{j}_wire'
            out.append(f'    {wire} = Wire([{", ".join(edges)}], sequenced=True)')
            wires.append(wire)
        if not wires:
            raise ConversionError('Empty upstream profile')
        face = f'f{index}_face'
        origin = tuple(float(v)*scale*working_scale for v in extrusion.sketch_pos)
        out.append(f'    {face} = _official_face([{", ".join(wires)}], {origin!r}, {x_axis!r}, {normal!r})')
        for distance in plans[index]:
            out.append(f'    segments.append(_official_prism({face}, {normal!r}, {distance*working_scale!r}))')
        out.extend(['    tool = _official_union(segments)',
                    '    body = tool' if index==0 else f'    body = _apply_solid_operation(body, tool, {operations[extrusion.operation]!r})',
                    '    if body is not None and not body.is_valid:',
                    f'        raise ValueError("Invalid geometry after official extrusion {index}")'])
    out.extend(['    if body is None or not body.solids():',
                '        raise ValueError("Model has no solid volume after feature operations")',
                '    if WORKING_SCALE != 1:',
                '        body = body.scale(1 / WORKING_SCALE)',
                '    if not body.is_valid:',
                '        raise ValueError("Invalid geometry after restoring original scale")'])
    return finish_source(out, source_id)
