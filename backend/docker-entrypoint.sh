#!/bin/sh
# 容器入口：先执行数据库迁移，再启动服务。
# 迁移先行的原因：应用启动时的 create_all 会建表但不盖 alembic 版本章，
# 若先启动后迁移，迁移会因表已存在而失败；这里保证顺序永远正确。
set -e

cd /srv/app/backend

echo ">>> 执行数据库迁移 (alembic upgrade head)..."
alembic upgrade head

echo ">>> 启动服务 (uvicorn, 单 worker)..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
