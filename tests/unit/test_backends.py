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
    assert not ok and "QISKIT_IBM_TOKEN" in msg


def test_creds_present_when_env_set(fresh):
    b = fresh(mock=False, env={"QISKIT_IBM_TOKEN": "x"})
    ok, msg = b.get_backend("ibm").credentials_present()
    assert ok


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
    assert "--ibm-backend" in declared and "--ibm-shots" in declared

    class _Args:
        ibm_backend = "ibm_brisbane"
        ibm_instance = None
        ibm_shots = "1024"

    opts = ibm.scout_options(_Args())
    assert opts["backend"] == "ibm_brisbane" and opts["shots"] == "1024"


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


def test_ibm_open_session_deferred_is_explicit():
    from flux_quantum.backends import get_backend
    import flux_quantum.backends.ibm  # noqa

    with __import__("pytest").raises(NotImplementedError):
        get_backend("ibm").open_session({"backend": "x"})
