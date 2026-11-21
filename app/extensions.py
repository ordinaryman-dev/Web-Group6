# 共用扩展对象：先创建 db、登录、迁移和 CSRF 工具，再由 create_app 绑定到应用，避免互相导入。
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect


db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "info"
csrf = CSRFProtect()
