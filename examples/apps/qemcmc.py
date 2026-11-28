"""Quantum-enhanced Markov chain Monte Carlo, after Layden et al.,
Nature 619, 282-287 (2023), arXiv:2203.12497.

The sampler targets the Boltzmann distribution of a classical Ising model.
Each step proposes by running a quantum circuit: prepare the current spin
configuration, evolve under H = (1 - gamma) * alpha * H_problem + gamma *
sum_j X_j for a time t, with gamma and t drawn fresh every step, and measure.
The classical side accepts or rejects with Metropolis-Hastings. The evolution
is Trotterized palindromically, so the circuit's matrix stays symmetric and
the proposal satisfies Q(s'|s) = Q(s|s'), which is what detailed balance
needs. Trotter error changes the proposal, not its symmetry, so any Trotter
depth gives a correct sampler.

Everything here is the standard library. Circuits are built as a neutral
list of gate ops, lowered to two wire formats (OpenQASM 3 for Braket and
QRMI, the IonQ circuit dict for IonQ's API), and runnable on the built-in
statevector simulator, which is exact and plenty fast below a dozen qubits.

Bit convention everywhere in this module: a state is a string of '0'/'1'
where character i is qubit i. Integers index the basis with qubit 0 as the
least significant bit.
"""

import cmath
import json
import math
import random

# per-step ranges for the proposal, from the paper
GAMMA_RANGE = (0.25, 0.6)
TIME_RANGE = (2.0, 20.0)


# ---- the model -------------------------------------------------------------


class Ising(object):
    """A classical Ising model: E(s) = -sum J_ij s_i s_j - sum h_i s_i,
    spins s_i in {+1, -1}, bit 0 is spin +1.

    couplings is {(i, j): J_ij} with i < j, fields is a list of h_i.
    """

    def __init__(self, couplings, fields):
        self.n = len(fields)
        self.couplings = dict(couplings)
        self.fields = list(fields)
        norm = sum(v * v for v in self.couplings.values())
        norm += sum(v * v for v in self.fields)
        # the paper's normalisation, so H_problem and the mixer have
        # comparable weight whatever the instance
        self.alpha = math.sqrt(self.n / norm) if norm else 1.0

    @classmethod
    def random(cls, n, seed=0):
        """A fully connected spin glass: J and h standard normal."""
        rng = random.Random(seed)
        couplings = {}
        for i in range(n):
            for j in range(i + 1, n):
                couplings[(i, j)] = rng.gauss(0.0, 1.0)
        fields = [rng.gauss(0.0, 1.0) for _ in range(n)]
        return cls(couplings, fields)

    def energy(self, state):
        spins = [1 - 2 * int(b) for b in state]
        e = 0.0
        for (i, j), v in self.couplings.items():
            e -= v * spins[i] * spins[j]
        for i, v in enumerate(self.fields):
            e -= v * spins[i]
        return e

    def states(self):
        for k in range(1 << self.n):
            yield int_to_state(k, self.n)

    def boltzmann(self, temperature):
        """The exact target distribution, {state: probability}."""
        weights = {s: math.exp(-self.energy(s) / temperature) for s in self.states()}
        z = sum(weights.values())
        return {s: w / z for s, w in weights.items()}


def int_to_state(k, n):
    return "".join("1" if (k >> i) & 1 else "0" for i in range(n))


def state_to_int(state):
    return sum(1 << i for i, b in enumerate(state) if b == "1")


# ---- the proposal circuit --------------------------------------------------


def proposal_ops(model, state, gamma, t, trotter=2):
    """The gate ops for one proposal: prepare state, evolve, measure later.

    The Trotterization is the palindrome A/2 B A B ... B A/2 where A is the
    diagonal problem phase and B the transverse field, so the whole product
    is a symmetric matrix and the proposal is symmetric.
    """
    ops = [("x", i) for i, b in enumerate(state) if b == "1"]
    tau = float(t) / trotter
    a = (1.0 - gamma) * model.alpha

    def problem(scale):
        out = []
        for (i, j), v in model.couplings.items():
            out.append(("rzz", i, j, -2.0 * a * v * tau * scale))
        for i, v in enumerate(model.fields):
            if v:
                out.append(("rz", i, -2.0 * a * v * tau * scale))
        return out

    mixer = [("rx", i, 2.0 * gamma * tau) for i in range(model.n)]
    ops += problem(0.5)
    for _ in range(trotter - 1):
        ops += mixer
        ops += problem(1.0)
    ops += mixer
    ops += problem(0.5)
    return ops


def lower(ops):
    """The same circuit with rzz spelled as cnot, rz, cnot, which every
    wire format takes."""
    out = []
    for op in ops:
        if op[0] == "rzz":
            _, i, j, angle = op
            out.append(("cnot", i, j))
            out.append(("rz", j, angle))
            out.append(("cnot", i, j))
        else:
            out.append(op)
    return out


def to_qasm3(n, ops):
    lines = ["OPENQASM 3;", "qubit[{}] q;".format(n), "bit[{}] c;".format(n)]
    for op in lower(ops):
        if op[0] == "x":
            lines.append("x q[{}];".format(op[1]))
        elif op[0] == "h":
            lines.append("h q[{}];".format(op[1]))
        elif op[0] == "rx":
            lines.append("rx({:.12g}) q[{}];".format(op[2], op[1]))
        elif op[0] == "rz":
            lines.append("rz({:.12g}) q[{}];".format(op[2], op[1]))
        elif op[0] == "cnot":
            lines.append("cnot q[{}], q[{}];".format(op[1], op[2]))
        else:
            raise ValueError("no qasm for {}".format(op[0]))
    lines.append("c = measure q;")
    return "\n".join(lines) + "\n"


def to_ionq(n, ops):
    circuit = []
    for op in lower(ops):
        if op[0] in ("x", "h"):
            circuit.append({"gate": op[0], "target": op[1]})
        elif op[0] in ("rx", "rz"):
            circuit.append({"gate": op[0], "target": op[1], "rotation": op[2]})
        elif op[0] == "cnot":
            circuit.append({"gate": "cnot", "control": op[1], "target": op[2]})
        else:
            raise ValueError("no ionq gate for {}".format(op[0]))
    return {"qubits": n, "gateset": "qis", "circuit": circuit}


def from_ionq(program):
    """The ops back from an IonQ circuit dict, for simulating it."""
    n = int(program["qubits"])
    ops = []
    for g in program["circuit"]:
        name = g["gate"]
        if name in ("x", "h"):
            ops.append((name, int(g["target"])))
        elif name in ("rx", "rz"):
            ops.append((name, int(g["target"]), float(g["rotation"])))
        elif name in ("cnot", "cx"):
            ops.append(("cnot", int(g["control"]), int(g["target"])))
        else:
            raise ValueError("cannot simulate gate {}".format(name))
    return n, ops


# ---- the simulator ---------------------------------------------------------


def simulate(n, ops):
    """The statevector after ops on |0...0>, a list of 2**n amplitudes."""
    size = 1 << n
    state = [0j] * size
    state[0] = 1.0 + 0j
    for op in ops:
        name = op[0]
        if name == "x":
            _one_qubit(state, op[1], 0.0, 1.0, 1.0, 0.0)
        elif name == "h":
            r = 1.0 / math.sqrt(2.0)
            _one_qubit(state, op[1], r, r, r, -r)
        elif name == "rx":
            c = math.cos(op[2] / 2.0)
            s = -1j * math.sin(op[2] / 2.0)
            _one_qubit(state, op[1], c, s, s, c)
        elif name == "rz":
            _phase(state, op[1], op[2])
        elif name == "rzz":
            _zz_phase(state, op[1], op[2], op[3])
        elif name == "cnot":
            _cnot(state, op[1], op[2])
        else:
            raise ValueError("cannot simulate {}".format(name))
    return state


def _one_qubit(state, q, a, b, c, d):
    """Apply [[a, b], [c, d]] on qubit q."""
    stride = 1 << q
    for base in range(0, len(state), stride << 1):
        for k in range(base, base + stride):
            lo, hi = state[k], state[k + stride]
            state[k] = a * lo + b * hi
            state[k + stride] = c * lo + d * hi


def _phase(state, q, angle):
    lo = cmath.exp(-0.5j * angle)
    hi = cmath.exp(0.5j * angle)
    bit = 1 << q
    for k in range(len(state)):
        state[k] *= hi if k & bit else lo


def _zz_phase(state, q1, q2, angle):
    even = cmath.exp(-0.5j * angle)
    odd = cmath.exp(0.5j * angle)
    b1, b2 = 1 << q1, 1 << q2
    for k in range(len(state)):
        same = bool(k & b1) == bool(k & b2)
        state[k] *= even if same else odd


def _cnot(state, control, target):
    cb, tb = 1 << control, 1 << target
    for k in range(len(state)):
        if k & cb and not k & tb:
            other = k | tb
            state[k], state[other] = state[other], state[k]


def probabilities(state_vector, n):
    """{state string: probability}, zeros left out."""
    out = {}
    for k, amp in enumerate(state_vector):
        p = (amp.real * amp.real) + (amp.imag * amp.imag)
        if p > 1e-12:
            out[int_to_state(k, n)] = p
    return out


def draw(weights, rng):
    """One state from {state: weight}."""
    total = sum(weights.values())
    r = rng.random() * total
    acc = 0.0
    last = None
    for state, w in weights.items():
        acc += w
        last = state
        if r <= acc:
            return state
    return last


# ---- the chain -------------------------------------------------------------


class LocalSampler(object):
    """Proposals from the exact simulator, for runs that cost nothing."""

    def __init__(self, rng=None):
        self.rng = rng or random.Random()

    def __call__(self, n, ops, meta=None):
        return draw(probabilities(simulate(n, ops), n), self.rng)


class Chain(object):
    """One Metropolis-Hastings chain with the quantum proposal.

    sampler(n, ops, meta) returns the measured state for one proposal
    circuit; meta carries the step's gamma, t and label for samplers that
    submit real tasks and want to record them.
    """

    def __init__(self, model, temperature, seed=0, trotter=2, start=None):
        self.model = model
        self.temperature = float(temperature)
        self.trotter = trotter
        self.rng = random.Random(seed)
        self.state = start or int_to_state(
            self.rng.randrange(1 << model.n), model.n
        )
        self.energy = model.energy(self.state)
        self.steps = 0
        self.accepted = 0
        self.history = []

    def step(self, sampler):
        gamma = self.rng.uniform(*GAMMA_RANGE)
        t = self.rng.uniform(*TIME_RANGE)
        ops = proposal_ops(self.model, self.state, gamma, t, self.trotter)
        meta = {"step": self.steps, "gamma": gamma, "t": t}
        proposal = sampler(self.model.n, ops, meta)
        e_new = self.model.energy(proposal)
        delta = e_new - self.energy
        accept = delta <= 0 or self.rng.random() < math.exp(
            -delta / self.temperature
        )
        record = {
            "step": self.steps,
            "state": self.state,
            "proposal": proposal,
            "gamma": round(gamma, 6),
            "t": round(t, 6),
            "energy": round(self.energy, 6),
            "proposal_energy": round(e_new, 6),
            "accepted": bool(accept),
        }
        if accept:
            self.state = proposal
            self.energy = e_new
            self.accepted += 1
        self.steps += 1
        self.history.append(record)
        return record

    def run(self, sampler, steps):
        for _ in range(steps):
            self.step(sampler)
        return self.history

    def summary(self):
        return {
            "steps": self.steps,
            "accepted": self.accepted,
            "acceptance": (self.accepted / self.steps) if self.steps else None,
            "energy": self.energy,
            "state": self.state,
        }


def empirical(history):
    """{state: fraction of steps spent there}, counting the post-step
    state, which is what the chain samples."""
    counts = {}
    state = None
    for rec in history:
        state = rec["proposal"] if rec["accepted"] else rec["state"]
        counts[state] = counts.get(state, 0) + 1
    total = sum(counts.values())
    return {s: c / total for s, c in counts.items()}


def main(argv=None):
    """A small local run, to see the sampler work: prints the chain summary
    and the distance to the exact Boltzmann distribution."""
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--qubits", type=int, default=4)
    ap.add_argument("--steps", type=int, default=2000)
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--trotter", type=int, default=2)
    args = ap.parse_args(argv)

    model = Ising.random(args.qubits, seed=args.seed)
    chain = Chain(model, args.temperature, seed=args.seed, trotter=args.trotter)
    chain.run(LocalSampler(rng=random.Random(args.seed + 1)), args.steps)
    seen = empirical(chain.history)
    target = model.boltzmann(args.temperature)
    tv = 0.5 * sum(abs(seen.get(s, 0.0) - target[s]) for s in target)
    out = chain.summary()
    out["total_variation_to_boltzmann"] = round(tv, 4)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
