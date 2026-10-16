FROM python:3.11-slim

WORKDIR /app
COPY portable_sop ./portable_sop
RUN env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy pip install --no-cache-dir ./portable_sop
ENV PYTHONPATH=/app/portable_sop/src
ENV SOP_RUNTIME_DB=/data/sop-runtime.sqlite
VOLUME ["/data"]

EXPOSE 8091
CMD ["python", "-m", "staffdeck_sop_runtime"]
