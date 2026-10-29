"""Geometry regression cases; no ABC source changes or network required."""
import ast
import unittest
import tempfile
from pathlib import Path
from build123d import Box, Cylinder, Sphere, Cone, Torus, Compound, Location, export_step, Shape
from OCP.BRepCheck import BRepCheck_Analyzer
from step_to_build123d import Generator, ConversionError, read_step
from evaluate_step_conversion import metrics


def imported_fixture(shape):
    parent=Path(__file__).resolve().parents[1]/'output'/'step_test_fixtures'
    parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as tmp:
        path=Path(tmp)/'fixture.step'
        export_step(shape,path)
        return Shape.cast(read_step(path)[0])


def rebuild(shape, policy):
    generator = Generator(shape.wrapped, policy)
    code = generator.generate()
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {'open','eval','exec','__import__'}
    namespace = {'__name__':'generated_test_model'}
    exec(compile(code,'<independent_generated_model>','exec'),namespace)
    return namespace['result'], generator


class SourceGenerationTests(unittest.TestCase):
    def test_exact_primitives_and_boolean_hole(self):
        shapes=[Box(10.24,20.26,30.75),Cylinder(3.7,8.2),Sphere(4.3),
                Cone(4.1,2.3,8.2),Torus(10.3,2.1),Box(12,12,8)-Cylinder(2,20)]
        for shape in shapes:
            with self.subTest(shape=type(shape).__name__):
                shape=imported_fixture(shape)
                result,_=rebuild(shape,'brep_exact')
                self.assertTrue(BRepCheck_Analyzer(result.wrapped,True).IsValid())
                self.assertEqual(metrics(shape.wrapped)['counts'],metrics(result.wrapped)['counts'])
                self.assertAlmostEqual(result.volume,shape.volume,delta=max(1e-8,abs(shape.volume)*1e-10))

    def test_exact_placed_instances(self):
        body=Box(2,3,4)
        shape=Compound(children=[body,body.moved(Location((20,30,40),(10,20,30)))])
        result,_=rebuild(shape,'brep_exact')
        self.assertEqual(len(result.solids()),2)
        self.assertAlmostEqual(result.volume,shape.volume,places=7)
        for a,b in zip(metrics(shape.wrapped)['bbox_mm'],metrics(result.wrapped)['bbox_mm']):
            self.assertAlmostEqual(a,b,places=7)

    def test_grid_polyhedron(self):
        result,generator=rebuild(imported_fixture(Box(10.24,20.26,30.75)),'length_grid_0p5_mm')
        self.assertEqual(len(result.solids()),1)
        self.assertTrue(generator.changes)
        for rec in generator.changes:
            self.assertEqual(float(rec['after'])*2,int(float(rec['after'])*2))
        self.assertNotAlmostEqual(result.volume,10.24*20.26*30.75,places=4)

    def test_grid_feature_collapse(self):
        with self.assertRaises(ConversionError) as caught:
            rebuild(imported_fixture(Box(0.1,10,10)),'length_grid_0p5_mm')
        self.assertEqual(caught.exception.code,'QUANTIZATION_FEATURE_COLLAPSE')

    def test_grid_curved_surface_is_explicitly_unsupported(self):
        with self.assertRaises(ConversionError) as caught:
            rebuild(Cylinder(3.7,8.2),'length_grid_0p5_mm')
        self.assertEqual(caught.exception.code,'QUANTIZATION_RECONSTRUCTION_UNSUPPORTED')

    def test_exact_offset_surface(self):
        from OCP.Geom import Geom_Plane,Geom_OffsetSurface
        from OCP.gp import gp_Ax3
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
        from OCP.BRep import BRep_Tool
        from OCP.TopoDS import TopoDS
        surface=Geom_OffsetSurface(Geom_Plane(gp_Ax3()),2.75,False)
        face=Shape.cast(BRepBuilderAPI_MakeFace(surface,0.,4.,0.,3.,1e-7).Face())
        result,_=rebuild(face,'brep_exact')
        self.assertAlmostEqual(result.area,12.,places=8)
        rebuilt_surface=BRep_Tool.Surface_s(TopoDS.Face(result.wrapped))
        self.assertEqual(type(rebuilt_surface).__name__,'Geom_OffsetSurface')
        self.assertLess(surface.Value(1.,1.).Distance(rebuilt_surface.Value(1.,1.)),1e-12)


if __name__=='__main__':
    unittest.main()
