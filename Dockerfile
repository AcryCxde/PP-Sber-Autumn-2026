FROM alpine:latest
RUN apk add --no-cache bash curl git
WORKDIR /workspace
# Точка входа для агента
CMD ["echo", "Agent container started"]