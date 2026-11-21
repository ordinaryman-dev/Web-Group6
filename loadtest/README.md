# HTTP 并发 / 压力测试工具（loadtest）

本目录是独立于应用代码的黑盒 HTTP 测试工具，对应 M2 测试报告中的
**L6 并发冲突层** 与 **L7 压力层**。所有虚拟用户都走真实浏览器流程：
登录表单、CSRF、搜索、确认页、提交预订；不直接调用 Python 服务函数。

## 目录

```text
loadtest/
├── config.py                 被测地址、数据库、账号与冲突夹具参数
├── serve_app.py              本地压测服务入口（仅放大连接池，不改应用代码）
├── run_conflict.py           同房抢订 + 幂等重放 + 假售罄探针
├── run_stress.py             S-browse / S-book / S-mixed 单场景压测
├── run_ramp.py               逐级增加虚拟用户的阶梯压测
├── run_seed_check.py         F1 种子数据回归检查（9 条）
├── run_dbcheck.py            I1–I15 一致性 SQL 独立检查
├── lib/
│   ├── http.py               每个 VU 一个 Session；登录、CSRF、搜索、下单
│   ├── metrics.py            线程安全采样、分位数、CSV/JSON 输出
│   ├── seed.py               虚拟账号、冲突夹具、数据清理
│   └── dbcheck.py            一致性 SQL 与 MySQL 状态计数器
├── scenarios/
│   ├── conflict.py           Barrier 同步的两客户端 / 20 客户端抢房
│   └── stress.py             浏览、预订、混合三类虚拟用户循环
├── tools/
│   └── make_charts.py        把结果 JSON 渲染成报告图表
└── results/                  原始 CSV / JSON、pytest 基线与 SQL 证据
```

## 前置条件

1. MySQL 8 已启动，并有一个**专用测试库**（不要指向 `hero` 开发库）。
2. 应用依赖已安装，数据库已迁移并写入演示库存：

   ```powershell
   $env:SECRET_KEY = "loadtest-secret"
   $env:DATABASE_URL = "mysql+pymysql://hero_app:HeroApp2026@127.0.0.1:3306/hero_test_20261116?charset=utf8mb4"
   python -m flask --app app db upgrade
   python -m flask --app app seed-demo
   python -m flask --app app seed-rooms
   python -m flask --app app seed-desk
   ```

3. 启动被测服务（5001 端口，线程数由 Werkzeug 的 `threaded=True` 提供，
   连接池为 20 + 40 overflow）：

   ```powershell
   $env:SECRET_KEY = "loadtest-secret"
   $env:DATABASE_URL = "mysql+pymysql://hero_app:HeroApp2026@127.0.0.1:3306/hero_test_20261116?charset=utf8mb4"
   python -m loadtest.serve_app
   ```

4. 测试账号由脚本自动创建：
   - 预订/冲突账号：`vu001@load.test` …（密码 `Load2026!`）
   - 幂等账号：`idem001@load.test`
   - 截图用经理：`manager@load.test`（密码 `Manager2026!`）
   - 前台演示账号来自 `seed-desk`：`desk@example.test / Desk2026!`

## 运行

```powershell
# 同房冲突：4 轮双客户端 + 3 轮 20 客户端 + 10 路幂等重放
python -m loadtest.run_conflict --two-clients 4 --heavy-clients 20 --heavy-rounds 3

# 单场景压力
python -m loadtest.run_stress --scenario browse --users 20 --duration 45
python -m loadtest.run_stress --scenario book   --users 15 --duration 60
python -m loadtest.run_stress --scenario mixed  --users 20 --duration 60

# 阶梯压测
python -m loadtest.run_ramp --scenario browse --users 1,5,10,20,40 --duration 20
python -m loadtest.run_ramp --scenario book   --users 1,5,10,20 --duration 20

# 只跑一致性检查 / 种子检查
python -m loadtest.run_dbcheck
python -m loadtest.run_seed_check

# 生成报告图表
python loadtest/tools/make_charts.py --results loadtest/results --output <输出目录>
```

每次 `run_stress` / `run_ramp` 默认先清理上一轮由 `*@load.test` 账号创建的订单，
保证同一测试库可以重复跑。`run_conflict` 会创建只含一间房的 `lt-conflict` 房型，
用于把并发集中到一行 `rooms` 记录；测试完成后可用
`python -c "from loadtest.lib import seed; seed.remove_conflict_fixture()"`
删除该夹具，避免影响 293 间的演示界面。

## 设计要点

- **每个 VU 独立登录**：避免共享 session 把并发变成串行；登录时解析真实
  CSRF token，不关闭 CSRF 校验。
- **每个请求独立幂等键**：确认页每次生成新的 `idempotency_key`；压测不会因为
  重复键只创建一单而失效。幂等测试则故意复用同一个键。
- **Barrier 同步**：冲突轮的确认页在栅栏前全部准备好，POST 在同一瞬间释放，
  测的是数据库锁而不是 HTTP 排队。
- **结果分类**：`success` / `sold_out` / `busy` / `price_changed` /
  `idempotency_mismatch` / `http_error`，避免把业务拒绝误计为服务错误。
- **假售罄探针**：抢光日期返回 0 间可售的同时，另一个空闲日期必须仍返回 1 间，
  证明售罄是真实库存而不是全局误判。
- **一致性 SQL 用独立连接**：不复用应用 Session，直接对 MySQL 检查超售、
  重叠订单、房晚数、金额、封锁冲突、幂等重复、房型错配与同名房型等 15 条不变量。
- **F1 回归**：`run_seed_check.py` 额外检查演示种子数据的 9 条性质；`seed-desk`
  现在复用实体房实际房型，重复执行会修正旧订单并清理无引用的重复房型。
- **假售罄探针**：抢光日期返回 0 间可售的同时，另一个空闲日期必须仍返回 1 间，
  证明售罄是真实库存而不是全局误判。

## 已知限制

- 被测对象是 `flask run` 的开发服务器（`threaded=True`），不是生产 WSGI
  服务器；吞吐上限主要反映单进程 Python 服务器，而不是 InnoDB。
- 压测机与应用同机运行，客户端线程和 MySQL 会争抢 CPU；绝对 RPS 只用于
  本次版本内部对比，不作为生产容量结论。
