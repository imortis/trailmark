# Trailmark live demo: HydraDB OSS and Trailmark in one container.
#
# HydraDB is the open-source engine (github.com/hydra-db/hydradb, Elastic
# License 2.0), built with `cargo build --release --features server-runtime
# --bin graph-node` at commit af6344037bffb2a354bf32c2b312ba4921a1583f on
# Ubuntu 26.04. It listens on localhost only; the public port serves
# Trailmark's demo page, never the database.
FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgraphblas7 libgomp1 ca-certificates git python3 python3-venv curl \
 && rm -rf /var/lib/apt/lists/*

# The demo repository: Reef (Apache-2.0), created after the models' training
# cutoff. Pinned: HEAD is the newest task, BASE is the snapshot the graph is
# built from (200 commits earlier).
ARG REEF_HEAD=0d723262abb67fe5f7747a4187f9865e7a358af3
ARG REEF_BASE=93bf204bdcc797a91991a6786faec827daad8fc7
RUN git clone --quiet https://github.com/Human-Agent-Society/reef.git /data/reef \
 && git -C /data/reef checkout --quiet ${REEF_HEAD} \
 && git -C /data/reef worktree add --quiet /data/reef-base ${REEF_BASE} \
 && echo ${REEF_BASE} > /data/reef-base.rev

COPY hydradb/graph-node /opt/hydradb/graph-node
COPY hydradb/LICENSE /opt/hydradb/LICENSE
COPY app /app
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir /app \
 && chmod +x /opt/hydradb/graph-node /app/start.sh \
 && mkdir -p /tmp/hydradb \
 && chown -R 1000:1000 /app /data /tmp/hydradb

USER 1000
WORKDIR /app
ENV PATH=/opt/venv/bin:$PATH HOME=/tmp
EXPOSE 7860
CMD ["/app/start.sh"]
