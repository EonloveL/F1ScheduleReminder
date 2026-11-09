FROM python:3.11-slim

WORKDIR /app

# CJK 字体（评分图表 matplotlib 渲染中文需要）
# 国内构建可加镜像加速：sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv 用于依赖锁定（可复现构建）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 先复制依赖描述并锁定安装，利用 Docker 缓存层
COPY pyproject.toml uv.lock /app/
# OSS 默认镜像装全平台依赖（QQ + Discord 共用 common），便于同一份镜像跑不同入口
RUN uv sync --locked --extra qq --extra discord --no-dev --no-install-project

# 创建非 root 运行用户，确保数据/导出目录可写
RUN useradd -m -u 1000 bot && mkdir -p /app/data/exports && chown -R bot:bot /app

# 复制应用代码并完成 editable 安装
COPY . /app/
RUN uv sync --locked --extra qq --extra discord --no-dev

ENV PATH="/app/.venv/bin:$PATH"
USER bot

CMD ["python", "-u", "main.py"]
