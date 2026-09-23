"""Exceptions raised by the Transatel API integration."""


class TransatelError(Exception):
    """Base class for all Transatel integration errors."""


class TransatelNotConfigured(TransatelError):
    """Raised when client credentials are missing from settings/env."""


class TransatelAuthError(TransatelError):
    """Raised when an OAuth2 access token could not be obtained."""


class TransatelAPIError(TransatelError):
    """Raised when the API returns a non-2xx response.

    Mirrors the plugin's error handling: the human-readable message is
    pulled from the response body's ``detail`` or ``message`` field.
    """

    def __init__(self, message, status_code=None, payload=None, response=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
        self.response = response
