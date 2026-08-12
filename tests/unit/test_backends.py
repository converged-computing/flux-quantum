IBM_CREDS = {
    "ibm_kingston_QRMI_IBM_QRS_ENDPOINT": "supersecret",
    "ibm_kingston_QRMI_IBM_QRS_IAM_ENDPOINT": "supersecret",
    "ibm_kingston_QRMI_IBM_QRS_IAM_APIKEY": "supersecret",
    "ibm_kingston_QRMI_IBM_QRS_SERVICE_CRN": "supersecret",
}


def test_real_backends_registered(backends_real):
    assert backends_real.known_vendors() == {"ibm", "braket"}


def test_mock_gated_by_env(backends_real, backends_mock):
    # fixtures import fresh, so check each one independently below
    pass


def test_mock_hidden_without_env(backends_real):
    assert "mock" not in backends_real.known_vendors()


def test_mock_present_with_env(backends_mock):
    assert {"mock", "mock_busy"} <= backends_mock.known_vendors()


def test_missing_creds_reported(backends_real):
    ok, msg = backends_real.get_backend("ibm").credentials_present()
    # names the variables it wants, never a value
    assert not ok and "_QRMI_IBM_QRS_IAM_APIKEY" in msg


def test_creds_present_when_env_set(fresh):
    b = fresh(mock=False, env=IBM_CREDS)
    ok, msg = b.get_backend("ibm").credentials_present()
    assert ok and "ibm_kingston" in msg


def test_partial_creds_name_only_the_missing_variable(fresh):
    partial = dict(IBM_CREDS)
    del partial["ibm_kingston_QRMI_IBM_QRS_SERVICE_CRN"]
    b = fresh(mock=False, env=partial)
    ok, msg = b.get_backend("ibm").credentials_present()
    assert not ok
    assert "ibm_kingston_QRMI_IBM_QRS_SERVICE_CRN" in msg
    # the api key value must never be echoed back
    assert "supersecret" not in msg


def test_mock_needs_no_creds(backends_mock):
    ok, _ = backends_mock.get_backend("mock").credentials_present()
    assert ok


def test_backend_declares_options_and_scout_options_roundtrip():
    """A vendor backend declares CLI options and extracts them from args."""
    from flux_quantum.backends import get_backend
    import flux_quantum.backends.ibm  # ensure ibm registered

    ibm = get_backend("ibm")
    declared = []
    ibm.add_options(lambda name, **kw: declared.append(name))
    assert "--ibm-resource" in declared and "--ibm-type" in declared

    class _Args:
        ibm_resource = "ibm_kingston"
        ibm_type = None

    opts = ibm.scout_options(_Args())
    assert opts["resource"] == "ibm_kingston"
    assert opts["type"] == "qiskit-runtime-service"


def test_scout_options_infer_the_resource_from_the_environment(fresh):
    """One resource configured is unambiguous, so the id need not be repeated."""
    b = fresh(mock=False, env=IBM_CREDS)

    class _Args:
        ibm_resource = None
        ibm_type = None

    assert b.get_backend("ibm").scout_options(_Args())["resource"] == "ibm_kingston"


def test_job_environment_follows_the_qrmi_convention(fresh):
    """The classical job gets the same variables Slurm and LSF set, so user
    code can call get_job_qpu_resources_and_types unchanged."""
    b = fresh(mock=False, env=IBM_CREDS)
    env = b.get_backend("ibm").job_environment(
        {"resource": "ibm_kingston", "type": "qiskit-runtime-service"}
    )
    assert env["QRMI_JOB_QPU_RESOURCES"] == "ibm_kingston"
    assert env["QRMI_JOB_QPU_TYPES"] == "qiskit-runtime-service"


def test_mock_open_session_uses_options(monkeypatch):
    monkeypatch.setenv("FLUX_QUANTUM_MOCK", "1")
    import importlib
    from flux_quantum.backends import mock as mockmod

    importlib.reload(mockmod)
    b = mockmod.MockBackend()
    # forced session id honored
    assert b.open_session({"session": "FORCED123"}) == "FORCED123"
    # otherwise a generated mock id
    assert b.open_session({}).startswith("mock-session-")


def test_open_session_without_a_resource_is_explicit():
    from flux_quantum.backends import get_backend
    import flux_quantum.backends.ibm  # noqa

    with __import__("pytest").raises(ValueError, match="no QRMI resource id"):
        get_backend("ibm").open_session({})


def test_open_session_names_the_missing_variable(fresh):
    partial = dict(IBM_CREDS)
    del partial["ibm_kingston_QRMI_IBM_QRS_IAM_APIKEY"]
    b = fresh(mock=False, env=partial)
    with __import__("pytest").raises(ValueError) as e:
        b.get_backend("ibm").open_session(
            {"resource": "ibm_kingston", "type": "qiskit-runtime-service"}
        )
    assert "ibm_kingston_QRMI_IBM_QRS_IAM_APIKEY" in str(e.value)
