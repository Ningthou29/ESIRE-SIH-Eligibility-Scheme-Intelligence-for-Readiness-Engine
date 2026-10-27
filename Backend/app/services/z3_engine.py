from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import z3

logger = logging.getLogger("PAANGBA.z3")


@dataclass
class ConstraintOutcome:
    constraint_id: str
    explanation: str
    status: str
    weight: int


@dataclass
class VerificationResult:
    status: str
    outcomes: list[ConstraintOutcome] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)
    satisfied: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "failed_conditions": self.failed,
            "uncertain_conditions": self.uncertain,
            "satisfied_conditions": self.satisfied,
            "outcomes": [outcome.__dict__ for outcome in self.outcomes],
        }


def _get(profile: dict[str, Any], field_name: Any) -> Any:
    if not isinstance(field_name, str):
        return None
    return profile.get(field_name)


def _known(value: Any) -> bool:
    return value is not None and value != ""


def _to_number(value: Any) -> float | None:
    """Best-effort numeric coercion that never raises. A missing, blank,
    non-numeric-string, or otherwise unusable value returns None so the
    caller can degrade the constraint to 'unknown' instead of crashing on
    int()/float() conversion errors (malformed profile or scheme data)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _safe_weight(spec: dict[str, Any]) -> int:
    try:
        return int(spec.get("weight") or 0)
    except (TypeError, ValueError):
        return 0


def _eval_simple(profile: dict[str, Any], spec: dict[str, Any], ctx: z3.Context) -> str:
    """Evaluate one constraint spec against the profile.

    `ctx` is a Z3 Context created fresh for each verify_scheme() call (see
    below) instead of relying on Z3's implicit global "main" context. The
    previous implementation called Int()/Bool()/Solver() with no explicit
    context, which quietly uses one process-wide native Z3 context; FastAPI
    runs sync path functions (like the /api/dashboard handler) in a shared
    thread pool, and Z3's native context object is not thread-safe. Two
    dashboard requests evaluating schemes at the same moment could corrupt
    that shared context, which is exactly what produced the native
    'ASSERTION VIOLATION ... UNEXPECTED CODE WAS REACHED' / 'unreachable'
    crash — it's a C-level Z3 AST corruption bug, not a Python exception,
    so it could not be caught by a try/except around the old code either.
    Giving every verification its own isolated Context makes concurrent
    requests fully independent at the native level.

    This function is also defensive against malformed data: missing
    profile fields, wrong types, missing spec keys, and unsupported rule
    types all resolve to "unknown" instead of raising.
    """
    try:
        if not isinstance(spec, dict):
            return "unknown"
        ctype = spec.get("type")

        if ctype == "age_range":
            age = _to_number(_get(profile, "age"))
            if age is None:
                return "unknown"
            lo = _to_number(spec.get("min"))
            hi = _to_number(spec.get("max"))
            lo = int(lo) if lo is not None else 0
            hi = int(hi) if hi is not None else 120
            solver = z3.Solver(ctx=ctx)
            age_var = z3.Int("age", ctx=ctx)
            solver.add(age_var == int(age))
            solver.add(age_var >= lo)
            solver.add(age_var <= hi)
            return "sat" if solver.check() == z3.sat else "unsat"

        if ctype in ("max_number", "min_number"):
            field_name = spec.get("field")
            value = _to_number(_get(profile, field_name))
            threshold = _to_number(spec.get("value"))
            if value is None or threshold is None:
                return "unknown"
            solver = z3.Solver(ctx=ctx)
            number = z3.Int("n", ctx=ctx)
            solver.add(number == int(value))
            if ctype == "max_number":
                solver.add(number <= int(threshold))
            else:
                solver.add(number >= int(threshold))
            return "sat" if solver.check() == z3.sat else "unsat"

        if ctype == "equals":
            field_name = spec.get("field")
            value = _get(profile, field_name)
            if not _known(value):
                return "unknown"
            solver = z3.Solver(ctx=ctx)
            flag = z3.Bool("eq", ctx=ctx)
            solver.add(flag == z3.BoolVal(value == spec.get("value"), ctx=ctx))
            solver.add(flag)
            return "sat" if solver.check() == z3.sat else "unsat"

        if ctype == "in_set":
            field_name = spec.get("field")
            value = _get(profile, field_name)
            if not _known(value):
                return "unknown"
            raw_values = spec.get("values")
            if not isinstance(raw_values, list):
                return "unknown"
            values = [str(item).lower() if isinstance(item, str) else item for item in raw_values]
            actual = value.lower() if isinstance(value, str) else value
            solver = z3.Solver(ctx=ctx)
            flag = z3.Bool("inset", ctx=ctx)
            solver.add(flag == z3.BoolVal(actual in values, ctx=ctx))
            solver.add(flag)
            return "sat" if solver.check() == z3.sat else "unsat"

        if ctype == "any_of":
            clauses = spec.get("clauses")
            if not isinstance(clauses, list) or not clauses:
                return "unknown"
            results = [_eval_simple(profile, clause, ctx) for clause in clauses]
            if "sat" in results:
                return "sat"
            if all(item == "unsat" for item in results):
                return "unsat"
            return "unknown"

        logger.warning("Unsupported constraint type in scheme rule: %r", ctype)
        return "unknown"

    except Exception as exc:  # noqa: BLE001 - a single malformed rule must
        # never take down the whole /api/dashboard request; degrade to
        # "unknown" and let the rest of the scheme (and catalog) proceed.
        logger.warning("Constraint evaluation failed for spec %r: %s", spec, exc)
        return "unknown"


def verify_scheme(profile: dict[str, Any], scheme: dict[str, Any]) -> VerificationResult:
    """Deterministically verify one scheme against a profile. Never raises —
    on any unexpected internal failure this returns an
    'insufficient_information' result with an explanatory note rather than
    propagating an exception up to the API layer."""
    try:
        return _verify_scheme_inner(profile, scheme)
    except Exception as exc:  # noqa: BLE001
        logger.error("verify_scheme crashed for scheme %r: %s", (scheme or {}).get("id"), exc)
        return VerificationResult(
            status="insufficient_information",
            outcomes=[],
            failed=[],
            uncertain=["This scheme's eligibility rules could not be evaluated."],
            satisfied=[],
        )


def _verify_scheme_inner(profile: dict[str, Any], scheme: dict[str, Any]) -> VerificationResult:
    # Fresh, isolated Z3 context per scheme verification — see the docstring
    # in _eval_simple for why this matters.
    ctx = z3.Context()
    solver = z3.Solver(ctx=ctx)

    constraints = scheme.get("constraints")
    if not isinstance(constraints, list):
        constraints = []

    outcomes: list[ConstraintOutcome] = []
    for index, spec in enumerate(constraints):
        if not isinstance(spec, dict) or not spec.get("id"):
            logger.warning("Skipping malformed constraint in scheme %r: %r", scheme.get("id"), spec)
            continue

        status = _eval_simple(profile, spec, ctx)
        outcomes.append(
            ConstraintOutcome(
                constraint_id=spec["id"],
                explanation=spec.get("explanation") or spec["id"],
                status=status,
                weight=_safe_weight(spec),
            )
        )

        if status in ("sat", "unsat"):
            try:
                # Suffix with the loop index so a duplicated constraint "id"
                # (malformed data) can never collide with an earlier flag.
                flag = z3.Bool(f"c{index}_{spec['id']}", ctx=ctx)
                solver.add(flag == (status == "sat"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not register Z3 flag for constraint %r: %s", spec.get("id"), exc)

    failed = [item.explanation for item in outcomes if item.status == "unsat"]
    uncertain = [item.explanation for item in outcomes if item.status == "unknown"]
    satisfied = [item.explanation for item in outcomes if item.status == "sat"]

    status_by_id = {item.constraint_id: item.status for item in outcomes}
    required_specs = [
        spec for spec in constraints if isinstance(spec, dict) and spec.get("id") and spec.get("required")
    ]
    required_unknown = [
        spec.get("explanation") or spec["id"] for spec in required_specs if status_by_id.get(spec["id"]) == "unknown"
    ]
    required_failed = any(status_by_id.get(spec["id"]) == "unsat" for spec in required_specs)

    if required_failed:
        overall = "not_eligible"
    elif required_unknown:
        overall = "insufficient_information"
    else:
        try:
            solved_ok = solver.check() == z3.sat
        except Exception as exc:  # noqa: BLE001
            logger.warning("Z3 solver check failed for scheme %r: %s", scheme.get("id"), exc)
            solved_ok = True  # an internal solver hiccup shouldn't punish the applicant
        overall = "verified_eligible" if solved_ok else "insufficient_information"

    return VerificationResult(
        status=overall,
        outcomes=outcomes,
        failed=failed,
        uncertain=required_unknown or uncertain,
        satisfied=satisfied,
    )
