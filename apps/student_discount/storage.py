"""Private storage for student ID documents.

These files must never be reachable by URL. Locally they live under
PRIVATE_MEDIA_ROOT, which (unlike MEDIA_ROOT) is not served by /media/.
With USE_S3 they go to the "private/" prefix of AWS_PRIVATE_STORAGE_BUCKET_NAME
(falling back to the main bucket), never through the public custom domain.
Admins open them through an admin-only view that streams the file.
"""

import os
import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage


def private_document_storage():
    if getattr(settings, "USE_S3", False):
        from storages.backends.s3 import S3Storage

        return S3Storage(
            bucket_name=(
                getattr(settings, "AWS_PRIVATE_STORAGE_BUCKET_NAME", None)
                or settings.AWS_STORAGE_BUCKET_NAME
            ),
            location="private",
            default_acl="private",
            querystring_auth=True,
            custom_domain=None,
        )
    return FileSystemStorage(location=settings.PRIVATE_MEDIA_ROOT)


def student_document_path(instance, filename):
    # Random name: the original filename can contain personal details.
    ext = os.path.splitext(filename)[1].lower()[:10]
    return f"student-discount/{uuid.uuid4().hex}{ext}"
