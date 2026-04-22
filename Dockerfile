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
    && rm -rf /var/lib/apt/lists/*

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
COPY pyproject.toml /app/pyproject.toml

WORKDIR /app

# Ensure output dirs exist
RUN mkdir -p /captures /recordings /config

# Unbuffered output to protect MCP stdio channel
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-u", "-m", "src.server"]
