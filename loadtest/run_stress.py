# 压力测试入口：按场景启动虚拟用户，采集延迟/吞吐/业务结果，结束后跑一致性 SQL。
"""Run one stress scenario and persist raw samples plus a summary.

    python -m loadtest.run_stress --scenario browse --users 20 --duration 45
    python -m loadtest.run_stress --scenario book   --users 15 --duration 60
    python -m loadtest.run_stress --scenario mixed  --users 20 --duration 60
"""

import argparse
import json
from pathlib import Path

from .config import BASE_URL
from .lib import dbcheck, seed
from .scenarios import stress


SCENARIOS = {
    "browse": stress.run_browse,
    "book": stress.run_book,
    "mixed": stress.run_mixed,
}


def main():
    parser = argparse.ArgumentParser(description="Hero Hotel HTTP stress test")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--duration", type=int, default=45, help="seconds")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--label", default=None)
    parser.add_argument("--output", default="loadtest/results")
    parser.add_argument("--no-reset", action="store_true",
                        help="keep bookings created by earlier load runs")
    args = parser.parse_args()

    seed.ensure_manager_account()
    if not args.no_reset:
        seed.reset_load_bookings()
    label = args.label or f"{args.scenario}_{args.users}u"
    before = dbcheck.server_counters()
    summary, csv_path, json_path = SCENARIOS[args.scenario](
        users=args.users,
        duration=args.duration,
        base_url=args.base_url,
        output_dir=args.output,
        label=label,
    )
    after = dbcheck.server_counters()
    checks = dbcheck.run_checks(
        include_payment_checks=args.scenario != "browse"
    )

    payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
    payload["extra"] = {
        "db_counters": {
            "before": before,
            "after": after,
            "delta": dbcheck.counter_delta(before, after),
        },
        "checks": checks,
        "users": args.users,
        "duration": args.duration,
        "base_url": args.base_url,
    }
    Path(json_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    print(f"\n== {summary['scenario']} · {args.users} users · {summary['duration_s']}s ==")
    print(f"  requests={summary['requests']}  rps={summary['rps']}  "
          f"success_rate={summary['success_rate']:.1%}")
    print(f"  latency p50={summary['latency'].get('p50_ms')}ms  "
          f"p95={summary['latency'].get('p95_ms')}ms  "
          f"p99={summary['latency'].get('p99_ms')}ms  "
          f"max={summary['latency'].get('max_ms')}ms")
    print(f"  outcomes={summary['outcomes']}")
    failed = [check for check in checks if not check["passed"]]
    print(f"  invariants: {len(checks) - len(failed)}/{len(checks)} passed")
    for check in failed:
        print(f"    [FAIL] {check['id']} {check['title']}: {check['actual']}")
    print(f"  wrote {csv_path} and {json_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
