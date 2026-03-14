# Production image: uses the official Pulumi Python image which includes the
# Pulumi CLI, Python runtime, and common plugins pre-installed.
FROM pulumi/pulumi-python:3.226.0@sha256:79e6109af8da1457c40887ab42382907b480ab75ddc3656dd3eaa6b94c337a5a

ENV PYTHONUNBUFFERED=1
WORKDIR /opt/resource/

# Install kubectl
RUN curl -fsSL "https://storage.googleapis.com/kubernetes-release/release/$(curl -s https://storage.googleapis.com/kubernetes-release/release/stable.txt)/bin/linux/amd64/kubectl" \
    -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl

COPY requirements.txt requirements.txt
RUN python3 -m pip install --upgrade pip && \
    pip install -r requirements.txt --no-deps

COPY concourse.py ./concourse.py
COPY pulumi_utils.py ./pulumi_utils.py
COPY io_utils.py ./io_utils.py
RUN python3 -m concoursetools . -r concourse.py

ENTRYPOINT ["python3"]
