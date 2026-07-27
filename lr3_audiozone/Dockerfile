ARG BUILD_FROM
FROM ${BUILD_FROM}

ARG BUILD_ARCH=aarch64
ENV DEBIAN_FRONTEND=noninteractive

# Icecast2 = streaming server. Liquidsoap = never-dropping Spotify source.
# ffmpeg = codecs. libasound2 = runtime dep of the librespot binary.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      icecast2 \
      liquidsoap \
      ffmpeg \
      jq \
      netcat-openbsd \
      ca-certificates \
      media-types \
      curl \
      libasound2 \
 && rm -rf /var/lib/apt/lists/*

# librespot (Spotify Connect) — not packaged in apt. Take the binary from the
# raspotify .deb (bundles librespot v0.8.0) for the target architecture.
RUN set -eu; \
    case "${BUILD_ARCH}" in \
      aarch64) DEB_ARCH=arm64 ;; \
      amd64)   DEB_ARCH=amd64 ;; \
      armv7|armhf) DEB_ARCH=armhf ;; \
      *) echo "Unsupported BUILD_ARCH: ${BUILD_ARCH}" >&2; exit 1 ;; \
    esac; \
    DEB="raspotify_0.48.2.librespot.v0.8.0-9c7d756_${DEB_ARCH}.deb"; \
    curl -fsSL -o /tmp/librespot.deb \
      "https://github.com/dtcooper/raspotify/releases/download/0.48.2/${DEB}"; \
    dpkg-deb -x /tmp/librespot.deb /tmp/rasp; \
    install -m 0755 "$(find /tmp/rasp -type f -name librespot | head -1)" /usr/local/bin/librespot; \
    rm -rf /tmp/librespot.deb /tmp/rasp; \
    librespot --version

# D-Bus + Avahi — the raspotify librespot uses the avahi zeroconf backend, so it
# needs a running avahi-daemon. python3 runs the SlimProto controller (lr3ctl).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      dbus \
      avahi-daemon \
      python3 \
 && rm -rf /var/lib/apt/lists/*

COPY run.sh /run.sh
COPY icecast.xml.tpl /etc/lr3/icecast.xml.tpl
COPY radio.liq.tpl /etc/lr3/radio.liq.tpl
COPY lr3ctl /opt/lr3ctl

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
