# 适合绿联DXP4800 (x86_64 / arm64 均可用，Python官方镜像多架构)
FROM python:3.11-slim

LABEL maintainer="dxp4800-log-collector"
LABEL description="绿联DXP4800 NAS Docker日志收集工具"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 时区
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 先拷贝依赖以利用构建缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 拷贝源码
COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/

EXPOSE 5000

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:5000/login',timeout=3).read(); sys.exit(0)" || exit 1

CMD ["python", "-m", "app.main"]
