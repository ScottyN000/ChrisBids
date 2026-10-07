# One image for the whole pipeline: the CLI, the golden tests and the readers
# that arrive in Phase 2. poppler-utils is the only system dependency, and it is
# what makes page images and page text possible without a model call.
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pipeline/ ./pipeline/
COPY tools/ ./tools/
COPY fixtures/ ./fixtures/
COPY tests/ ./tests/

# A packet is a folder and a bid is a run (architecture p.17): mount the packet
# read-only at /packet and let runs land in /runs.
VOLUME ["/packet", "/runs"]
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "pipeline"]
CMD ["verify-fixtures", "--out", "/runs/verify"]
