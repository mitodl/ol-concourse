FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1

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
    pip install dist/*.whl "bump-my-version==1.3.0"
