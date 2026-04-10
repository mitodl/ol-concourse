FROM python:3.14-slim

# Bring in the uv binary from the official uv image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

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
