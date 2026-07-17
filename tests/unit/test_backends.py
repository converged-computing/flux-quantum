def test_real_backends_registered(backends_real):
    assert backends_real.known_vendors() == {"ibm", "braket"}


def test_mock_gated_by_env(backends_real, backends_mock):
    # note: fixtures import fresh; check each independently below instead
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
