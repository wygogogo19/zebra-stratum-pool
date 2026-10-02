# Container image for the Stratum engine (ZCG milestone 3, container recipe).
#
# The engine is standard-library-only Python, so this image installs **nothing**: there is no pip step,
# no build toolchain and no lockfile to keep in sync. That is the whole point of the engine's design and
# it is why the image is small enough to audit by reading.
#
#   docker build -t zebra-stratum-pool:local .
#
# It is meant to run next to a zebrad container; see deploy/compose.yaml and docs/OPERATOR-RUNBOOK.md.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="zebra-stratum-pool" \
      org.opencontainers.image.description="Non-custodial solo mining for Zcash, built on zebrad" \
      org.opencontainers.image.source="https://github.com/wygogogo19/zebra-stratum-pool" \
      org.opencontainers.image.licenses="MIT"

# uid/gid 10001 deliberately matches the uid the official zfnd/zebra image drops privileges to. Both
# containers therefore read/write the shared RPC cookie directory as the same user, and neither has to
# run as root.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid "${APP_GID}" zecpool \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" \
            --home-dir /var/lib/zebra-stratum-pool --no-create-home \
            --shell /usr/sbin/nologin zecpool \
 && install -d -o "${APP_UID}" -g "${APP_GID}" /var/lib/zebra-stratum-pool

WORKDIR /app

# Only what the engine imports at runtime (`pool.py` pulls in build_coinbase / zcash_v6 and optionally
# equihash_verify); the test suite, docs and schemas stay out of the runtime image.
COPY pool.py build_coinbase.py zcash_v6.py equihash_verify.py pool_selftest_submit.py ./
COPY config.example.json /etc/zebra-stratum-pool/config.example.json

USER ${APP_UID}:${APP_GID}

# pool.py reads its config path from ZECPOOL_CONFIG (it takes no arguments).
ENV ZECPOOL_CONFIG=/etc/zebra-stratum-pool/config.json
ENV ZECPOOL_STATUS_FILE=/var/lib/zebra-stratum-pool/status.json

# Stratum. This is the only port the engine needs; the zebrad RPC stays on the compose network.
EXPOSE 3132

# Liveness = the engine is still writing its status snapshot. A pool that lost its node keeps serving
# miners from the last job, so "process is up" alone would not be an honest health signal.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python3 -c "import os,sys,time; p=os.environ['ZECPOOL_STATUS_FILE']; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 120 else 1)"

CMD ["python3", "/app/pool.py"]
