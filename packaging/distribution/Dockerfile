FROM node:22-bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends python3 git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN corepack enable && python3 prepare.py --without-backend

FROM nginx:stable-alpine AS edge
COPY --from=build /app/edge-dist /usr/share/nginx/html

FROM python:3.11-slim-bookworm AS app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY --from=build /app /app
RUN pip install --no-cache-dir ./backend && python packaging/distribution/install_dependencies.py
RUN useradd --uid 10001 --create-home staffdeck && mkdir /data && chown staffdeck:staffdeck /data
USER staffdeck
ENV STAFFDECK_DISTRIBUTION_MANIFEST=/app/distribution/manifest.json STAFFDECK_INSTALL_ROOT=/app
ENV ULTRARAG_DATA_DIR=/data ULTRARAG_HOST=0.0.0.0 ULTRARAG_PORT=5173 STAFFDECK_HEADLESS=true
VOLUME ["/data"]
EXPOSE 5173
CMD ["python", "launch.py"]
