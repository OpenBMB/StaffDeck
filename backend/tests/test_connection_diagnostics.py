import errno
import ssl

from app.llm.connection_diagnostics import connection_diagnostic, provider_error_message


def test_connection_causes_are_classified_without_exposing_credentials():
    exc = ConnectionError("Connection error; api_key=secret-example Authorization=Bearer-private")
    exc.__cause__ = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
    info = connection_diagnostic(exc)
    assert info["connection_code"] == "MODEL_CONNECTION_REFUSED"
    message = provider_error_message(exc)
    assert "secret-example" not in message and "Bearer-private" not in message
    assert "ConnectionRefusedError" in message


def test_tls_failure_is_not_misreported_as_bad_api_key():
    exc = ConnectionError("Connection error")
    exc.__cause__ = ssl.SSLCertVerificationError(1, "certificate invalid")
    assert connection_diagnostic(exc)["connection_code"] == "MODEL_TLS_FAILED"


def test_cyclic_exception_chain_is_bounded():
    exc = ConnectionError("failed")
    exc.__cause__ = exc
    assert connection_diagnostic(exc)["cause_types"] == ["ConnectionError"]
