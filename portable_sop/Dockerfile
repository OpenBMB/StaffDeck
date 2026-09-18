FROM python:3.11-slim

WORKDIR /app
# The sidecar imports StaffDeck's published SOP module. It intentionally does
# not copy or package a second transition engine.
COPY backend ./backend
COPY portable_sop/src ./portable_sop/src
# The Python base image already carries a compatible setuptools. Avoid pip's
# isolated build environment, which otherwise fetches setuptools before it can
# install the existing StaffDeck package. Clear inherited Desktop proxy values
# only for this dependency-install layer.
RUN env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
    pip install --no-cache-dir --no-build-isolation ./backend
ENV PYTHONPATH=/app/backend:/app/backend/src:/app/portable_sop/src

EXPOSE 8091
CMD ["python", "-m", "staffdeck_sop_runtime"]
