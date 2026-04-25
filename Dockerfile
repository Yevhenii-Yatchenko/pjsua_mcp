FROM python:3.13-bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    swig \
    libssl-dev \
    libasound2-dev \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# setuptools provides distutils (removed in Python 3.13)
RUN pip install --no-cache-dir setuptools

# Build pjproject from source with Python SWIG bindings
ARG PJPROJECT_TAG=2.14.1
RUN git clone --depth 1 --branch ${PJPROJECT_TAG} \
        https://github.com/pjsip/pjproject.git /opt/pjproject

WORKDIR /opt/pjproject

# Disable video, enable shared libs + SSL
RUN ./configure \
        CFLAGS="-fPIC" \
        --enable-shared \
        --with-ssl \
        --disable-video \
    && make dep \
    && make -j$(nproc) \
    && make install \
    && cd pjsip-apps/src/swig/python \
    && make \
    && make install \
    && ldconfig

# Runtime stage
FROM python:3.13-bookworm

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 \
    libasound2 \
    tcpdump \
    libcap2-bin \
    && rm -rf /var/lib/apt/lists/*

# Allow tcpdump to open raw sockets without root. The image has no baked-in
# UID — runtime user comes from `user:` in docker-compose.yml so the image
# stays portable across hosts with different host UIDs. Container still needs
# NET_RAW/NET_ADMIN in its bounding set (granted by cap_add in compose).
RUN setcap cap_net_raw,cap_net_admin+eip /usr/bin/tcpdump

# Copy pjproject shared libraries
COPY --from=builder /usr/local/lib/libpj*.so* /usr/local/lib/
COPY --from=builder /usr/local/lib/libresample*.so* /usr/local/lib/
COPY --from=builder /usr/local/lib/libsrtp*.so* /usr/local/lib/
COPY --from=builder /usr/local/lib/libgsmcodec*.so* /usr/local/lib/
COPY --from=builder /usr/local/lib/libspeex*.so* /usr/local/lib/
COPY --from=builder /usr/local/lib/libilbccodec*.so* /usr/local/lib/
COPY --from=builder /usr/local/lib/libg7221codec*.so* /usr/local/lib/
COPY --from=builder /usr/local/lib/libwebrtc*.so* /usr/local/lib/
# Copy pjsua2 Python bindings from SWIG build output
COPY --from=builder \
     /opt/pjproject/pjsip-apps/src/swig/python/build/lib.linux-x86_64-cpython-313/ \
     /usr/local/lib/python3.13/site-packages/
RUN ldconfig

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application
COPY src/ /app/src/
COPY tests/ /app/tests/
COPY audio/ /app/audio/
COPY scenarios/ /app/scenarios/
COPY pyproject.toml /app/pyproject.toml

WORKDIR /app

# Ensure output dirs exist. Mode 1777 (sticky + world-writable, like /tmp)
# means any runtime UID set via `user:` in compose can write here while only
# the owner can delete their own files. /config is mounted read-only.
RUN mkdir -p /captures /recordings /config \
    && chmod 1777 /captures /recordings

# Unbuffered output to protect MCP stdio channel
ENV PYTHONUNBUFFERED=1

# HOME defaults to / for arbitrary UIDs with no /etc/passwd entry; /tmp is
# always writable, so point HOME there to avoid noisy warnings from libs.
ENV HOME=/tmp

ENTRYPOINT ["python", "-u", "-m", "src.server"]
