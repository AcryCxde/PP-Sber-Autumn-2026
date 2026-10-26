FROM node:22-alpine

RUN apk add --no-cache git
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY server.js git-storage.js metadata.js ./
COPY public ./public

ENV PORT=4173 DATA_DIR=/var/lib/coreteam/web STORAGE_ROOT_PATH=/var/lib/coreteam/artifacts
EXPOSE 4173
CMD ["node", "server.js"]
