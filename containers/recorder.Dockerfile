# docker.io/library/python:3.12-slim-bookworm, resolved 2026-11-01.
FROM docker.io/library/python@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src OPENBLAS_NUM_THREADS=1
WORKDIR /app
COPY requirements/recorder.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /app/requirements.txt
COPY src/sessionscribe/ /app/src/sessionscribe/
USER 65532:65532
# Control transport only; voice and processing remain gated by the runtime policy.
ENTRYPOINT ["python", "-m", "sessionscribe.cli"]
CMD ["bot", "--policy", "/config/bot.json"]
