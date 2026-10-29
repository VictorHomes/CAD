"""Arithmetic contracts only. Run with unittest discovery; no CAD dependencies."""
from decimal import Decimal
import unittest
from step_length_quantization import QuantizationError, quantize_length_mm, quantize_parameter


class LengthQuantizationTests(unittest.TestCase):
    def test_rounding_boundaries_and_idempotence(self):
        cases = {
            "1.24": "1", "1.25": "1.5", "1.26": "1.5",
            "1.74": "1.5", "1.75": "2", "1.76": "2",
            "-1.24": "-1", "-1.25": "-1.5", "-1.75": "-2",
            "0.24": "0", "0.25": "0.5", "-0.25": "-0.5",
            "1.249999999999999999999999999999999999": "1",
            "1.250000000000000000000000000000000001": "1.5",
            "-0.0": "0", "12.5": "12.5",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                actual = quantize_length_mm(raw)
                self.assertEqual(actual, Decimal(expected))
                self.assertEqual(quantize_length_mm(actual), actual)
                self.assertLessEqual(abs(actual - Decimal(raw)), Decimal("0.25"))
                if actual == 0:
                    self.assertFalse(actual.is_signed())

    def test_rejects_invalid_and_unresolved_floats(self):
        for raw in (True, None, "NaN", "Infinity", float("nan"), "text", 2.0 ** 52):
            with self.subTest(raw=raw), self.assertRaises(QuantizationError):
                quantize_length_mm(raw)

    def test_record_and_field_guards(self):
        args = dict(object_id="point_1", field="x", field_kind="point_coordinate",
                    unit="mm", coordinate_space="definition_local")
        record = quantize_parameter("1.25", **args)
        self.assertEqual(record["after"], "1.5")
        self.assertEqual(record["delta"], "0.25")
        self.assertEqual(record["status"], "candidate_unverified")
        for overrides in ({"unit": "inch"}, {"field_kind": "direction"},
                          {"coordinate_space": "world"}, {"field_kind": "radius"}):
            with self.subTest(overrides=overrides), self.assertRaises(QuantizationError):
                quantize_parameter("0.24", **(args | overrides))


if __name__ == "__main__":
    unittest.main()
