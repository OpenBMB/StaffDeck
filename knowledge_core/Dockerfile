FROM node:22-slim AS ui
WORKDIR /src/ui
COPY ui/package*.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

FROM python:3.12-slim AS wheel
WORKDIR /src
COPY pyproject.toml setup.py ./
COPY src/ ./src/
COPY --from=ui /src/src/knowledge_core/ui/ ./src/knowledge_core/ui/
RUN pip wheel --no-deps --wheel-dir /dist .

FROM python:3.12-slim
WORKDIR /opt/km
COPY --from=wheel /dist/ /opt/km/dist/
RUN pip install --no-cache-dir /opt/km/dist/*.whl
RUN mkdir /data
ENV KM_DATABASE_URL=sqlite:////data/knowledge.sqlite
EXPOSE 8787
CMD ["knowledge-core", "--host", "0.0.0.0"]
