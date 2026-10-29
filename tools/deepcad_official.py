"""Run upstream DeepCAD parsing and unchanged geometry functions with OCP bindings.

This is an OCP adaptation, not an installation of upstream's pythonocc environment.
Upstream checkout remains unchanged. No build123d converter is used to rebuild JSON.
"""
from __future__ import annotations

import ast
import argparse
import json
import os
from pathlib import Path
import socket
import sys
from copy import copy

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / 'third_party/DeepCAD'
os.environ.setdefault('MPLCONFIGDIR', str(ROOT / 'tmp/matplotlib'))
Path(os.environ['MPLCONFIGDIR']).mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(UPSTREAM))

import numpy as np
from cadlib.extrude import CADSequence, Extrude, CoordSystem
from cadlib.sketch import Loop, Profile
from cadlib.curves import CurveBase, Line, Circle, Arc
from cadlib.macro import EXTRUDE_OPERATIONS, EXTENT_TYPE
from OCP.gp import gp_Pnt, gp_Dir, gp_Circ, gp_Pln, gp_Vec, gp_Ax3, gp_Ax2, gp_Lin
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse, BRepAlgoAPI_Common
from OCP.GC import GC_MakeArcOfCircle
from OCP.TopoDS import TopoDS
from build123d import Shape, export_step

UPSTREAM_COMMIT = 'f7b350790a3bce0267a5345339fd09be0250c8cb'


class _LegacyNumpy:
    """Restore old np.cross 2D semantics only inside upstream module namespaces."""
    def __getattr__(self, name):
        if name == 'int':
            return int
        if name == 'float':
            return float
        return getattr(np, name)

    @staticmethod
    def cross(a, b):
        a, b = np.asarray(a), np.asarray(b)
        if a.shape[-1] == 2 and b.shape[-1] == 2:
            return a[..., 0]*b[..., 1] - a[..., 1]*b[..., 0]
        if a.shape[-1] == 2:
            a = np.concatenate([a, np.zeros(a.shape[:-1]+(1,))], axis=-1)
        if b.shape[-1] == 2:
            b = np.concatenate([b, np.zeros(b.shape[:-1]+(1,))], axis=-1)
        return np.cross(a, b)


for _module in ('cadlib.sketch', 'cadlib.curves', 'cadlib.extrude', 'cadlib.math_utils'):
    sys.modules[_module].np = _LegacyNumpy()


def _load_upstream_functions():
    """Load exact function ASTs, replacing only the import/binding namespace."""
    path = UPSTREAM / 'cadlib/visualize.py'
    source = path.read_text(encoding='utf-8')
    wanted = {'create_CAD', 'create_by_extrude', 'create_profile_face',
              'create_loop_3d', 'create_edge_3d', 'point_local2global'}
    tree = ast.parse(source, filename=str(path))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    if {n.name for n in nodes} != wanted:
        raise RuntimeError('Upstream geometry function set changed')
    namespace = dict(globals())
    class FaceBuilderCompatibility:
        def __init__(self, *args):
            self.builder = BRepBuilderAPI_MakeFace(*args)

        def Add(self, wire):
            # pythonocc accepted generic reversed TopoDS_Shape; OCP needs Wire.
            self.builder.Add(TopoDS.Wire(wire))

        def __getattr__(self, name):
            return getattr(self.builder, name)

    namespace['BRepBuilderAPI_MakeFace'] = FaceBuilderCompatibility
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['create_CAD']


def reconstruct(json_path, normalize=False):
    data = json.loads(Path(json_path).read_text(encoding='utf-8-sig'))
    sequence = CADSequence.from_dict(data)
    if not sequence.seq:
        raise ValueError('Upstream parser produced no extrusion operations')
    if normalize:
        sequence.normalize()
    raw = _load_upstream_functions()(sequence)
    model = Shape.cast(raw)
    if not model.is_valid or not model.solids():
        raise ValueError('Official reconstruction is invalid or contains no solids')
    return model


def show_model(model, port=3940, name='DeepCAD source JSON'):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=2):
            pass
    except OSError as exc:
        raise SystemExit(f'Model exported, viewer port {port} is not listening. '
                         'In VS Code run OCP CAD Viewer: Open viewer, then retry.') from exc
    from ocp_vscode import Camera, set_port, show
    set_port(port)
    show(model, names=[name], reset_camera=Camera.ISO)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--normalize', action='store_true', help='Match upstream show.py global normalization')
    parser.add_argument('--view', action='store_true')
    parser.add_argument('--port', type=int, default=3940)
    args = parser.parse_args()
    output = args.output or ROOT/'output/deepcad_official'/args.input.stem/('normalized.step' if args.normalize else 'original.step')
    if output.resolve() == args.input.resolve():
        parser.error('Output cannot overwrite input JSON')
    model = reconstruct(args.input, normalize=args.normalize)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not export_step(model, output):
        raise RuntimeError('STEP export failed')
    print(f'Official DeepCAD parser + geometry / OCP adapter; upstream={UPSTREAM_COMMIT}')
    print(f'normalize={args.normalize}; valid={model.is_valid}; solids={len(model.solids())}; volume={model.volume:.15g}')
    print(output.resolve())
    if args.view or os.environ.get('CAD_VSCODE_PREVIEW') == '1':
        show_model(model,args.port,f'DeepCAD JSON {args.input.name}')


if __name__ == '__main__':
    main()
