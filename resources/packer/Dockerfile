ARG PACKER_VERSION=1.16.1

FROM python:3.14-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72 AS packer-download
ARG PACKER_VERSION
RUN apk add --no-cache curl && \
    curl -fsSL "https://releases.hashicorp.com/packer/${PACKER_VERSION}/packer_${PACKER_VERSION}_linux_amd64.zip" \
         -o /tmp/packer.zip && \
    unzip /tmp/packer.zip -d /usr/local/bin/ && \
    rm /tmp/packer.zip

FROM python:3.14-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

COPY --from=packer-download /usr/local/bin/packer /usr/local/bin/packer

# Install OpenSSH for Packer SSH communicator
RUN apk add --no-cache openssh-client

COPY dist/ dist/
RUN python3 -m pip install --upgrade pip && \
    pip install dist/*.whl

WORKDIR /opt/resource/
COPY concourse.py ./concourse.py
COPY packer.py ./packer.py
COPY io_utils.py ./io_utils.py

# Install Packer Amazon plugin and generate concoursetools asset scripts
RUN packer plugins install github.com/hashicorp/amazon
RUN python3 -m concoursetools assets . -r concourse.py

ENTRYPOINT ["python3"]
