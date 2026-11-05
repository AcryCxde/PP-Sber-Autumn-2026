# syntax=docker/dockerfile:1
# Frontend: статическая сборка Vite за nginx; /api и /ws проксируются в backend.
# Контекст сборки — каталог frontend.

FROM node:20-bookworm-slim AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine

ENV BACKEND_URL=http://backend:8000

COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
