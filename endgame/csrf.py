# -*- coding: utf-8 -*-
"""本地 CSRF 防护: 进程内随机令牌, 写请求必须带着它。

为什么这样够用:
    * 服务只监听 127.0.0.1, 令牌在进程启动时随机生成, 页面用
      GET /api/studies/csrf 取一次(前端存起来, 之后每个写请求塞请求头);
    * 跨站页面即使能让浏览器发请求, 也读不到这个令牌(同源策略), 于是过不了
      POST/PUT/PATCH/DELETE 的校验;
    * 令牌只用来证明"请求来自本项目页面", 不做用户鉴权 —— 本地单人使用,
      没有账号体系, 也不该假装有。

令牌的传法(任选其一, 先看请求头):
    X-CSRF-Token: <token>
    JSON 体里的 {"csrfToken": "<token>", ...}

不校验的方法: GET / HEAD / OPTIONS(它们本来就不该改数据)。
"""

import hmac
import secrets
from functools import wraps

from flask import jsonify, request

TOKEN_HEADER = "X-CSRF-Token"
BODY_FIELD = "csrfToken"
EXEMPT_METHODS = ("GET", "HEAD", "OPTIONS")

# 进程内单例: 重启服务 = 换令牌(前端重新取一次即可)
_SECRET = secrets.token_urlsafe(32)


def current_token() -> str:
    """当前令牌 (GET /api/studies/csrf 回给前端)"""
    return _SECRET


def token_from(req=None):
    """从请求里取令牌, 取不到返回 None"""
    req = req or request
    header = req.headers.get(TOKEN_HEADER)
    if header and header.strip():
        return header.strip()
    payload = req.get_json(silent=True)
    if isinstance(payload, dict):
        value = payload.get(BODY_FIELD)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def is_valid(value) -> bool:
    """常数时间比较, 免得靠响应时间把令牌试出来"""
    return bool(value) and hmac.compare_digest(str(value), _SECRET)


def check(req=None) -> bool:
    """这个请求能不能放行"""
    req = req or request
    if req.method in EXEMPT_METHODS:
        return True
    return is_valid(token_from(req))


def error_response():
    """统一的 403 响应 (与 games_routes 的 ok/error 约定保持一致)"""
    detail = {"header": TOKEN_HEADER, "bodyField": BODY_FIELD,
              "hint": "先 GET /api/studies/csrf 取令牌, 再在写请求里带上"}
    message = "缺少或错误的 CSRF 令牌"
    return jsonify({"ok": False, "code": "csrf_failed", "error": message,
                    "message": message, "detail": detail}), 403


def protect(view):
    """装饰器: 给写接口加 CSRF 校验"""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not check():
            return error_response()
        return view(*args, **kwargs)
    return wrapper
