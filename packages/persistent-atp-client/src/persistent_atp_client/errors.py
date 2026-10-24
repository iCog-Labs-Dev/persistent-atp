"""Safe client errors; a transport failure is never proof failure."""
from __future__ import annotations


class ClientError(Exception):
    code = "CLIENT_ERROR"
    retryable = False

    def __init__(self, message: str = "", *, request_id: str | None = None, operation_ref=None):
        super().__init__(message or self.code)
        self.request_id = request_id
        self.operation_ref = operation_ref


class InvalidRequest(ClientError): code = "INVALID_REQUEST"
class UnsupportedContract(ClientError): code = "UNSUPPORTED_CONTRACT"
class UnsupportedCapability(ClientError): code = "UNSUPPORTED_CAPABILITY"
class Unauthenticated(ClientError): code = "UNAUTHENTICATED"
class Forbidden(ClientError): code = "FORBIDDEN"
class IdempotencyConflict(ClientError): code = "IDEMPOTENCY_CONFLICT"
class RequestIdExpired(ClientError): code = "REQUEST_ID_EXPIRED"
class StalePrecondition(ClientError): code = "STALE_PRECONDITION"
class ArtifactUnavailable(ClientError): code = "ARTIFACT_UNAVAILABLE"
class HashMismatch(ClientError): code = "HASH_MISMATCH"
class BackendUnavailable(ClientError): code = "BACKEND_UNAVAILABLE"
class QuotaExceeded(ClientError): code = "QUOTA_EXCEEDED"
class ReviewRequired(ClientError): code = "REVIEW_REQUIRED"
class RateLimited(ClientError): code = "RATE_LIMITED"; retryable = True
class TransientUnavailable(ClientError): code = "TRANSIENT_UNAVAILABLE"; retryable = True
class SubmissionUncertain(ClientError): code = "SUBMISSION_UNCERTAIN"; retryable = True
class ObservationTimeout(ClientError): code = "OBSERVATION_TIMEOUT"
class CursorExpired(ClientError): code = "CURSOR_EXPIRED"


class AdmissionRejected(ClientError):
    code = "ADMISSION_REJECTED"

    def __init__(self, admission):
        super().__init__(admission.error_code or self.code, request_id=admission.request_id)
        self.admission = admission
