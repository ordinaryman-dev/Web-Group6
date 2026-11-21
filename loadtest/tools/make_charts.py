# 图表生成：把压测原始 JSON 转成报告用的延迟、吞吐、结果分布和锁竞争图。
"""Render the report charts from the raw load-test JSON files."""

import argparse
import json
from collections import Counter
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.dpi"] = 160
plt.rcParams["savefig.dpi"] = 160
plt.rcParams["axes.edgecolor"] = "#c9d2cc"
plt.rcParams["axes.linewidth"] = 0.8
plt.rcParams["axes.grid"] = True
plt.rcParams["grid.color"] = "#e6ebe7"
plt.rcParams["grid.linewidth"] = 0.7

GREEN = "#1f4d3a"
GOLD = "#b8860b"
RED = "#a33a2a"
BLUE = "#2f5d8a"
GRAY = "#6b7280"
PALE = "#dce8e0"

LANG = "zh"
STRINGS = {
    "latency_x": ("响应时间（毫秒，对数轴）", "Response time (ms, log scale)"),
    "cumulative_requests": ("累计请求比例", "Cumulative share of requests"),
    "ms": ("毫秒", "Milliseconds"),
    "latency_title": (
        "三类压力场景的响应时间分位（成功与业务拒绝合并统计）",
        "Response-time percentiles across the three stress scenarios",
    ),
    "single_request_x": (
        "单请求响应时间（毫秒，对数轴）",
        "Single-request response time (ms, log scale)",
    ),
    "cumulative_share": ("累计比例", "Cumulative share"),
    "ecdf_title": (
        "响应时间累计分布（ECDF）：尾部延迟对比",
        "Response-time ECDF: tail-latency comparison",
    ),
    "throughput": ("吞吐量（请求/秒）", "Throughput (req/s)"),
    "concurrent_users": ("并发虚拟用户数", "Concurrent virtual users"),
    "requests_per_second": ("请求/秒", "Requests/s"),
    "p95_latency": ("P95 延迟（毫秒）", "P95 latency (ms)"),
    "total_requests": ("请求总数", "Total requests"),
    "requests_per_scenario": ("各场景实际发出的请求数", "Requests issued per scenario"),
    "abnormal_rate": ("非正常响应比例（%）", "Non-normal response rate (%)"),
    "outcome_title": (
        "业务拒绝与 HTTP 错误：无 5xx",
        "Business rejections and HTTP errors: no 5xx",
    ),
    "clients": ("客户端", "clients"),
    "post_latency": (
        "POST /guest/book 响应时间（毫秒）",
        "POST /guest/book response time (ms)",
    ),
    "conflict_title": (
        "同一间房的抢订延迟：20 个请求在 Room 行锁上排队",
        "Same-room contention: 20 requests queue on the Room row lock",
    ),
    "two_clients": ("2 客户端", "2 clients"),
    "twenty_clients": ("20 客户端", "20 clients"),
    "success_orders": ("成功创建订单", "Successful bookings"),
    "rejected": ("被拒绝（售罄/忙碌）", "Rejected (sold out / busy)"),
    "success": ("成功", "success"),
    "requests": ("请求数", "Requests"),
    "conflict_outcome_title": (
        "每一轮恰好只有一张订单写入，无超售",
        "Exactly one booking per round, no overbooking",
    ),
    "row_lock_waits": ("InnoDB 行锁等待次数", "InnoDB row-lock waits"),
    "slow_queries": ("慢查询数", "Slow queries"),
    "count": ("次数", "Count"),
    "db_title": (
        "数据库侧锁竞争：读路径为 0，写入场景出现可解释的行锁等待",
        "Database-side lock contention: zero waits on reads, explainable waits on writes",
    ),
    "seconds": ("压测开始后的秒数", "Seconds since load start"),
    "rps_per_second": ("每秒请求数", "Requests per second"),
    "timeline_title": (
        "{label}：负载期间每秒请求数（稳态，无雪崩）",
        "{label}: requests per second during the run (steady state, no collapse)",
    ),
    "read_only": ("S-browse（只读）", "S-browse (read-only)"),
    "write_book": ("S-book（写订单）", "S-book (write)"),
    "scaling_x": ("并发虚拟用户数", "Concurrent virtual users"),
    "scaling_y": ("吞吐量（请求/秒）", "Throughput (req/s)"),
    "scaling_title": (
        "读路径与写路径的扩展性对比",
        "Read-path vs write-path scalability",
    ),
    "ramp_browse_title": (
        "S-browse 阶梯压测：40 VU 时约 154 请求/秒，P95 354ms",
        "S-browse ramp: ~154 req/s at 40 VU, P95 354 ms",
    ),
    "ramp_book_title": (
        "S-book 阶梯压测：写路径在约 33 请求/秒处饱和",
        "S-book ramp: the write path saturates at ~33 req/s",
    ),
    "conflict_label": ("S-conflict\n20 并发抢房", "S-conflict\n20 concurrent"),
}


def t(key):
    chinese, english = STRINGS[key]
    return chinese if LANG == "zh" else english


def load(results_dir, name):
    path = Path(results_dir) / name
    if not path.exists():
        raise SystemExit(f"missing result file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def save(figure, output_dir, name):
    path = Path(output_dir) / name
    figure.tight_layout()
    figure.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(figure)
    print(f"wrote {path}")


def latency_style(ax):
    ax.set_xlabel(t("latency_x"))
    ax.set_ylabel(t("cumulative_requests"))
    ax.set_xscale("log")


def chart_latency_percentiles(output_dir, scenarios):
    labels = [item[0] for item in scenarios]
    p50 = [item[1]["summary"]["latency"]["p50_ms"] for item in scenarios]
    p95 = [item[1]["summary"]["latency"]["p95_ms"] for item in scenarios]
    p99 = [item[1]["summary"]["latency"]["p99_ms"] for item in scenarios]
    x = range(len(labels))
    width = 0.26
    figure, ax = plt.subplots(figsize=(9, 4.6))
    bars = [
        ax.bar([i - width for i in x], p50, width, label="P50", color="#8fb3a0", edgecolor=GREEN),
        ax.bar(list(x), p95, width, label="P95", color=GOLD, alpha=0.9),
        ax.bar([i + width for i in x], p99, width, label="P99", color=GREEN, alpha=0.9),
    ]
    for group in bars:
        for bar in group:
            height = bar.get_height()
            ax.annotate(f"{height:.0f}", (bar.get_x() + bar.get_width() / 2, height),
                        ha="center", va="bottom", fontsize=8, color="#333")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_ylabel(t("ms"))
    ax.set_title(t("latency_title"))
    ax.legend(frameon=False, ncol=3, loc="upper left")
    save(figure, output_dir, "chart_latency_percentiles.png")


def chart_ecdf(output_dir, scenarios):
    figure, ax = plt.subplots(figsize=(9, 4.6))
    colors = [BLUE, GREEN, GOLD]
    for (label, payload), color in zip(scenarios, colors):
        values = sorted(
            sample["latency_ms"] for sample in payload["samples"]
            if sample["latency_ms"] > 0
        )
        if not values:
            continue
        ys = [index / len(values) for index in range(1, len(values) + 1)]
        ax.plot(values, ys, label=f"{label} (n={len(values)})", color=color, linewidth=1.8)
    ax.set_xscale("log")
    ax.set_xlabel(t("single_request_x"))
    ax.set_ylabel(t("cumulative_share"))
    ax.set_yticks([0, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99, 1.0])
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:.0%}"))
    ax.axhline(0.95, color=RED, linestyle="--", linewidth=1, alpha=0.6)
    ax.set_title(t("ecdf_title"))
    ax.legend(frameon=False, loc="lower right")
    save(figure, output_dir, "chart_latency_ecdf.png")


def chart_ramp(output_dir, ramp, single_level=None, title="", filename="chart_ramp.png"):
    levels = [item["users"] for item in ramp["levels"]]
    rps = [item["summary"]["rps"] for item in ramp["levels"]]
    p95 = [item["summary"]["latency"]["p95_ms"] for item in ramp["levels"]]
    if single_level:
        levels.append(single_level[0])
        rps.append(single_level[1]["rps"])
        p95.append(single_level[1]["latency"]["p95_ms"])
    order = sorted(range(len(levels)), key=lambda index: levels[index])
    levels = [levels[index] for index in order]
    rps = [rps[index] for index in order]
    p95 = [p95[index] for index in order]

    figure, ax = plt.subplots(figsize=(9, 4.6))
    ax.plot(levels, rps, marker="o", color=GREEN, linewidth=2, label=t("throughput"))
    for x, y in zip(levels, rps):
        ax.annotate(f"{y:.1f}", (x, y), textcoords="offset points", xytext=(0, 8),
                    ha="center", fontsize=8, color=GREEN)
    ax.set_xlabel(t("concurrent_users"))
    ax.set_ylabel(t("requests_per_second"), color=GREEN)
    ax.tick_params(axis="y", labelcolor=GREEN)
    ax.set_xticks(levels)
    ax.set_ylim(0, max(rps) * 1.22)
    ax2 = ax.twinx()
    ax2.plot(levels, p95, marker="s", linestyle="--", color=GOLD, linewidth=1.6,
             label=t("p95_latency"))
    for x, y in zip(levels, p95):
        if x == max(levels):
            offset = (10, -18)
        elif y < 150:
            offset = (10, -14)
        else:
            offset = (10, -4)
        ax2.annotate(f"{y:.0f}", (x, y), textcoords="offset points", xytext=offset,
                     ha="left", fontsize=8, color=GOLD)
    ax2.set_ylabel(t("p95_latency"), color=GOLD)
    ax2.tick_params(axis="y", labelcolor=GOLD)
    ax2.set_ylim(0, max(p95) * 1.22)
    ax2.grid(False)
    ax.set_title(title)
    lines = ax.get_lines() + ax2.get_lines()
    ax.legend(lines, [line.get_label() for line in lines], frameon=False, loc="upper left")
    save(figure, output_dir, filename)


def _outcome_groups(summary):
    outcomes = Counter(summary["outcomes"])
    normal = outcomes.get("success", 0) + outcomes.get("ok", 0)
    business = sum(
        outcomes.get(key, 0) for key in
        ("sold_out", "busy", "unavailable", "rejected", "price_changed",
         "idempotency_mismatch", "prepare_failed")
    )
    errors = sum(
        outcomes.get(key, 0) for key in
        ("http_error", "client_error", "invalid_json", "unknown", "barrier_broken")
    )
    return normal, business, errors


def chart_outcomes(output_dir, scenarios):
    labels = [item[0] for item in scenarios]
    groups = [_outcome_groups(item[1]["summary"]) for item in scenarios]
    totals = [sum(group) or 1 for group in groups]
    abnormal = [(group[1] + group[2]) / total * 100 for group, total in zip(groups, totals)]
    x = range(len(labels))
    figure, (left, right) = plt.subplots(1, 2, figsize=(10, 4.0), gridspec_kw={"width_ratios": [1, 1]})
    left.bar(x, totals, color=GREEN)
    for index, total in enumerate(totals):
        left.annotate(f"{total}", (index, total), ha="center", va="bottom", fontsize=9)
    left.set_xticks(list(x))
    left.set_xticklabels(labels, fontsize=8)
    left.set_ylabel(t("total_requests"))
    left.set_title(t("requests_per_scenario"))
    left.set_ylim(0, max(totals) * 1.2)

    right.bar(x, abnormal, color=RED, width=0.5)
    for index, value in enumerate(abnormal):
        right.annotate(f"{value:.3f}%", (index, value), ha="center", va="bottom", fontsize=9)
    right.set_xticks(list(x))
    right.set_xticklabels(labels, fontsize=8)
    right.set_ylabel(t("abnormal_rate"))
    right.set_title(t("outcome_title"))
    right.set_ylim(0, max(0.5, max(abnormal) * 1.4))
    save(figure, output_dir, "chart_outcomes.png")


def chart_conflict(output_dir, payload, scenarios=None):
    rounds = [item for item in payload["rounds"] if not item.get("aborted")]
    figure, ax = plt.subplots(figsize=(9, 4.6))
    labels = []
    medians = []
    lows = []
    highs = []
    colors = []
    for item in rounds:
        values = item["post_latency_ms"]
        if not values:
            continue
        ordered = sorted(values)
        labels.append(f"#{item['round']}\n{item['clients']} {t('clients')}")
        medians.append(ordered[len(ordered) // 2])
        lows.append(ordered[0])
        highs.append(ordered[-1])
        colors.append(GREEN if item["kind"] == "book_2_clients" else GOLD)
    positions = range(len(labels))
    for position, low, median, high, color in zip(positions, lows, medians, highs, colors):
        ax.vlines(position, low, high, color=color, linewidth=5, alpha=0.35)
        ax.plot(position, median, "o", color=color, markersize=8)
        ax.annotate(f"{median:.0f}ms", (position, median), textcoords="offset points",
                    xytext=(0, 9), ha="center", fontsize=8)
    ax.set_xticks(list(positions))
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel(t("post_latency"))
    ax.set_title(t("conflict_title"))
    from matplotlib.lines import Line2D
    ax.legend(handles=[
        Line2D([0], [0], color=GREEN, marker="o", linestyle="", label=t("two_clients")),
        Line2D([0], [0], color=GOLD, marker="o", linestyle="", label=t("twenty_clients")),
    ], frameon=False, loc="upper left")
    save(figure, output_dir, "chart_conflict_latency.png")

    figure, ax = plt.subplots(figsize=(9, 4.2))
    successes = [item["successes"] for item in rounds]
    rejected = [item["clients"] - item["successes"] for item in rounds]
    positions = range(len(rounds))
    ax.bar(positions, successes, color=GREEN, label=t("success_orders"))
    ax.bar(positions, rejected, bottom=successes, color="#c9c2b4",
           label=t("rejected"))
    for position, success in zip(positions, successes):
        ax.annotate(f"{success} {t('success')}", (position, success),
                    ha="center", va="bottom", fontsize=8, color=GREEN)
    ax.set_xticks(list(positions))
    ax.set_xticklabels([
        f"#{item['round']}\n{item['clients']} {t('clients')}" for item in rounds
    ], fontsize=8)
    ax.set_ylabel(t("requests"))
    ax.set_title(t("conflict_outcome_title"))
    ax.legend(frameon=False, loc="upper right")
    save(figure, output_dir, "chart_conflict_outcomes.png")


def chart_db_contention(output_dir, scenarios):
    labels = [item[0] for item in scenarios]
    waits = []
    slow = []
    for _, payload in scenarios:
        delta = payload.get("db_counters", {}).get("delta") or \
            payload.get("extra", {}).get("db_counters", {}).get("delta", {})
        waits.append(delta.get("Innodb_row_lock_waits", 0))
        slow.append(delta.get("Slow_queries", 0))
    x = range(len(labels))
    figure, ax = plt.subplots(figsize=(9, 4.3))
    ax.bar([i - 0.18 for i in x], waits, 0.36, color=BLUE, label=t("row_lock_waits"))
    ax.bar([i + 0.18 for i in x], slow, 0.36, color=RED, label=t("slow_queries"))
    for index, value in enumerate(waits):
        ax.annotate(str(value), (index - 0.18, value), ha="center", va="bottom", fontsize=8)
    for index, value in enumerate(slow):
        ax.annotate(str(value), (index + 0.18, value), ha="center", va="bottom", fontsize=8)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_ylabel(t("count"))
    ax.set_ylim(0, max(waits + [1]) * 1.25)
    ax.set_title(t("db_title"))
    ax.legend(frameon=False)
    save(figure, output_dir, "chart_db_contention.png")


def chart_timeline(output_dir, payload, label="S-mixed 20 VU"):
    samples = payload["samples"]
    if not samples:
        return
    start = min(sample["ts"] for sample in samples)
    buckets = Counter(int(sample["ts"] - start) for sample in samples)
    seconds = sorted(buckets)
    figure, ax = plt.subplots(figsize=(9, 4.2))
    ax.plot(seconds, [buckets[second] for second in seconds], color=GREEN, linewidth=1.6)
    ax.fill_between(seconds, [buckets[second] for second in seconds], color=PALE, alpha=0.8)
    ax.set_xlabel(t("seconds"))
    ax.set_ylabel(t("rps_per_second"))
    ax.set_title(t("timeline_title").format(label=label))
    save(figure, output_dir, "chart_timeline.png")


def chart_scaling_compare(output_dir, browse_ramp, book_ramp, book_extra):
    figure, ax = plt.subplots(figsize=(9, 4.6))
    for ramp, label, color in ((browse_ramp, t("read_only"), BLUE),
                               (book_ramp, t("write_book"), GREEN)):
        levels = [item["users"] for item in ramp["levels"]]
        rps = [item["summary"]["rps"] for item in ramp["levels"]]
        if label.startswith("S-book") and book_extra:
            levels.append(book_extra[0])
            rps.append(book_extra[1]["rps"])
        order = sorted(range(len(levels)), key=lambda index: levels[index])
        ax.plot([levels[i] for i in order], [rps[i] for i in order], marker="o",
                color=color, linewidth=2, label=label)
        for index in order:
            ax.annotate(f"{rps[index]:.1f}", (levels[index], rps[index]),
                        textcoords="offset points", xytext=(0, 8), ha="center",
                        fontsize=8, color=color)
    ax.set_xlabel(t("scaling_x"))
    ax.set_ylabel(t("scaling_y"))
    ax.set_title(t("scaling_title"))
    ax.legend(frameon=False)
    save(figure, output_dir, "chart_scaling_compare.png")


def main():
    global LANG
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default="loadtest/results")
    parser.add_argument("--output", required=True)
    parser.add_argument("--lang", choices=("zh", "en"), default="zh")
    args = parser.parse_args()
    LANG = args.lang
    Path(args.output).mkdir(parents=True, exist_ok=True)

    payloads = {
        "browse": load(args.results, "browse_20u.json"),
        "book": load(args.results, "book_15u.json"),
        "mixed": load(args.results, "mixed_20u.json"),
    }
    scenarios = [
        ("S-browse\n20 VU", payloads["browse"]),
        ("S-book\n15 VU", payloads["book"]),
        ("S-mixed\n20 VU", payloads["mixed"]),
    ]
    chart_latency_percentiles(args.output, scenarios)
    chart_ecdf(args.output, scenarios)
    chart_outcomes(args.output, scenarios)

    browse_ramp = load(args.results, "ramp_browse.json")
    book_ramp = load(args.results, "ramp_book.json")
    book_30 = load(args.results, "book_30u.json")["summary"]
    chart_ramp(
        args.output, browse_ramp,
        title=t("ramp_browse_title"),
        filename="chart_ramp_browse.png",
    )
    chart_ramp(
        args.output, book_ramp, single_level=(30, book_30),
        title=t("ramp_book_title"),
        filename="chart_ramp_book.png",
    )
    chart_scaling_compare(args.output, browse_ramp, book_ramp, (30, book_30))

    conflict = load(args.results, "conflict.json")
    chart_conflict(args.output, conflict)

    conflict_for_db = (t("conflict_label"), conflict)
    book_for_db = ("S-book\n15 VU", payloads["book"])
    browse_for_db = ("S-browse\n20 VU", payloads["browse"])
    mixed_for_db = ("S-mixed\n20 VU", payloads["mixed"])
    chart_db_contention(args.output, [browse_for_db, book_for_db, mixed_for_db, conflict_for_db])
    chart_timeline(args.output, payloads["mixed"])


if __name__ == "__main__":
    main()
