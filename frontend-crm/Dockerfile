# frontend-crm/Dockerfile — static cockpit served by unprivileged nginx on 8080.
ARG BASE_REGISTRY=
FROM ${BASE_REGISTRY}nginxinc/nginx-unprivileged:1.27-alpine
USER root
COPY frontend-crm/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY frontend-crm/nginx/10-inject-config.sh /docker-entrypoint.d/10-inject-config.sh
COPY frontend-crm/html /usr/share/nginx/html
RUN chmod +x /docker-entrypoint.d/10-inject-config.sh && chown -R nginx:nginx /usr/share/nginx/html
USER nginx
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://localhost:8080/healthz || exit 1
