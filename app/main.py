"""
入口：启动Web服务 + 后台日志收集器 + 定时清理
用法: python -m app.main
"""
import threading
import logging
import signal
import sys
from apscheduler.schedulers.background import BackgroundScheduler

from app.config import Config
from app.web import create_app
from app.collector import LogCollector
from app.storage import LogStorage
from app import models


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    setup_logging()
    models.init_db()

    collector = LogCollector()
    collector_thread = threading.Thread(target=collector.run_foreground, daemon=True)
    collector_thread.start()

    # 定时清理旧日志
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        lambda: (
            models.cleanup_old_logs(Config.LOG_RETENTION_DAYS),
            LogStorage.cleanup_expired_files(Config.LOG_RETENTION_DAYS),
        ),
        "interval",
        hours=6,
        id="cleanup-logs",
    )
    scheduler.start()

    app = create_app()
    # 把 collector 实例注入到 Flask 里，API /collect_now 能立刻触发拉取+刷盘
    try:
        app.config.get("SET_COLLECTOR_HOOK", lambda c: None)(collector)
    except Exception as e:
        logging.warning(f"inject collector hook failed: {e}")

    def _graceful(signum, frame):
        logging.info("收到退出信号，优雅关闭...")
        collector.stop()
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGTERM, _graceful)

    logging.info(f"Web服务启动: http://{Config.WEB_HOST}:{Config.WEB_PORT}")
    app.run(host=Config.WEB_HOST, port=Config.WEB_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
