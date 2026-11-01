# Experimental receiver for explicitly authorized qualification, never selected by bot.
FROM docker.io/library/python@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS candidate
ENV PYTHONDONTWRITEBYTECODE=1 SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PY_CORD=2.8.1+g10a5e8cf13bd8db21f95baf62cf866056d1e1091
WORKDIR /build
COPY requirements/voice-build.txt /build/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /build/requirements.txt
COPY scripts/voice_candidate.py /build/voice_candidate.py
RUN python /build/voice_candidate.py --target /build/pycord \
    && python -m build --wheel --no-isolation --outdir /build/wheels /build/pycord

FROM docker.io/library/python@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src OPENBLAS_NUM_THREADS=1
RUN apt-get update \
    && apt-get install --no-install-recommends -y libopus0=1.3.1-3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements/voice-test.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /app/requirements.txt
COPY --from=candidate /build/wheels/ /tmp/wheels/
RUN python -m pip install --no-cache-dir --no-index --no-deps /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels \
    && python -c "from discord.voice.receive.reader import UDPKeepAlive; assert UDPKeepAlive.delay == 5"
COPY src/sessionscribe/ /app/src/sessionscribe/
USER 65532:65532
ENTRYPOINT ["python", "-m", "sessionscribe.cli"]
CMD ["voice-test", "--policy", "/config/voice-test.json"]
