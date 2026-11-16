FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /uvx /bin/

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
    pip install dist/*.whl "bump-my-version==1.5.1"
