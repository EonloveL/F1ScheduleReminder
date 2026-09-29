FROM python:3.11-slim

WORKDIR /app

# CJK 字体（评分图表 matplotlib 渲染中文需要）
# 国内构建可加镜像加速：sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
# 国内构建可加：-i https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app/

CMD ["python", "-u", "main.py"]
