# Multi-stage build. The browser SDKs (ACS calling, speech) are devDependencies and
# only needed to bundle the SPA in the build stage; the runtime image installs prod
# deps only (express + Entra identity + openai), so it is much smaller and faster to
# push/pull and cold-start. Layer order maximises Docker cache hits on unchanged deps.

# ---- build stage: install everything, bundle the Vite SPA ----
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# ---- runtime stage: prod deps + server + built assets only ----
FROM node:20-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
COPY package*.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
COPY server.js nudge-engine.js toolapi.js teams.js graph.js ./
COPY public ./public
COPY data ./data
COPY --from=build /app/dist ./dist
ENV PORT=3000
EXPOSE 3000
CMD ["node", "server.js"]
