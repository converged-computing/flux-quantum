FROM ubuntu:24.04 AS builder
ENV DEBIAN_FRONTEND=noninteractive

# docker build -t ghcr.io/converged-computing/fluxion-quantum:flux-base-ubuntu-test .

RUN apt-get update && apt-get install -y build-essential \
    tar \
    autoconf \
    automake \
    make \
    wget \
    git \
    gcc \
    g++ \
    zip \
    libblas-dev \
    liblapack-dev \
    libxml2 \
    libxml2-dev \
    hdf5-tools \
    libhdf5-dev \
    libboost-all-dev \
    && apt-get clean

# Utilities
RUN apt-get update && \
    apt-get -qq install -y --no-install-recommends \
        apt-utils \
        locales \
        ca-certificates \
        wget \
        man \
        git \
        flex \
        ssh \
        sudo \
        vim \
        luarocks \
        munge \
        lcov \
        ccache \
        lua5.2 \
        python3-dev \
        python3-pip \
        valgrind \
        jq && \
    rm -rf /var/lib/apt/lists/*

# Compilers, autotools
RUN apt-get update && \
    apt-get -qq install -y --no-install-recommends \
        build-essential \
        pkg-config \
        autotools-dev \
        libtool \
	libffi-dev \
        autoconf \
        automake \
        make \
        clang \
        clang-tidy \
        gcc \
        g++ && \
    rm -rf /var/lib/apt/lists/*

# Python build/test/doc helpers. On Noble (24.04) the system Python is
# PEP-668 "externally managed", so --break-system-packages is required for a
# system pip install (fine for a throwaway build image). Old markupsafe /
# jsonschema pins are dropped -- they don't play with Python 3.12 or a modern
# flux-core.
RUN pip install --break-system-packages --upgrade --ignore-installed \
        coverage cffi ply six pyyaml jsonschema \
        sphinx sphinx-rtd-theme sphinxcontrib-spelling

RUN apt-get update && \
    apt-get -qq install -y --no-install-recommends \
        libsodium-dev \
        libzmq3-dev \
        libczmq-dev \
        libjansson-dev \
        libmunge-dev \
        libncursesw5-dev \
        liblua5.2-dev \
        liblz4-dev \
        libsqlite3-dev \
        uuid-dev \
        libhwloc-dev \
        libs3-dev \
        libevent-dev \
        libarchive-dev \
        libpam-dev && \
    rm -rf /var/lib/apt/lists/*

# Testing utils and libs
RUN apt-get update && \
    apt-get -qq install -y --no-install-recommends \
        faketime \
        libfaketime \
        pylint \
        cppcheck \
        enchant-2 \
        aspell \
        aspell-en && \
    rm -rf /var/lib/apt/lists/*

RUN locale-gen en_US.UTF-8

# NOTE: luaposix installed by rocks due to Ubuntu bug: #1752082 https://bugs.launchpad.net/ubuntu/+source/lua-posix/+bug/1752082
RUN luarocks install luaposix

ARG PMIX_VERSION=5.0.10
ARG PRRTE_VERSION=3.0.13
WORKDIR /opt
RUN wget https://github.com/openpmix/openpmix/releases/download/v${PMIX_VERSION}/pmix-${PMIX_VERSION}.tar.gz && \
    tar xzf pmix-${PMIX_VERSION}.tar.gz && cd pmix-${PMIX_VERSION} && \
    ./configure --prefix=/usr && make -j"$(nproc)" install && ldconfig && \
    cd /opt && rm -rf pmix-${PMIX_VERSION}* && \
    wget https://github.com/openpmix/prrte/releases/download/v${PRRTE_VERSION}/prrte-${PRRTE_VERSION}.tar.gz && \
    tar xzf prrte-${PRRTE_VERSION}.tar.gz && cd prrte-${PRRTE_VERSION} && \
    ./configure --prefix=/usr --with-pmix=/usr && make -j"$(nproc)" install && ldconfig && \
    cd /opt && rm -rf prrte-${PRRTE_VERSION}*

ENV LANG=C.UTF-8
# flux-security: bumped to latest (was 0.11.0)
ARG FLUX_SECURITY_VERSION=0.15.0

WORKDIR /opt
RUN CCACHE_DISABLE=1 && \
    V=$FLUX_SECURITY_VERSION && \
    PKG=flux-security-$V && \
    URL=https://github.com/flux-framework/flux-security/releases/download && \
    wget ${URL}/v${V}/${PKG}.tar.gz && \
    tar xvfz ${PKG}.tar.gz && \
    cd ${PKG} && \
    ./configure --prefix=/usr --sysconfdir=/etc || cat config.log && \
    make -j 4 && \
    make install && \
    cd .. && \
    rm -rf flux-security-*

# Setup MUNGE directories & key
RUN mkdir -p /var/run/munge && \
    dd if=/dev/urandom bs=1 count=1024 > /etc/munge/munge.key && \
    chown -R munge /etc/munge/munge.key /var/run/munge && \
    chmod 600 /etc/munge/munge.key

# flux-core: bumped to latest (was 0.61.2); still autotools from the release
# tarball. --with-flux-security enables signing/IMP (built above).
ARG FLUX_CORE_VERSION=0.87.0
RUN wget https://github.com/flux-framework/flux-core/releases/download/v${FLUX_CORE_VERSION}/flux-core-${FLUX_CORE_VERSION}.tar.gz && \
    tar xzvf flux-core-${FLUX_CORE_VERSION}.tar.gz && \
    cd flux-core-${FLUX_CORE_VERSION} && \
    ./configure --prefix=/usr --sysconfdir=/etc --with-flux-security && \
    make clean && \
    make -j$(nproc) && \
    sudo make install && \
    ldconfig && \
    cd .. && rm -rf flux-core-${FLUX_CORE_VERSION}*

# flux-sched build dependencies. Added ninja-build (the default CMake preset
# uses Ninja) and python3-yaml (fluxion python bindings / flux-ion tools).
RUN sudo apt-get update && \
    sudo apt-get -qq install -y --no-install-recommends \
	libboost-graph-dev \
	libboost-system-dev \
	libboost-filesystem-dev \
	libboost-regex-dev \
	libyaml-cpp-dev \
	libedit-dev \
        libboost-dev \
        ninja-build \
        python3-yaml \
	curl && \
    rm -rf /var/lib/apt/lists/*

ENV LD_LIBRARY_PATH=/opt/miniconda/lib

# CMake: bumped to 3.26.4 (was 3.23.1). flux-sched's CMakePresets.json is
# schema version 6, which requires CMake >= 3.25. uname -m covers x86_64/arm.
ENV CMAKE=3.26.4
RUN curl -s -L https://github.com/Kitware/CMake/releases/download/v$CMAKE/cmake-$CMAKE-linux-$(uname -m).sh > cmake.sh && \
    sudo sh cmake.sh --prefix=/usr/local --skip-license && \
    rm cmake.sh

# flux-sched: build from the add-hold branch of vsoch/flux-sched (hold/unhold +
# reserve-only verb + reserve-first). This is a git checkout, not a release
# tarball, so we use the CMake preset build (the ./configure path is gone for
# git builds). No submodules to init. Installs into /usr so its fluxion modules
# match the flux-core installed above.
ARG FLUX_SCHED_REPO=https://github.com/vsoch/flux-sched
ARG FLUX_SCHED_BRANCH=add-hold
ENV FLUX_SCHED_VERSION=0.59.0
RUN git clone --depth 1 --branch ${FLUX_SCHED_BRANCH} ${FLUX_SCHED_REPO} flux-sched && \
    cd flux-sched && \
    cmake -B build --preset default -DCMAKE_INSTALL_PREFIX=/usr && \
    cmake --build build -j$(nproc) && \
    cmake --build build -t install && \
    ldconfig 

RUN apt-get update && \
    apt-get install -y pdsh libfabric-dev libfabric1 \
        openssh-client openssh-server \
        dnsutils telnet strace git g++ \
        unzip bzip2

WORKDIR /opt

RUN export EFA_VERSION=latest && \
    mkdir /tmp/efa && cd /tmp/efa && \
    curl -O https://s3-us-west-2.amazonaws.com/aws-efa-installer/aws-efa-installer-${EFA_VERSION}.tar.gz && \
    tar -xf aws-efa-installer-${EFA_VERSION}.tar.gz && cd aws-efa-installer && ./efa_installer.sh -y --skip-kmod

ENV PATH=/opt/amazon/openmpi/bin:$PATH
ENV LD_LIBRARY_PATH=/opt/amazon/openmpi/lib:$LD_LIBRARY_PATH
COPY flux-inject.py /usr/libexec/flux/cmd/flux-inject.py
RUN chmod +x /usr/libexec/flux/cmd/flux-inject.py
