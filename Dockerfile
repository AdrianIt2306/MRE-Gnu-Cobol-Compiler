FROM debian:bookworm-slim

# Instalar dependencias base
RUN apt-get update && apt-get install -y \
    gnucobol \
    libcob4-dev \
    libpq-dev \
    libssl-dev \
    zlib1g-dev \
    libxml2 \
    patchelf \
    git \
    make \
    automake \
    autoconf \
    libtool \
    pkg-config \
    gcc \
    bison \
    flex \
    && rm -rf /var/lib/apt/lists/*

# Compilar e instalar OCESQL desde fuente
RUN git clone https://github.com/opensourcecobol/Open-COBOL-ESQL.git /tmp/ocesql \
    && cd /tmp/ocesql \
    && autoreconf -fi \
    && ./configure --prefix=/usr/local \
    && make \
    && make install \
    && ldconfig \
    && rm -rf /tmp/ocesql

# Python 3 (para build.py)
RUN apt-get update && apt-get install -y python3 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# build.py se copia en la imagen; src/ se monta como volumen en runtime
COPY build.py .

ENTRYPOINT ["python3", "build.py"]
