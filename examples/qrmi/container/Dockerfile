# syntax=docker/dockerfile:1
# The flux-quantum coscheduling stack in one container, for the QRMI demo.
# docker build -f examples/qrmi/container/Dockerfile -t flux-quantum-qrmi --build-arg FLUX_QUANTUM_REF= .

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8

ARG FLUX_SECURITY_VERSION=0.15.0
ARG FLUX_CORE_VERSION=0.87.0
ARG FLUX_SCHED_REPO=https://github.com/vsoch/flux-sched
ARG FLUX_SCHED_BRANCH=hold
# a shallow clone has no tags to describe, so cmake is told a version. It
# only names the build, the branch is what matters.
ARG FLUX_SCHED_VERSION=0.56.0
ARG FLUX_QUANTUM_REPO=https://github.com/converged-computing/flux-quantum
# the branch to clone, as the cluster's start-script does. Empty means use the
# build context, this checkout, which is the only way to get work that is not
# pushed yet into the image.
ARG FLUX_QUANTUM_REF=braket-hybrid
# pip spec for qrmi. Pin here, e.g. "qrmi[ibm]==0.26.0", for a reproducible image
ARG QRMI_SPEC="qrmi[ibm]"
ARG INSTALL_IBMCLOUD=1

# build and runtime dependencies of flux-core and flux-sched, the list from
# the AMI build script, in ubuntu 24.04 package names
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget git jq vim less sudo apt-utils \
        build-essential pkg-config autotools-dev libtool libffi-dev \
        autoconf automake make gcc g++ cmake ninja-build flex \
        lua5.4 liblua5.4-dev lua-posix \
        python3-dev python3-pip python3-yaml \
        libsodium-dev libzmq3-dev libczmq-dev libjansson-dev \
        munge libmunge-dev libncursesw5-dev liblz4-dev libsqlite3-dev \
        uuid-dev libhwloc-dev libevent-dev libarchive-dev libpam-dev \
        libboost-dev libboost-graph-dev libboost-system-dev \
        libboost-filesystem-dev libboost-regex-dev libyaml-cpp-dev \
        libedit-dev \
    && rm -rf /var/lib/apt/lists/*

# flux-core's python bindings need cffi and ply, and qrmi later needs a newer
# cffi than debian ships. A debian cffi has no pip RECORD, so pip cannot
# replace it and the qrmi install fails. Install these with pip from the
# start, like the CI image does, so pip owns them and can upgrade them.
RUN python3 -m pip install --break-system-packages --no-cache-dir \
        --upgrade --ignore-installed cffi ply pyyaml jsonschema

WORKDIR /opt

# flux-core 0.87 needs flux-security 0.13 or newer
RUN wget -q https://github.com/flux-framework/flux-security/releases/download/v${FLUX_SECURITY_VERSION}/flux-security-${FLUX_SECURITY_VERSION}.tar.gz \
    && tar -xzf flux-security-${FLUX_SECURITY_VERSION}.tar.gz \
    && cd flux-security-${FLUX_SECURITY_VERSION} \
    && ./configure --prefix=/usr --sysconfdir=/etc \
    && make -j"$(nproc)" && make install && ldconfig \
    && cd /opt && rm -rf flux-security-${FLUX_SECURITY_VERSION}*

# prefix /usr and sysconfdir /etc, like the AMI. flux python is then
# /usr/bin/python3, the python everything below installs into.
RUN wget -q https://github.com/flux-framework/flux-core/releases/download/v${FLUX_CORE_VERSION}/flux-core-${FLUX_CORE_VERSION}.tar.gz \
    && tar -xzf flux-core-${FLUX_CORE_VERSION}.tar.gz \
    && cd flux-core-${FLUX_CORE_VERSION} \
    && ./configure --prefix=/usr --sysconfdir=/etc --runstatedir=/run/flux \
                   --with-flux-security --disable-docs \
    && make -j"$(nproc)" && make install && ldconfig \
    && cd /opt && rm -rf flux-core-${FLUX_CORE_VERSION}*

# CMAKE_INSTALL_SYSCONFDIR must be the absolute /etc. flux-sched derives its
# rc1.d path from it before adopting flux-core's prefix, and a relative "etc"
# lands the fluxion rc script in /usr/etc/flux/rc1.d where flux-core never
# looks, so fluxion silently never loads and the instance runs sched-simple.
RUN git clone --depth 1 -b "${FLUX_SCHED_BRANCH}" "${FLUX_SCHED_REPO}" /opt/flux-sched \
    && cd /opt/flux-sched && git log --oneline -1 \
    && FLUX_SCHED_VERSION=${FLUX_SCHED_VERSION} cmake -B build \
         -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_INSTALL_SYSCONFDIR=/etc \
         -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    && cmake --build build -j"$(nproc)" \
    && cmake --build build -t install && ldconfig \
    && test -x /etc/flux/rc1.d/01-sched-fluxion \
    && test -f /etc/flux/modprobe/modprobe.d/fluxion.toml \
    && rm -rf /opt/flux-sched/build

# qrmi into the python flux uses. The flux-quantum ibm backend still speaks
# the pre-0.23 names (ResourceType.IBMQiskitRuntimeService, the QRMI_IBM_QRS_
# variables), which qrmi keeps as deprecated aliases until 2026-11-21. Fail
# the build now, rather than at the first submit, if the installed qrmi has
# dropped them.
RUN python3 -m pip install --break-system-packages --no-cache-dir "${QRMI_SPEC}"
RUN python3 - <<'PY'
import qrmi, qiskit
from qrmi import ResourceType
print("qrmi", getattr(qrmi, "__version__", "?"), "qiskit", qiskit.__version__)
assert hasattr(ResourceType, "IBMQiskitRuntimeService"), (
    "this qrmi has dropped ResourceType.IBMQiskitRuntimeService. Pin an older "
    "qrmi with --build-arg QRMI_SPEC='qrmi[ibm]==<version>' or migrate "
    "flux_quantum/backends/qrmi.py to the ibm-quantum-compute-service names")
PY

RUN if [ "${INSTALL_IBMCLOUD}" = 1 ]; then \
        curl -fsSL https://clis.cloud.ibm.com/install/linux | sh \
        && ibmcloud version; \
    fi

# the demo runs as a user, the scout and the classical job both run as the
# submitting user and that is where the credentials live
ARG USERNAME=ubuntu
RUN echo "${USERNAME} ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/${USERNAME} \
    && chmod 0440 /etc/sudoers.d/${USERNAME} \
    && mkdir -p /etc/flux/system/jobtap /etc/flux/system/conf.d

# flux-quantum, from the branch or from this checkout. Editable, so a bind
# mount of the repository over /opt/flux-quantum picks up edits without a
# rebuild. The jobtap plugin is rebuilt from source, since a checkout may
# carry a quantum.so built against another flux-core. The scripts under
# examples/qrmi/container/bin come from the same tree, so a cloned branch
# has to carry this directory already.
COPY --chown=${USERNAME}:${USERNAME} . /opt/flux-quantum-checkout
RUN if [ -n "${FLUX_QUANTUM_REF}" ]; then \
        git clone --depth 1 -b "${FLUX_QUANTUM_REF}" "${FLUX_QUANTUM_REPO}" /opt/flux-quantum \
        && rm -rf /opt/flux-quantum-checkout; \
    else \
        mv /opt/flux-quantum-checkout /opt/flux-quantum; \
    fi \
    && cd /opt/flux-quantum && (git log --oneline -1 || echo "flux-quantum: this checkout") \
    && { test -d /opt/flux-quantum/examples/qrmi/container/bin \
         || { echo "flux-quantum: ${FLUX_QUANTUM_REF:-the checkout} has no examples/qrmi/container/bin." \
                   "Push the directory, or build with --build-arg FLUX_QUANTUM_REF= to use this checkout." >&2; exit 1; }; } \
    && python3 -m pip install --break-system-packages --no-cache-dir -e /opt/flux-quantum \
    && flux python -c "from flux_quantum.cli import QuantumCLIPlugin; print('flux_quantum importable')" \
    && rm -f /opt/flux-quantum/flux_quantum/jobtap/quantum.so \
    && make -C /opt/flux-quantum/flux_quantum/jobtap \
    && cp /opt/flux-quantum/flux_quantum/jobtap/quantum.so /etc/flux/system/jobtap/ \
    && chown -R ${USERNAME}:${USERNAME} /opt/flux-quantum \
    && install -m 0755 /opt/flux-quantum/examples/qrmi/container/bin/* /usr/local/bin/ \
    && chmod +x /opt/flux-quantum/examples/qrmi/ibm-run.sh \
    && ln -sf /opt/flux-quantum/examples/qrmi/ibm-run.sh /usr/local/bin/ibm-run

# FLUX_CLI_PLUGINPATH holds only the discovery shim, never the package.
# FLUX_QUANTUM_MOCK registers the token free mock vendor. It has to be in the
# environment before flux start, because the ingest validator is a separate
# process. Real vendors are registered regardless, so it stays on.
ENV FLUX_CLI_PLUGINPATH=/opt/flux-quantum/cli-plugins \
    FLUX_QUANTUM_MOCK=1 \
    FLUX_QUANTUM_JOBTAP=/etc/flux/system/jobtap/quantum.so

USER ${USERNAME}
WORKDIR /opt/flux-quantum/examples/qrmi/container
CMD ["flux-quantum-start"]
