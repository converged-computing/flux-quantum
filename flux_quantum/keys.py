"""Names shared by the scout, the wrapper and the backends. Kept apart so the
wrapper, which runs in every classical job, imports no vendor SDK."""

# memo key carrying the session id to the classical job
SESSION_KEY = "quantum_session"

# QRMI reads the acquisition token from <resource> + this. The Slurm and LSF
# plugins set it, so a workload written for either runs here unchanged.
ACQUISITION_TOKEN = "_QRMI_JOB_ACQUISITION_TOKEN"
