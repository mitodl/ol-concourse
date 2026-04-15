FROM python:3.13-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY dist/ dist/
# The build workflow produces exactly one wheel; the glob is unambiguous.
# If this ever changes, clean dist/ before building to prevent installing
# unintended packages.
RUN pip install --upgrade pip && \
    pip install dist/*.whl "bumpver==2025.1131"
