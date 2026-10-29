# 后端服务镜像 — 百货通智能客服（FastAPI + uvicorn 单 worker）
#
# 设计要点（面试可讲）：
# - 依赖层与代码层分开 COPY：requirements 不变时构建走缓存，改代码不重装依赖
# - 数据目录（chroma_db/uploads）由 compose 挂卷覆盖，容器删除数据不丢
# - 入口脚本先跑 alembic 迁移再启动，消除"先迁移后启动"的顺序坑

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

# 依赖层（利用构建缓存）
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

# 代码与入口脚本
COPY backend backend

# 数据目录（compose 挂卷后由卷提供持久化）
RUN mkdir -p /srv/app/backend/chroma_db /srv/app/backend/uploads

EXPOSE 8000

# 入口：迁移 → 启动（单 worker：向量库为进程内单例，不能多进程）
ENTRYPOINT ["sh", "/srv/app/backend/docker-entrypoint.sh"]
