# 独立于应用代码的 HTTP 并发/压力测试工具包；只通过公开页面与 JSON 接口访问被测系统。
"""Black-box HTTP test harness for the Hero Hotel project.

The package talks to a running Flask instance over HTTP. It never imports the
application except in :mod:`loadtest.serve_app`, which is only a local server
launcher with a larger database pool for the load runs.
"""
