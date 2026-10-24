# push-core: Web Push subscription service + PWA. Spec: docs/CONTRACTS.md section 5.
# Base image: role stacks passes build.args.NODE_BASE from images.node_base (roles/stacks/templates/push-compose.yaml.j2).
# The default only serves manual builds and must equal that value (a test enforces this).
# No "# syntax=" directive: it would pull an unpinned frontend image on every build.
ARG NODE_BASE=node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914

FROM ${NODE_BASE} AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund

FROM ${NODE_BASE}
# Package managers are not needed at runtime; dropping them shrinks the attack surface.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
      /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack /opt/yarn-* \
      /usr/local/bin/yarn /usr/local/bin/yarnpkg
ENV NODE_ENV=production \
    PUSH_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8085
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY package.json ./
COPY src ./src
COPY public ./public
# Code stays root-owned and read-only; only /data (bind mount of homelab_push_dir) is written.
USER 10101:10101
EXPOSE 8085
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["node", "/app/src/healthcheck.js"]
CMD ["node", "/app/src/server.js"]
