# syntax=docker/dockerfile:1.7
FROM node:26-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
# Optional extra CA (corporate / egress proxies): docker build --secret id=extra_ca,src=ca.crt
RUN --mount=type=secret,id=extra_ca,required=false --mount=type=cache,target=/root/.npm \
    if [ -f /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:stable-alpine AS runtime
LABEL org.opencontainers.image.title="argus-council-web" \
      org.opencontainers.image.source="https://github.com/lordprime/Argus-Council" \
      org.opencontainers.image.licenses="MIT"
# Take the base image's OS security fixes at build time (it can lag behind Alpine), and drop curl:
# nothing here uses it (the healthcheck uses busybox wget), and libcurl goes with it.
USER root
RUN apk upgrade --no-cache && if apk info -e curl >/dev/null; then apk del --no-cache curl; fi
USER 101
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD wget -qO- http://127.0.0.1:8080/ >/dev/null || exit 1
