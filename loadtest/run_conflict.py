# 冲突测试入口：双客户端与 20 客户端同时抢同一间房，结束后自动跑一致性 SQL。
"""Run the same-room contention suite.

    python -m loadtest.run_conflict --heavy-clients 20 --heavy-rounds 3
"""

import argparse
import json
from pathlib import Path

from .config import BASE_URL
from .lib import dbcheck, seed
from .scenarios import conflict


def main():
    parser = argparse.ArgumentParser(description="HTTP same-room conflict tests")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--two-clients", type=int, default=4,
                        help="number of two-client rounds")
    parser.add_argument("--heavy-clients", type=int, default=20)
    parser.add_argument("--heavy-rounds", type=int, default=3)
    parser.add_argument("--output", default="loadtest/results")
    args = parser.parse_args()

    seed.ensure_manager_account()
    before = dbcheck.server_counters()
    payload = conflict.run(
        base_url=args.base_url,
        two_client_rounds=args.two_clients,
        heavy_clients=args.heavy_clients,
        heavy_rounds=args.heavy_rounds,
        output_dir=args.output,
    )
    after = dbcheck.server_counters()
    payload["db_counters"] = {
        "before": before,
        "after": after,
        "delta": dbcheck.counter_delta(before, after),
    }
    payload["checks"] = dbcheck.run_checks()

    path = Path(args.output) / "conflict.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")

    print(f"\n== Same-room contention ({args.heavy_clients} clients) ==")
    for outcome in payload["rounds"]:
        if outcome.get("aborted"):
            print(f"  round {outcome['round']}: ABORTED {outcome.get('prepare_failures')}")
            continue
        print(
            f"  round {outcome['round']:>2} {outcome['kind']:<14} "
            f"{outcome['check_in']}..{outcome['check_out']} "
            f"success={outcome['successes']} outcomes={outcome['outcomes']} "
            f"db_bookings={outcome['db']['bookings_for_round']} "
            f"db_nights={outcome['db']['nights_for_round']}"
        )
    print("\n== Idempotency ==")
    print(f"  {payload['idempotency']}")
    print("\n== Consistency checks ==")
    for check in payload["checks"]:
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"  [{mark}] {check['id']} {check['title']}: {check['actual']}")
    failed = [check for check in payload["checks"] if not check["passed"]]
    print(f"\nResult: {len(payload['checks']) - len(failed)}/{len(payload['checks'])} "
          f"invariants passed; wrote {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
