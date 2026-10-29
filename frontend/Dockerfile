# 前端服务镜像 — 多阶段构建（面试可讲：构建产物与运行环境分离，镜像不含 node 运行时）
#
# 阶段 1（builder）：node 环境里执行 tsc + vite build，产出纯静态 dist
# 阶段 2（runtime）：nginx:alpine 托管 dist + 反代 /api 到 backend 容器
# 最终镜像只含 nginx + 静态文件，体积小、攻击面小

# ── 构建阶段 ──
FROM node:22-alpine AS builder
WORKDIR /build

# 先拷 lockfile 装依赖（利用构建缓存）
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ── 运行阶段 ──
FROM nginx:1.27-alpine
COPY deploy/nginx-docker.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /build/dist /usr/share/nginx/html
