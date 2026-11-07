# docker.io/library/python:3.12-slim-bookworm, resolved 2026-11-07.
FROM docker.io/library/python@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
RUN for package in apt libapt-pkg6.0; do \
    dpkg --compare-versions "$(dpkg-query -W -f='${Version}' "${package}")" ge 2.6.2 || exit 1; \
    done
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src OPENBLAS_NUM_THREADS=1
WORKDIR /app
COPY requirements/recorder.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /app/requirements.txt \
    && python -m pip uninstall -y pip setuptools wheel
COPY src/sessionscribe/ /app/src/sessionscribe/
USER 65532:65532
# Control transport only; voice and processing remain gated by the runtime policy.
ENTRYPOINT ["python", "-m", "sessionscribe.cli"]
CMD ["bot", "--policy", "/config/bot.json"]
