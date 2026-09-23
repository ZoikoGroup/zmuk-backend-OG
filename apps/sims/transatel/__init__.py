"""Transatel API integration.

A Python port of the WordPress `transatel-api-manager` plugin's Core layer:
OAuth2 client-credentials authentication with automatic token refresh
(``token_manager``), a thin authenticated HTTP client (``client``), and the
high-level subscriber operations used to activate a SIM against an order
(``service``).

Public entry point:

    from apps.sims.transatel import get_service
    service = get_service()
    service.activate_subscriber(serial_number, package_code, order_reference, ...)
"""

from .config import TransatelConfig, get_config
from .service import TransatelService, get_service
from .exceptions import (
    TransatelError,
    TransatelAuthError,
    TransatelAPIError,
    TransatelNotConfigured,
)

__all__ = [
    "TransatelConfig",
    "get_config",
    "TransatelService",
    "get_service",
    "TransatelError",
    "TransatelAuthError",
    "TransatelAPIError",
    "TransatelNotConfigured",
]
