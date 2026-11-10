# FlowOps / 云策后端镜像
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5409
# P0-5：就绪探针用 /health/ready（依赖可用才 200），而不是 /health（仅进程活着）
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5409/health/ready',timeout=4).status==200 else 1)"
CMD ["python", "sau_backend.py"]
