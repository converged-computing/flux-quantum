#!/bin/bash
# Sourced by the integration tests that submit quantum pairs. A pair needs
# fluxion: the submit plugin reads the graph through sched-fluxion-resource
# and the scout releases the held job through qmanager. sched-simple has
# neither.
#
# qmanager runs the coschedule queue policy. A held job only stays held
# under it, under fcfs the classical half is scheduled straight away and the
# scout's release fails because the job is no longer pending. A fluxion
# without the policy falls back to the default, so an older build still runs.
#
# sched-fluxion-feasibility is left out on purpose. It keeps a private copy of
# the graph that the qdevice vertices populate adds never reach, so with it
# loaded every pair is refused as unsatisfiable.
ensure_fluxion () {
    # reverse dependency order, feasibility sits between resource and qmanager
    flux module remove -f sched-fluxion-qmanager 2>/dev/null || true
    flux module remove -f sched-fluxion-feasibility 2>/dev/null || true
    flux module remove -f sched-fluxion-resource 2>/dev/null || true
    flux module remove -f sched-simple 2>/dev/null || true
    flux module load sched-fluxion-resource || return 1
    if flux module load sched-fluxion-qmanager queue-policy=coschedule 2>/dev/null; then
        echo "fluxion loaded, queue policy coschedule"
    else
        flux module load sched-fluxion-qmanager || return 1
        echo "fluxion loaded, WARNING no coschedule policy, held jobs may not stay held"
    fi
}
