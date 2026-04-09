FROM python:3.13-slim

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt requirements.txt

RUN \
    python3 -m pip install --upgrade pip && \
    pip install -r requirements.txt --no-deps

WORKDIR /opt/resource/
COPY concourse.py ./concourse.py
RUN python3 -m concoursetools . -r concourse.py

ENTRYPOINT ["python3"]
