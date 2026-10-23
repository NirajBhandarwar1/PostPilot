import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_json(path):
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def main():
    parser = argparse.ArgumentParser(description="Gate saved candidate outputs against synthetic cases")
    parser.add_argument("--cases", type=Path, default=ROOT / "cases.json")
    parser.add_argument("--candidate", type=Path, default=ROOT / "candidate_SIMULATED.json")
    args = parser.parse_args()

    try:
        cases = load_json(args.cases)
        candidate = load_json(args.candidate)
        outputs = candidate["outputs"]
        source = candidate["source"]
        if not isinstance(cases, list) or not cases:
            raise ValueError("cases must be a nonempty list")
        if not isinstance(outputs, dict):
            raise ValueError("outputs must be an object keyed by case ID")
        if source not in ("SIMULATED", "USER_SUPPLIED_REAL"):
            raise ValueError("source must be SIMULATED or USER_SUPPLIED_REAL")
        ids = [case["id"] for case in cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate case IDs")
        for case in cases:
            if not isinstance(case["required_fields"], list) or not isinstance(case["invariant"], dict):
                raise ValueError(f"invalid rules for {case['id']}")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        print(f"INPUT ERROR: {error}")
        return 2

    label = "SIMULATED (not model results)" if source == "SIMULATED" else "USER_SUPPLIED_REAL (provenance not verified)"
    print(f"SOURCE: {label}")
    print("CASE     STATUS RULE / DETAIL")
    failed_cases = set()
    failed_rules = 0

    for case in cases:
        case_id = case["id"]
        output = outputs.get(case_id)
        errors = []
        if not isinstance(output, dict):
            errors.append("output missing or not an object")
        else:
            for field in case["required_fields"]:
                if field not in output:
                    errors.append(f"required.{field} missing")
            for field, expected in case["invariant"].items():
                if field in output and (type(output[field]) is not type(expected) or output[field] != expected):
                    errors.append(f"invariant.{field} expected {expected!r}; got {output[field]!r}")

        if errors:
            failed_cases.add(case_id)
            failed_rules += len(errors)
            for error in errors:
                print(f"{case_id:<8} {'FAIL':<6} {error}")
        else:
            print(f"{case_id:<8} {'PASS':<6} -")

    for case_id in sorted(set(outputs) - set(ids)):
        failed_cases.add(case_id)
        failed_rules += 1
        print(f"{case_id:<8} {'FAIL':<6} unexpected output ID")

    if failed_rules:
        print(f"PROMOTION: BLOCKED | failed cases: {len(failed_cases)}/{len(cases)} | failed rules: {failed_rules}")
        return 1
    print(f"PROMOTION: PASS | failed cases: 0/{len(cases)} | failed rules: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
