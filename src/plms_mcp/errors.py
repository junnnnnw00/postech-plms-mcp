class PLMSError(Exception):
    code = "plms_error"


class AuthenticationRequired(PLMSError):
    code = "authentication_required"


class LoginFailed(PLMSError):
    code = "login_failed"


class MFARequired(PLMSError):
    code = "mfa_required"


class UnsafeURL(PLMSError):
    code = "unsafe_url"


class UpstreamError(PLMSError):
    code = "upstream_error"


class UnsupportedPage(PLMSError):
    code = "unsupported_page"
