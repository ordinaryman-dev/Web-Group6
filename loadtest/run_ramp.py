# 阶梯压测入口：同一场景逐级增加虚拟用户，用于画吞吐/延迟随并发变化的曲线。
"""Run the same scenario at several user levels (stress ramp).

    python -m loadtest.run_ramp --scenario browse --users 1,5,10,20,40 --duration 20
"""

import argparse
import json
from pathlib import Path

from .config import BASE_URL
from .lib import dbcheck, seed
from .scenarios import stress


SCENARIOS = {"browse": stress.run_browse, "book": stress.run_book}


def main():
    parser = argparse.ArgumentParser(description="Ramp users up and record throughput")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="browse")
    parser.add_argument("--users", default="1,5,10,20,40")
    parser.add_argument("--duration", type=int, default=20)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--output", default="loadtest/results")
    args = parser.parse_args()

    levels = [int(value) for value in args.users.split(",") if value.strip()]
    seed.ensure_manager_account()
    runs = []
    for users in levels:
        seed.reset_load_bookings()
        label = f"ramp_{args.scenario}_{users}u"
        before = dbcheck.server_counters()
        summary, _, _ = SCENARIOS[args.scenario](
            users=users,
            duration=args.duration,
            base_url=args.base_url,
            output_dir=args.output,
            label=label,
        )
        after = dbcheck.server_counters()
        runs.append({
            "users": users,
            "summary": summary,
            "db_delta": dbcheck.counter_delta(before, after),
        })
        print(
            f"  {args.scenario} {users:>2}u: rps={summary['rps']:>7} "
            f"p50={summary['latency'].get('p50_ms', 0):>7}ms "
            f"p95={summary['latency'].get('p95_ms', 0):>7}ms "
            f"success={summary['success_rate']:.1%} "
            f"outcomes={summary['outcomes']}"
        )

    path = Path(args.output) / f"ramp_{args.scenario}.json"
    path.write_text(
        json.dumps(
            {
                "scenario": args.scenario,
                "duration": args.duration,
                "levels": runs,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
