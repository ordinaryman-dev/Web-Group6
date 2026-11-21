# 三个角色共用的改密码表单：旧密码必填、新密码为 8～128 字符、两次新密码必须一致；身份校验由后端完成。
from flask_wtf import FlaskForm
from wtforms import PasswordField, SubmitField
from wtforms.validators import EqualTo, InputRequired, Length


class PasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[InputRequired(), Length(max=128)])
    new_password = PasswordField("New password", validators=[InputRequired(), Length(min=8, max=128)])
    confirm = PasswordField("Confirm new password", validators=[InputRequired(), EqualTo("new_password", message="Passwords must match.")])
    submit = SubmitField("Change password")
