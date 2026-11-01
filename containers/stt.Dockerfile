FROM docker.io/library/python@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=2 HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
WORKDIR /app
COPY requirements/stt.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /app/requirements.txt
COPY src/sessionscribe/ /app/src/sessionscribe/
USER 65532:65532
ENTRYPOINT ["python", "-m", "sessionscribe.cli"]
CMD ["stt-worker"]
