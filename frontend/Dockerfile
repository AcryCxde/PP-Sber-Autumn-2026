# syntax=docker/dockerfile:1
# Frontend: статические сборки Vite за nginx; /api и /ws проксируются в backend.
# Интерфейс CoreTeams из этого каталога (страницы и свободный чат #chat) — /,
# рабочее пространство web-product (контракт V2) и панель администратора — /product/.
# Контекст сборки — корень репозитория.

FROM node:20-bookworm-slim AS build

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# web-product требует Node 22.12+ и собирается pnpm по своему lockfile.
FROM node:22-bookworm-slim AS product

WORKDIR /app
RUN npm install -g pnpm@10
COPY web-product/package.json web-product/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web-product/ .
RUN pnpm build

FROM nginx:1.27-alpine

ENV BACKEND_URL=http://backend:8000

COPY frontend/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
COPY --from=product /app/dist /usr/share/nginx/html/product

EXPOSE 80
