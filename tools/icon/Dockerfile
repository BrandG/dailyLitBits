FROM debian:12-slim
# librsvg rasterises, imagemagick assembles the .ico. Both live here rather
# than on the host: the droplet has neither, and this way the build needs
# nothing but Docker wherever it runs.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 librsvg2-bin imagemagick \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /work
