# The base image is passed in by compose from images.node_base (inventory/group_vars/all/versions.yml),
# so no tag lives here. Building without it fails on purpose.
ARG NODE_BASE
FROM ${NODE_BASE}
WORKDIR /app
COPY relay.js .
ENV NODE_ENV=production
USER node
CMD ["node", "relay.js"]
