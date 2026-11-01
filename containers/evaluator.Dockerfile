FROM docker.io/library/python@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements/core.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /app/requirements.txt
COPY src/sessionscribe/ /app/src/sessionscribe/
USER 65532:65532
ENTRYPOINT ["python", "-m", "sessionscribe.cli"]
CMD ["evaluate-worker"]
