"""Run the voice-assistant evals against the real Claude API.

    ANTHROPIC_API_KEY=... python -m evals.run                 # all cases, 3 runs each
    python -m evals.run --only event --repeats 5               # a tag or a case id
    python -m evals.run --min 0.9                              # exit 1 below a 90% pass rate

Cases live in evals/cases.json (public, made-up family) and, if present, evals/private_cases.json
(gitignored: drafts from your own voice log, see evals/from_log.py). Each case runs several times
because the model is not deterministic: a case that passes some runs and fails others is flaky.
Results go to evals/results/latest.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.assistant import MODEL, Assistant, AssistantError
from evals.score import score

HERE = Path(__file__).parent


def load(path: Path) -> tuple[dict, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["context"], data["cases"]


def run_case(case: dict, context: dict, assistant: Assistant, repeats: int) -> dict:
    ctx = {**context, **case.get("context", {})}
    zone = ZoneInfo(ctx["timezone"])
    now = datetime.fromisoformat(case.get("now", ctx["now"])).replace(tzinfo=zone)
    runs = []
    for _ in range(repeats):
        try:
            plan = assistant.plan(case["text"], ctx["lists"], ctx["calendars"], zone, now, weather=True)
            ok, reasons = score(case["expect"], plan)
            runs.append({"ok": ok, "reasons": reasons, "actions": plan.actions, "reply": plan.reply})
        except AssistantError as exc:
            runs.append({"ok": False, "reasons": [f"error: {exc}"], "actions": [], "reply": ""})
    passed = sum(r["ok"] for r in runs)
    status = "PASS" if passed == len(runs) else "FAIL" if passed == 0 else "FLAKY"
    return {"id": case["id"], "tags": case.get("tags", []), "text": case["text"], "status": status,
            "passed": passed, "runs": runs}


def run_all(cases: list[dict], context: dict, make_assistant, repeats: int, workers: int = 4) -> list[dict]:
    """make_assistant(context) -> Assistant; one per context so people and aliases match."""
    def one(case):
        ctx = {**context, **case.get("context", {})}
        return run_case(case, context, make_assistant(ctx), repeats)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, cases))


def summary(results: list[dict]) -> tuple[float, dict[str, tuple[int, int]]]:
    total = sum(len(r["runs"]) for r in results)
    passed = sum(r["passed"] for r in results)
    by_tag: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in results:
        for tag in r["tags"] or ["untagged"]:
            by_tag[tag][0] += r["passed"]
            by_tag[tag][1] += len(r["runs"])
    return (passed / total if total else 0.0), {t: (p, n) for t, (p, n) in sorted(by_tag.items())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--only", help="a tag or a case id")
    parser.add_argument("--min", type=float, default=0.0, help="exit 1 if the pass rate is below this (0-1)")
    args = parser.parse_args(argv)

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("Set ANTHROPIC_API_KEY to run the evals.", file=sys.stderr)
        return 2

    context, cases = load(HERE / "cases.json")
    private = HERE / "private_cases.json"
    if private.exists():
        private_context, private_cases = load(private)
        cases += [{**c, "context": private_context, "tags": [*c.get("tags", []), "private"]}
                  for c in private_cases if not c.get("review")]
    if args.only:
        cases = [c for c in cases if args.only == c["id"] or args.only in c.get("tags", [])]

    def make(ctx):
        return Assistant(key, aliases=tuple(ctx.get("aliases", ())), people=ctx.get("people", {}))

    results = run_all(cases, context, make, args.repeats)
    rate, by_tag = summary(results)

    for r in results:
        if r["status"] != "PASS":
            first = next(x["reasons"][0] for x in r["runs"] if not x["ok"])
            print(f"{r['status']:5}  {r['id']}  ({r['passed']}/{len(r['runs'])})  {first}")
    print()
    for tag, (p, n) in by_tag.items():
        print(f"{tag:10} {p:3}/{n:<3} {p / n:6.0%}")
    print(f"\n{MODEL}: {rate:.0%} of {sum(len(r['runs']) for r in results)} runs passed "
          f"({sum(r['status'] == 'PASS' for r in results)}/{len(results)} cases on every run)")

    out = HERE / "results"
    out.mkdir(exist_ok=True)
    report = {"model": MODEL, "at": datetime.now().isoformat(timespec="seconds"), "pass_rate": rate,
              "by_tag": by_tag, "results": results}
    (out / "latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if rate < args.min else 0


if __name__ == "__main__":
    sys.exit(main())
