# Pinned receiver; runtime policy defaults disabled and qualification requires explicit scope.
FROM docker.io/library/python@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS candidate
RUN for package in apt libapt-pkg6.0; do \
    dpkg --compare-versions "$(dpkg-query -W -f='${Version}' "${package}")" ge 2.6.2 || exit 1; \
    done
ENV PYTHONDONTWRITEBYTECODE=1 SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PY_CORD=2.8.1+g10a5e8cf13bd8db21f95baf62cf866056d1e1091
WORKDIR /build
COPY requirements/voice-build.txt /build/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /build/requirements.txt
COPY scripts/voice_candidate.py /build/voice_candidate.py
RUN python /build/voice_candidate.py --target /build/pycord \
    && python -m build --wheel --no-isolation --outdir /build/wheels /build/pycord

FROM docker.io/library/python@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
RUN for package in apt libapt-pkg6.0; do \
    dpkg --compare-versions "$(dpkg-query -W -f='${Version}' "${package}")" ge 2.6.2 || exit 1; \
    done
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src OPENBLAS_NUM_THREADS=1
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=0 -o APT::Update::Error-Mode=any update \
    && apt-get -o Acquire::Retries=0 install --no-install-recommends -y libopus0=1.3.1-3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements/voice-test.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /app/requirements.txt
COPY --from=candidate /build/wheels/ /tmp/wheels/
RUN python -m pip install --no-cache-dir --no-index --no-deps /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels \
    && python -m pip uninstall -y pip setuptools wheel \
    && python -c "from discord.voice.receive.reader import UDPKeepAlive; assert UDPKeepAlive.delay == 5"
COPY src/sessionscribe/ /app/src/sessionscribe/
USER 65532:65532
ENTRYPOINT ["python", "-m", "sessionscribe.cli"]
CMD ["bot", "--policy", "/config/bot.json"]
