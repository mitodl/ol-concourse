FROM python:3.14-slim@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151

# Bring in the uv binary from the official uv image
COPY --from=ghcr.io/astral-sh/uv:latest@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /uvx /usr/local/bin/

# Install curl and the Pulumi CLI
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && curl -fsSL https://get.pulumi.com | sh \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.pulumi/bin:$PATH"

# Build the Python environment
WORKDIR /app
COPY dist/ ./dist/
RUN uv venv --relocatable /app/.venv && \
    uv pip install dist/*.whl

ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy the resource module and generate Concourse asset scripts under /opt/resource/
WORKDIR /opt/resource/
COPY concourse.py ./concourse.py
RUN python3 -m concoursetools assets . -r concourse.py

ENTRYPOINT ["python3"]
