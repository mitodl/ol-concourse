FROM python:3.14-slim@sha256:bc389f7dfcb21413e72a28f491985326994795e34d2b86c8ae2f417b4e7818aa

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY dist/ dist/
RUN python3 -m pip install --upgrade pip && \
    pip install dist/*.whl

WORKDIR /opt/resource/
COPY concourse.py ./concourse.py
RUN python3 -m concoursetools assets . -r concourse.py

ENTRYPOINT ["python3"]
