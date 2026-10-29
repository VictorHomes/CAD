"""R-30 length quantization only; not a STEP reader or geometry converter."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import math

POLICY = "length_grid_0p5_mm"
STEP_MM = Decimal("0.5")
LENGTH_FIELDS = frozenset({
    "point_coordinate", "origin_coordinate", "control_point_coordinate",
    "radius", "semi_axis", "length_offset", "translation_component",
})


class QuantizationError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise QuantizationError("INVALID_QUANTIZATION_INPUT", "Expected a finite numeric value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise QuantizationError("INVALID_QUANTIZATION_INPUT", "Invalid decimal value") from None
    if not result.is_finite():
        raise QuantizationError("INVALID_QUANTIZATION_INPUT", "Non-finite value")
    if isinstance(value, float) and math.ulp(value) >= 0.5:
        raise QuantizationError("QUANTIZATION_RESOLUTION_INSUFFICIENT", "Float spacing reaches the grid step")
    return result


def quantize_length_mm(value):
    """Return Decimal; input must already be in mm. Midpoint ties go away from zero."""
    number = _decimal(value)
    # Precision grows with the coefficient, preserving decimal boundary cases.
    with localcontext() as ctx:
        ctx.prec = max(34, len(number.as_tuple().digits) + 4)
        rounded = (number * 2).to_integral_value(rounding=ROUND_HALF_UP) / 2
    return Decimal("0.0") if rounded.is_zero() else rounded


def quantize_parameter(value, *, object_id, field, field_kind, unit, coordinate_space):
    """Create an auditable candidate, never a geometry-validation success record.

    The caller must identify independent length fields and deduplicate shared IDs.
    Strings in the record preserve exact decimals when serialized as JSON.
    """
    if unit != "mm" or field_kind not in LENGTH_FIELDS:
        raise QuantizationError("INVALID_QUANTIZATION_INPUT", "Only whitelisted mm lengths are accepted")
    if coordinate_space not in {"definition_local", "occurrence_relative"}:
        raise QuantizationError("INVALID_QUANTIZATION_INPUT", "Explicit supported coordinate space required")
    if not isinstance(object_id, str) or not object_id or not isinstance(field, str) or not field:
        raise QuantizationError("INVALID_QUANTIZATION_INPUT", "Object ID and field path required")
    before = _decimal(value)
    after = quantize_length_mm(value)
    if field_kind in {"radius", "semi_axis"} and (before <= 0 or after <= 0):
        raise QuantizationError("QUANTIZATION_FEATURE_COLLAPSE", "Radius or semi-axis must remain positive")
    with localcontext() as ctx:
        ctx.prec = max(34, len(before.as_tuple().digits) + abs(before.adjusted()) + 8)
        delta = after - before
    return {
        "policy": POLICY, "step_mm": "0.5", "unit": unit,
        "midpoint_rule": "away_from_zero", "whitelist_version": "R-30-v1",
        "object_id": object_id, "field": field, "field_kind": field_kind,
        "coordinate_space": coordinate_space,
        "before": str(before), "after": str(after), "delta": str(delta),
        "approximation_used": True, "status": "candidate_unverified",
    }
