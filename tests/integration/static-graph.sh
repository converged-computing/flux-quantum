#!/bin/bash
# Build a config directory that gives a throwaway test instance the static
# scheduling graph, the same way the real cluster gets it:
#
#   source tests/integration/static-graph.sh
#   dir=$(make_static_config 2 "mock ibm braket" 4)
#   flux start -s2 -o,--config-path=$dir/conf bash tests/integration/test-X.sh
#
# Phase 1 starts a plain instance of the same size just to read the R flux
# derives for this machine, so the generated graph's ranks, hostnames and
# cores match the broker's R verbatim (there is no namespace remapping in a
# system or top-level instance). Phase 2 is the caller's instance, with
# [resource] scheduling pointing at the generated file. R is deterministic
# for a given machine and size, so the two phases see the same resources.
make_static_config () {
    local size="$1" vendors="$2" qpus="${3:-2}"
    local dir
    dir=$(mktemp -d) || return 1
    flux start -s "$size" sh -c 'flux kvs get resource.R' > "$dir/R.json" || return 1
    flux python -m flux_quantum.schedjgf $vendors --qpus "$qpus" \
        --from-r "$dir/R.json" --output "$dir/scheduling.json" || return 1
    mkdir -p "$dir/conf"
    # resource.scheduling must ride on a configured R (resource.path), the
    # same pairing the system instance uses in /etc/flux/system/conf.d
    cat > "$dir/conf/resource.toml" <<EOF
[resource]
path = "$dir/R.json"
scheduling = "$dir/scheduling.json"
EOF
    echo "$dir"
}
