# syntax=docker/dockerfile:1
# vedun: REST API + dashboard + MCP server in one image, Docling included (PDF conversion runs inside it).
# Secrets are passed at run time only (environment), never baked into the image; see scripts/ for the launcher.

# ---------------------------------------------------------------- builder: locked deps, CPU-only torch
FROM python:3.15-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /build
COPY pyproject.toml uv.lock ./
# The lock was resolved on Windows and lists CUDA wheels (nvidia-*, triton) for Linux: about 3 GB of GPU libraries
# that a CPU-only laptop never uses. Export the locked versions, drop the CUDA packages, let uv pull torch from the
# PyTorch CPU index (--torch-backend=cpu) and release the torchvision pin (0.29.2 is not on that index).
RUN uv export --frozen --no-dev --all-extras --no-hashes --no-emit-project -o /tmp/req.txt \
 && grep -v -E '^(nvidia-|triton==)' /tmp/req.txt | sed -E 's/^torchvision==[^ ;]+/torchvision/' > /tmp/req.cpu.txt \
 && uv venv /opt/venv \
 && uv pip install --python /opt/venv/bin/python --torch-backend=cpu -r /tmp/req.cpu.txt

# ---------------------------------------------------------------- runtime
FROM python:3.15-slim AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin vedun \
 && mkdir -p /data/kb /data/pdf /hf /repo \
 && chown vedun:vedun /hf && chmod 0777 /hf
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY vedun/ /app/vedun/
# The root filesystem is read-only at run time (compose.yaml): HOME and caches go to the /tmp tmpfs; the writable places
# are the bind mounts (/data/kb, /data/pdf) and the HF model cache volume (/hf).
# /hf is world-writable: compose.release.yaml may run the service under the host uid (Docker on Linux) or 0 (Podman).
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    HF_HOME=/hf \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    VEDUN_ROOT=/data/kb \
    VEDUN_PDF_DIR=/data/pdf \
    VEDUN_STATE_DIR=/data/kb/.state-docker \
    VEDUN_REPO_DIR=/repo
USER vedun
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4).status == 200 else 1)"
# One worker on purpose: the job queue (single SQLite writer) lives in the process.
CMD ["uvicorn", "vedun.server.app:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "1", "--no-server-header"]
