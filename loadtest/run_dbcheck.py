# 独立一致性检查入口：不压测，只对当前数据库跑 I1–I14 并打印结果。
"""Run the post-condition SQL checks against the current database."""

from .lib import dbcheck


def main():
    checks = dbcheck.run_checks()
    width = max(len(check["title"]) for check in checks)
    print(f"{'ID':<4} {'状态':<6} {'检查项':<{width}}  实测值")
    for check in checks:
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"{check['id']:<4} {mark:<6} {check['title']:<{width}}  {check['actual']}")
    failed = [check for check in checks if not check["passed"]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
