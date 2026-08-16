import io
import csv
import json
import logging
from functools import wraps
from flask import (
    Flask, render_template, request, jsonify, Response, abort,
    send_from_directory, session, redirect, url_for
)
from app.config import Config
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
    app.secret_key = "dxp4800-log-secret-key"

    # ---------- 登录鉴权 ----------
    def login_required(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return wrapper

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            u = request.form.get("username")
            p = request.form.get("password")
            if u == Config.WEB_USERNAME and p == Config.WEB_PASSWORD:
                session["logged_in"] = True
                return redirect(url_for("index"))
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
            live = DockerClient().list_running_containers()
            live_ids = {c["id"] for c in live}
            db = models.list_containers()
            # 合并：加上DB中已停过的历史容器
            seen = live_ids.copy()
            for c in db:
                if c["id"] not in seen:
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
        import os
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
        # 默认只读最后5000行（预览不卡死）；传 tail=0 再传 full=1 才拿全文
        full = request.args.get("full", default="0") in ("1", "true", "True")
        if tail == 0 and not full:
            tail = 5000
        content = LogStorage.read_log_file(container_name, filename, tail=tail)
        if fmt == "download":
            return Response(
                content,
                mimetype="text/plain",
                headers={"Content-Disposition": f"attachment; filename={container_name}-{filename}"},
            )
        return Response(content, mimetype="text/plain; charset=utf-8")

    # ---------- API: 数据库搜索 ----------
    @app.route("/api/logs/search")
    @login_required
    def api_search_logs():
        params = {
            "container_id": request.args.get("container_id"),
            "container_name": request.args.get("container_name"),
            "keyword": request.args.get("keyword"),
            "start_time": request.args.get("start_time"),
            "end_time": request.args.get("end_time"),
            "limit": request.args.get("limit", type=int, default=500),
            "offset": request.args.get("offset", type=int, default=0),
        }
        rows = models.search_logs(**params)
        fmt = request.args.get("format", "json")
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["timestamp", "container_name", "source", "content"])
            for r in rows:
                w.writerow([r.get("timestamp"), r.get("container_name"), r.get("source"), r.get("content")])
            return Response(
                buf.getvalue(),
                mimetype="text/csv",
                headers={"Content-Disposition": "attachment; filename=logs.csv"},
            )
        if fmt == "json_download":
            return Response(
                json.dumps(rows, ensure_ascii=False, indent=2),
                mimetype="application/json",
                headers={"Content-Disposition": "attachment; filename=logs.json"},
            )
        return jsonify(rows)

    # ---------- API: 实时日志 tail ----------
    @app.route("/api/containers/<cid_or_name>/tail")
    @login_required
    def api_tail(cid_or_name):
        n = request.args.get("n", type=int, default=200)
        # 防止一次性拉太多把 NAS Python 进程撑爆
        n = min(max(1, n), 20000)
        text = DockerClient().get_container_logs(cid_or_name, tail=n) or ""
        return Response(text, mimetype="text/plain; charset=utf-8")

    return app
