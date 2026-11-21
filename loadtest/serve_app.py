# 本地压测服务入口：使用与应用相同的 create_app，仅放大数据库连接池以匹配虚拟用户数。
"""Local server launcher used by the HTTP tests.

It is intentionally thin: the application factory is untouched. The only
difference from ``flask run`` is a larger SQLAlchemy pool so that 40 virtual
users do not queue on the default five connections.
"""

import os

from app import create_app


def main():
    port = int(os.environ.get("LOAD_PORT", "5001"))
    application = create_app({
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "pool_pre_ping": True,
            "pool_size": int(os.environ.get("LOAD_POOL_SIZE", "20")),
            "max_overflow": int(os.environ.get("LOAD_POOL_OVERFLOW", "40")),
            "pool_timeout": 30,
            "pool_recycle": 280,
        },
        "TESTING": False,
    })
    application.run(
        host=os.environ.get("LOAD_HOST", "127.0.0.1"),
        port=port,
        threaded=True,
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
