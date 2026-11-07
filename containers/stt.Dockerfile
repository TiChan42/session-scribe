FROM docker.io/library/python@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
RUN for package in apt libapt-pkg6.0; do \
    dpkg --compare-versions "$(dpkg-query -W -f='${Version}' "${package}")" ge 2.6.2 || exit 1; \
    done
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=2 HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
WORKDIR /app
COPY requirements/stt.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /app/requirements.txt \
    && python -m pip uninstall -y pip setuptools wheel
COPY src/sessionscribe/ /app/src/sessionscribe/
USER 65532:65532
ENTRYPOINT ["python", "-m", "sessionscribe.cli"]
CMD ["stt-worker"]
