FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd

RUN apk add --no-cache nodejs npm

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

COPY dist/ dist/
RUN python3 -m pip install --upgrade pip && \
    pip install dist/*.whl

WORKDIR /opt/resource/
COPY concourse.py ./concourse.py
RUN python3 -m concoursetools assets . -r concourse.py

ENTRYPOINT ["python3"]
