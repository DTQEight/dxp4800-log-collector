import docker
from docker.errors import DockerException, NotFound
from app.config import Config
import logging

logger = logging.getLogger(__name__)


class DockerClient:
    """封装Docker API客户端，用于与绿联NAS上的Docker daemon交互"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_client()
        return cls._instance

    def _init_client(self):
        try:
            self.client = docker.DockerClient(base_url=Config.DOCKER_SOCKET)
            self.client.ping()
            logger.info("Docker API连接成功")
        except DockerException as e:
            logger.error(f"无法连接到Docker API: {e}")
            raise

    def list_running_containers(self):
        """获取所有运行中的容器（排除配置中的黑名单）"""
        containers = self.client.containers.list()
        result = []
        for c in containers:
            name = c.name.lstrip("/")
            if name in Config.EXCLUDE_CONTAINERS:
                continue
            result.append({
                "id": c.id,
                "name": name,
                "image": c.image.tags[0] if c.image.tags else "none",
                "status": c.status,
                "created": c.attrs.get("Created"),
            })
        return result

    def get_container_logs(self, container_id_or_name, tail=1000, since=None):
        """获取指定容器的日志"""
        try:
            c = self.client.containers.get(container_id_or_name)
            kwargs = {"tail": tail, "stream": False, "timestamps": True}
            if since:
                kwargs["since"] = since
            logs = c.logs(**kwargs)
            return logs.decode("utf-8", errors="replace") if isinstance(logs, bytes) else logs
        except NotFound:
            logger.warning(f"容器未找到: {container_id_or_name}")
            return None
        except Exception as e:
            logger.error(f"获取容器日志失败 {container_id_or_name}: {e}")
            return None

    def stream_container_logs(self, container_id_or_name, since=None):
        """流式获取容器日志（用于实时收集）"""
        try:
            c = self.client.containers.get(container_id_or_name)
            kwargs = {"stream": True, "timestamps": True}
            if since:
                kwargs["since"] = since
            return c.logs(**kwargs)
        except Exception as e:
            logger.error(f"建立日志流失败 {container_id_or_name}: {e}")
            return None
