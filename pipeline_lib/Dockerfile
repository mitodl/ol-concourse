FROM python:3.14-slim@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97

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
