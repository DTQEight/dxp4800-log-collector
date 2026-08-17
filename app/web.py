import csv
import json
import os
import time
import secrets
import threading
import logging
from functools import wraps
from datetime import timedelta
from flask import (
    Flask, render_template, request, jsonify, Response,
    session, redirect, url_for
)
from app.config import Config, _verify_password
from app.docker_client import DockerClient
from app.storage import LogStorage
from app import models

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )
    app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
    # Session 12 小时后自动过期
    app.permanent_session_lifetime = timedelta(hours=12)

    # ---------- 登录尝试限制（内存计数，防暴力破解） ----------
    _login_attempts: dict[str, list] = {}  # ip -> [count, first_attempt_ts]
    _LOGIN_MAX_ATTEMPTS = 5
    _LOGIN_WINDOW_SEC = 300  # 5 分钟窗口

    # ---------- 全局异常处理器：/api/* 路径永远返回 JSON，不返回 Flask 默认 HTML 500 页 ----------
    @app.errorhandler(Exception)
    def _global_exception_handler(e):
        # /api/ 前缀的请求一律 JSON 响应，避免前端把 HTML 错误页当 JSON 解析出 Unexpected token '<'
        if request.path.startswith("/api/"):
            logger.exception("API 未处理异常: path=%s", request.path)
            return jsonify({"error": str(e)}), 500
        # 页面路径走默认错误页
        raise e

    # ---------- 登录鉴权 ----------
    def login_required(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("logged_in"):
                # API 请求未登录返回 JSON 401，页面请求 302 到登录页
                if request.path.startswith("/api/"):
                    return jsonify({"error": "未登录或会话过期"}), 401
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return wrapper

    # ---------- 收集器句柄（Web API 能 request_immediate_flush / 触发即时收集） ----------
    # 由 app/main.py 启动时把 collector 实例通过 set_collector_hook 注入进来
    _collector_ref = []

    def set_collector_hook(c):
        _collector_ref[:] = [c]
        app._collector_weakref_hook = c
    app.config["SET_COLLECTOR_HOOK"] = set_collector_hook

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            ip = request.remote_addr or "unknown"
            now_ts = time.time()
            # 检查登录尝试限制
            attempts = _login_attempts.get(ip)
            if attempts:
                count, first_ts = attempts
                if now_ts - first_ts < _LOGIN_WINDOW_SEC and count >= _LOGIN_MAX_ATTEMPTS:
                    remain = int(_LOGIN_WINDOW_SEC - (now_ts - first_ts))
                    return render_template("login.html", error=f"登录失败次数过多，请 {remain} 秒后再试"), 429
                elif now_ts - first_ts >= _LOGIN_WINDOW_SEC:
                    # 窗口过期，重置
                    _login_attempts[ip] = [0, now_ts]

            u = request.form.get("username")
            p = request.form.get("password")
            if u == Config.WEB_USERNAME and _verify_password(p, Config.WEB_PASSWORD):
                session.permanent = True
                session["logged_in"] = True
                _login_attempts.pop(ip, None)
                return redirect(url_for("index"))
            # 记录失败
            if ip not in _login_attempts or now_ts - _login_attempts.get(ip, [0, now_ts])[1] >= _LOGIN_WINDOW_SEC:
                _login_attempts[ip] = [1, now_ts]
            else:
                _login_attempts[ip][0] += 1
            return render_template("login.html", error="用户名或密码错误")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---------- 页面 ----------
    @app.route("/")
    @login_required
    def index():
        return render_template("index.html")

    # ---------- API: 容器 ----------
    @app.route("/api/containers")
    @login_required
    def api_containers():
        """返回实时运行的容器列表（来自Docker API）"""
        try:
            live_all = DockerClient.get_instance().list_running_containers()
            # 过滤掉被排除的容器（默认排除自身 dxp4800-log-collector）
            live = [c for c in live_all if not c.get("exclude")]
            live_ids = {c["id"] for c in live}
            db = models.list_containers()
            # 合并：加上DB中已停过的历史容器（也排除掉 EXCLUDE_CONTAINERS）
            excludes = set(Config.EXCLUDE_CONTAINERS or [])
            seen = live_ids.copy()
            for c in db:
                if c["id"] in seen:
                    continue
                if c.get("name") in excludes:
                    continue
                live.append({
                    "id": c["id"],
                    "name": c["name"],
                    "image": c.get("image"),
                    "status": "exited",
                    "first_seen": c.get("first_seen"),
                    "last_seen": c.get("last_seen"),
                })
                seen.add(c["id"])
            return jsonify(live)
        except Exception as e:
            logger.exception("获取容器列表失败")
            return jsonify({"error": str(e)}), 500

    # ---------- API: 日志文件浏览 ----------
    @app.route("/api/containers/<container_name>/files")
    @login_required
    def api_container_files(container_name):
        files = LogStorage.list_container_files(container_name)
        # 顺带返回每个文件的大小，前端能提示"大文件建议只取最后N行"
        info = []
        for fn in files:
            fpath = os.path.join(Config.LOG_STORAGE_PATH, container_name, fn)
            size = 0
            try:
                size = os.path.getsize(fpath)
            except OSError:
                pass
            info.append({"name": fn, "size": size})
        return jsonify({"files": info})

    @app.route("/api/containers/<container_name>/files/<filename>")
    @login_required
    def api_read_file(container_name, filename):
        tail = request.args.get("tail", type=int, default=0)
        fmt = request.args.get("format", "text")
        full = request.args.get("full", default="0") in ("1", "true", "True")
        # ====== 增量追新：前端传 from_bytes 或 from_lines，只返回文件新增尾部 ======
        from_bytes = request.args.get("from_bytes", type=int, default=None)
        from_iso   = request.args.get("from_ts", type=str, default=None)

        base_dir = os.path.join(Config.LOG_STORAGE_PATH, container_name)
        fpath = os.path.join(base_dir, filename)
        if not os.path.isfile(fpath):
            # 文件不存在：可能日期翻到新一天了，给前端一个"换新文件"的信号
            if fmt == "download":
                return Response("", status=404)
            return Response("", status=404, mimetype="text/plain; charset=utf-8")

        if fmt == "download":
            # 下载永远下整篇（不受 tail/增量影响）
            with open(fpath, "rb") as f:
                content = f.read().decode("utf-8", errors="replace")
            return Response(
                content,
                mimetype="text/plain",
                headers={"Content-Disposition": f"attachment; filename={container_name}-{filename}"},
            )

        # ---------- 增量：from_bytes 优先（最省CPU/IO，不用扫描全文）----------
        if from_bytes is not None and from_bytes >= 0:
            try:
                size = os.path.getsize(fpath)
            except OSError: size = 0
            if from_bytes > size:
                # 文件被轮转/截断了：清空重来
                content = LogStorage.read_log_file(container_name, filename, tail=max(1, tail or 50))
                from_bytes_back = 0
            else:
                try:
                    with open(fpath, "rb") as f:
                        f.seek(from_bytes)
                        chunk = f.read()
                    content = chunk.decode("utf-8", errors="replace")
                    from_bytes_back = size
                except Exception as e:
                    logger.error(f"增量读失败 {fpath} @{from_bytes}: {e}")
                    content = LogStorage.read_log_file(container_name, filename, tail=max(1, tail or 50))
                    from_bytes_back = 0
            headers = {
                "X-Content-Bytes": str(from_bytes_back),
                "X-Content-NewFile": "0",
                "Cache-Control": "no-store",
            }
            return Response(content, mimetype="text/plain; charset=utf-8", headers=headers)

        # ---------- 整段预览：默认只给最后 5000 行 ----------
        if tail == 0 and not full:
            tail = 5000
        content = LogStorage.read_log_file(container_name, filename, tail=tail)
        size = 0
        try: size = os.path.getsize(fpath)
        except OSError: pass
        headers = {
            "X-Content-Bytes": str(size),
            "X-Content-NewFile": "1",   # 首次读当"新文件"，前端拿到 size 作为后续 from_bytes 起点
            "Cache-Control": "no-store",
        }
        return Response(content, mimetype="text/plain; charset=utf-8", headers=headers)

    # ---------- API: 运行时 Tab 临时拉 n 行预览（不写入日志文件） ----------
    @app.route("/api/containers/<cid_or_name>/tail")
    @login_required
    def api_tail(cid_or_name):
        n = request.args.get("n", type=int, default=200)
        n = min(max(1, n), 20000)
        # json-log 单通道模式：一律用 tail_runtime 从 Docker SDK 临时拉最新 n 行做预览
        dc = DockerClient.get_instance()
        cid = cid_or_name
        # 支持按 name 查找（前端点击容器时传的可能是 name 也可能是 cid）
        if len(cid_or_name) < 64:
            container = dc.get_container(cid_or_name)
            if container is None:
                # 按名字找不到 → 再按 ID 前缀找
                for live in dc.list_running_containers():
                    if live["name"] == cid_or_name or live["id"].startswith(cid_or_name):
                        cid = live["id"]
                        break
            else:
                cid = container.id
        lines = dc.tail_runtime(cid, n=n)
        text = "\n".join(lines)
        try:
            hook = getattr(app, "_collector_weakref_hook", None)
            if hook is not None:
                flush_fn = getattr(hook, "request_immediate_flush", None)
                if flush_fn is not None:
                    flush_fn()
        except Exception:
            pass
        return Response(text, mimetype="text/plain; charset=utf-8")

    # ---------- API: 强制触发一次"全量增量拉取 + 立即刷盘"（解决"前端看一分钟才更新"的终极兜底） ----------
    @app.route("/api/collect_now", methods=["POST"])
    @login_required
    def api_collect_now():
        ok = False
        msg = ""
        c = _collector_ref[0] if _collector_ref else None
        if c is None:
            msg = "收集器实例尚未注入（启动中？）"
        else:
            try:
                poke = getattr(c, "_poke_flush_event", None)
                if poke is not None:
                    poke.set()
                collect_fn = getattr(c, "_collect_once", None)
                if collect_fn is not None:
                    threading.Thread(target=collect_fn, daemon=True, name="collect-now").start()
                ok = True
            except Exception as e:
                logger.error(f"collect_now 触发失败: {e}")
                msg = str(e)
        return jsonify({"ok": ok, "msg": msg})

    # ---------- API: 企业微信通知测试 ----------
    @app.route("/api/wechat/test", methods=["POST"])
    @login_required
    def api_wechat_test():
        """手动触发一条测试通知，验证 corpid/corpsecret/agentid 配置是否正确。

        body 可选: {"message": "自定义测试内容"}；不传则发默认测试消息。
        """
        if not Config.WECHAT_WORK_ENABLED:
            return jsonify({"ok": False, "msg": "未启用企业微信通知 (WECHAT_WORK_ENABLED=false)"}), 400
        try:
            from app.wechat_work import send_wechat_message
            payload = request.get_json(silent=True) or {}
            custom = (payload.get("message") or "").strip()
            message = custom or (
                "【DXP4800 日志中心测试通知】\n"
                "这是一条来自 dxp4800-log-collector 的测试消息。\n"
                "如果你收到了，说明企业微信通知配置正确。"
            )
            ok, msg = send_wechat_message(message)
            return jsonify({"ok": ok, "msg": msg})
        except Exception as e:
            logger.exception("企业微信测试通知异常")
            return jsonify({"ok": False, "msg": str(e)}), 500

    @app.route("/api/wechat/status", methods=["GET"])
    @login_required
    def api_wechat_status():
        """返回企业微信通知的当前配置状态（不暴露 secret），供前端显示。"""
        return jsonify({
            "enabled": Config.WECHAT_WORK_ENABLED,
            "corpid_configured": bool(Config.WECHAT_WORK_CORPID),
            "corpsecret_configured": bool(Config.WECHAT_WORK_CORPSECRET),
            "agentid": Config.WECHAT_WORK_AGENTID,
            "to_user": Config.WECHAT_WORK_TOUSER,
            "error_keywords": Config.WECHAT_WORK_ERROR_KEYWORDS,
            "cooldown_sec": Config.WECHAT_WORK_COOLDOWN_SEC,
            "include_containers": Config.WECHAT_WORK_INCLUDE_CONTAINERS,
            "proxy_configured": bool(Config.WECHAT_WORK_PROXY_URL),
        })

    # ---------- API: 数据库日志检索 + 导出 ----------
    @app.route("/api/logs/search")
    @login_required
    def api_search_logs():
        container_id = request.args.get("container_id") or None
        container_name = request.args.get("container_name") or None
        keyword = request.args.get("keyword") or None
        start_time = request.args.get("start_time") or None
        end_time = request.args.get("end_time") or None
        fmt = request.args.get("format", "json")
        limit = request.args.get("limit", type=int, default=500)
        offset = request.args.get("offset", type=int, default=0)

        # 导出场景放宽上限
        if fmt in ("csv", "json_download"):
            limit = min(max(limit, 1), 50000)
        else:
            limit = min(max(limit, 1), 5000)

        rows = models.search_logs(
            container_id=container_id,
            container_name=container_name,
            keyword=keyword,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            offset=offset,
        )

        if fmt == "csv":
            import io as _io
            buf = _io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["timestamp", "container_name", "source", "content"])
            for r in rows:
                writer.writerow([
                    r.get("timestamp", ""),
                    r.get("container_name", ""),
                    r.get("source", ""),
                    r.get("content", ""),
                ])
            return Response(
                buf.getvalue(),
                mimetype="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=logs.csv"},
            )

        if fmt == "json_download":
            return Response(
                json.dumps(rows, ensure_ascii=False, indent=2),
                mimetype="application/json; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=logs.json"},
            )

        return jsonify(rows)

    # ---------- API: 运行时配置（UI 可调参数）----------
    @app.route("/api/config", methods=["GET"])
    @login_required
    def api_get_config():
        from app.config import get_ui_config
        return jsonify(get_ui_config())

    @app.route("/api/config", methods=["POST"])
    @login_required
    def api_save_config():
        from app.config import save_runtime_config
        payload = request.get_json(silent=True) or {}
        result = save_runtime_config(payload)
        return jsonify({"ok": True, **result})

    return app
