import io
import csv
import json
import os
import time
import threading
import logging
from functools import wraps
from flask import (
    Flask, render_template, request, jsonify, Response, abort,
    send_from_directory, session, redirect, url_for, stream_with_context
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

    # ---------- 收集器句柄（Web API 能 request_immediate_flush / request_immediate_collect） ----------
    # 由 app/main.py 启动时把 collector 实例通过 set_collector_hook 注入进来
    _collector_ref = []

    def set_collector_hook(c):
        _collector_ref[:] = [c]
        app._collector_weakref_hook = c
    app.config["SET_COLLECTOR_HOOK"] = set_collector_hook

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

    # ---------- API: 实时日志 tail ----------
    @app.route("/api/containers/<cid_or_name>/tail")
    @login_required
    def api_tail(cid_or_name):
        n = request.args.get("n", type=int, default=200)
        n = min(max(1, n), 20000)
        use_sse = request.args.get("stream", default="0") in ("1", "true", "True")
        if use_sse and Config.STREAM_ENABLED:
            return _tail_sse(cid_or_name, n)
        text = DockerClient().get_container_logs(cid_or_name, tail=n) or ""
        # /tail 的响应也顺手返回 X-Collect-Now，让前端知道"我刚刚触发了收集"
        headers = {}
        try:
            app._collector_weakref_hook and app._collector_weakref_hook.request_immediate_flush()
        except Exception:
            pass
        return Response(text, mimetype="text/plain; charset=utf-8", headers=headers)

    def _tail_sse(cid_or_name: str, n: int) -> Response:
        """真实 SSE tail -f：有新日志就推事件，不推则 15s 心跳。省轮询开销。"""
        def gen():
            # 先吐一次历史 n 行
            hist = DockerClient().get_container_logs(cid_or_name, tail=n) or ""
            yield f"event: lines\ndata: {json.dumps({'lines': hist.splitlines()})}\n\n"
            # 再起流
            last_ts = int(time.time())
            stream = DockerClient().stream_container_logs(cid_or_name, since=last_ts)
            if stream is None:
                yield "event: error\ndata: stream_closed\n\n"
                return
            buf: list[str] = []
            last_flush = time.monotonic()
            try:
                for line in stream:
                    if not line:
                        continue
                    buf.append(line)
                    now = time.monotonic()
                    # 聚 500ms 再推一次事件，避免每一行都推，前端主线程崩
                    if len(buf) >= 50 or (now - last_flush) >= 0.5:
                        yield f"event: lines\ndata: {json.dumps({'lines': buf})}\n\n"
                        buf.clear()
                        last_flush = now
                if buf:
                    yield f"event: lines\ndata: {json.dumps({'lines': buf})}\n\n"
            except GeneratorExit:
                return
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'err': str(e)})}\n\n"
            finally:
                yield "event: end\ndata: {}\n\n"

        return Response(
            stream_with_context(gen()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    # ---------- API: 强制触发一次"全量增量拉取 + 立即刷盘"（解决"前端看一分钟才更新"的终极兜底） ----------
    @app.route("/api/collect_now", methods=["POST"])
    @login_required
    def api_collect_now():
        ok = False
        c = _collector_ref[0] if _collector_ref else None
        if c is not None:
            try:
                c._poke_flush_event.set()
                # 后台线程立刻独立跑一轮 _collect_once，不阻塞这个 HTTP
                threading.Thread(target=c._collect_once, daemon=True, name="collect-now").start()
                ok = True
            except Exception as e:
                logger.error(f"collect_now 触发失败: {e}")
        return jsonify({"ok": ok})

    return app
