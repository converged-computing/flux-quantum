"""The Braket tests run without the AWS SDKs.

tests/probe_hold.py imports boto3 and braket.aws at the top, since as a tool
it cannot run without them. Its logic, the verdict, the queue, the timeline,
is what these tests check, and none of that calls AWS. So when the SDKs are
not installed, as in the unit CI job, two stand-in modules let it import. A
test that reaches a stand-in fails loudly rather than talking to AWS.
"""

import sys
import types

import pytest


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    mod.__flux_quantum_stub__ = True
    return mod


def _unavailable(what):
    def fail(*a, **k):
        raise RuntimeError("{} is a stand-in in the unit tests".format(what))

    return fail


try:
    import boto3  # noqa: F401
except ImportError:
    sys.modules["boto3"] = _stub(
        "boto3",
        client=_unavailable("boto3.client"),
        Session=_unavailable("boto3.Session"),
    )

try:
    import braket.aws  # noqa: F401
except ImportError:
    pkg = sys.modules.get("braket") or _stub("braket")
    aws = _stub(
        "braket.aws",
        AwsSession=_unavailable("braket.aws.AwsSession"),
        AwsDevice=_unavailable("braket.aws.AwsDevice"),
        AwsQuantumTask=_unavailable("braket.aws.AwsQuantumTask"),
    )
    pkg.aws = aws
    sys.modules.setdefault("braket", pkg)
    sys.modules["braket.aws"] = aws


@pytest.fixture
def braket_sdk():
    """Skip a test that drives the real SDK objects when only the stand-ins
    are installed. pip install amazon-braket-sdk to run it."""
    for name in ("boto3", "braket.aws"):
        if getattr(sys.modules.get(name), "__flux_quantum_stub__", False):
            pytest.skip("needs amazon-braket-sdk, {} is a stand-in".format(name))
    pytest.importorskip("botocore")
