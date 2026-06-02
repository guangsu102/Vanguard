FROM python:3.11-slim

LABEL maintainer="XBoard Team"
LABEL description="XBoard Telegram Bot Matrix"

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# 复制项目代码
COPY . .

# 创建资源目录
RUN mkdir -p /app/assets/posters /app/logs

# 运行用户
RUN useradd -m -u 1000 botuser && \
    chown -R botuser:botuser /app

USER botuser

# 暴露端口
EXPOSE 8080

# 默认命令
CMD ["python", "-m", "src.main"]
