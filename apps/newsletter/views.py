from django.conf import settings
from django.core.mail import send_mail
from django.core.signing import BadSignature, loads as sign_loads
from django.db import IntegrityError
from django.shortcuts import redirect

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Subscriber
from .serializers import SubscriberSerializer


class SubscribeView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email")

        if not email:
            return Response({
                "status": False,
                "message": "Email is required."
            }, status=status.HTTP_400_BAD_REQUEST)

        # Normalise so "A@X.com" and "a@x.com" don't create two rows.
        email = email.strip().lower()

        try:
            subscriber, created = Subscriber.objects.get_or_create(email=email)

            # If subscriber exists but inactive → reactivate
            if not created and not subscriber.is_active:
                subscriber.is_active = True
                subscriber.save(update_fields=["is_active"])
                message = "Subscription reactivated successfully."
            elif not created:
                return Response({
                    "status": False,
                    "message": "This email is already subscribed."
                }, status=status.HTTP_400_BAD_REQUEST)
            else:
                message = "Successfully subscribed!"

            # Send confirmation email
            try:
                send_mail(
                    subject="Subscription Confirmation",
                    message=(
                        "Thank you for subscribing to Zoiko Mobile updates.\n\n"
                        "You'll get an email whenever we publish a new article."
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[subscriber.email],
                    fail_silently=True,
                )
            except Exception:
                pass

            return Response({
                "status": True,
                "message": message,
                "data": {
                    "email": subscriber.email,
                    "subscribed_at": subscriber.subscribed_at
                }
            }, status=status.HTTP_201_CREATED)

        except IntegrityError:
            return Response({
                "status": False,
                "message": "Subscription failed due to duplicate email."
            }, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            return Response({
                "status": False,
                "message": "Something went wrong.",
                "error": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class UnsubscribeView(APIView):
    """One-click unsubscribe from a signed link in the email footer.

    Required by UK PECR / GDPR: every marketing email must offer a working
    unsubscribe. This is also what Gmail's native "Unsubscribe" button calls.

    GET  — a human clicking the link in the footer. Redirects to the frontend.
    POST — Gmail/Outlook one-click (RFC 8058). Returns 200 with no body.

    The token is a Django signed payload, so no database token column is needed
    and the link cannot be tampered with. It is intentionally NOT time-limited:
    an expired unsubscribe link would be worse than no link at all.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def _deactivate(self, token):
        try:
            data = sign_loads(token, salt="newsletter.unsubscribe")
        except BadSignature:
            return None
        email = (data or {}).get("email")
        if not email:
            return None
        Subscriber.objects.filter(email=email).update(is_active=False)
        return email

    def get(self, request, token):
        email = self._deactivate(token)
        base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")

        if email is None:
            if base:
                return redirect(f"{base}/unsubscribe?status=invalid")
            return Response({
                "status": False,
                "message": "This unsubscribe link is invalid or has been altered."
            }, status=status.HTTP_400_BAD_REQUEST)

        if base:
            return redirect(f"{base}/unsubscribe?status=ok")
        return Response({
            "status": True,
            "message": f"{email} has been unsubscribed."
        }, status=status.HTTP_200_OK)

    def post(self, request, token):
        # RFC 8058 one-click. Mail clients expect a 2xx and ignore the body.
        self._deactivate(token)
        return Response(status=status.HTTP_200_OK)
