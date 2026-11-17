import re
from datetime import date

from django.db import transaction
from rest_framework import serializers

from .models import MAX_NOMINEES, Nominee, PublicSectorRegistration

# Same rules as the frontend form
NAME_RE = re.compile(r"^[a-zA-Z\s'\-.]+$")
PHONE_RE = re.compile(r"^\+?[\d\s\-()]{7,20}$")


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def digits_only(value):
    return re.sub(r"\D", "", value or "")


def clean_name(value, label="Full name"):
    value = value.strip()
    if len(value) < 2:
        raise serializers.ValidationError(f"{label} must be at least 2 characters.")
    if not NAME_RE.match(value):
        raise serializers.ValidationError(
            f"{label} can only contain letters, spaces, hyphens, and apostrophes."
        )
    return value


def clean_phone(value):
    value = value.strip()
    if not PHONE_RE.match(value):
        raise serializers.ValidationError(
            "Phone number can only contain digits, spaces, +, - and brackets."
        )
    if not 10 <= len(digits_only(value)) <= 14:
        raise serializers.ValidationError("Phone number must contain 10 to 14 digits.")
    return value


def clean_text(value, label):
    value = value.strip()
    if len(value) < 2:
        raise serializers.ValidationError(f"{label} must be at least 2 characters.")
    return value


def nominee_conflicts(nominee, own_emails, own_phone, seen_emails, seen_phones):
    """Errors for one nominee that clashes with the applicant or another nominee.

    `seen_emails` / `seen_phones` hold the nominees already added (emails
    lower-cased, phones as digits only).
    """
    errors = {}
    email = nominee["email"]
    phone = digits_only(nominee["phone"])

    if email in own_emails:
        errors["email"] = ["A nominee's email can't be the same as your own."]
    elif email in seen_emails:
        errors["email"] = ["This email has already been added for another nominee."]

    if phone == own_phone:
        errors["phone"] = ["A nominee's phone number can't be the same as your own."]
    elif phone in seen_phones:
        errors["phone"] = ["This phone number has already been added for another nominee."]

    return errors


# ─── NOMINEE ─────────────────────────────────────────────────────────────────

class NomineeSerializer(serializers.ModelSerializer):

    class Meta:
        model = Nominee
        fields = ["id", "full_name", "email", "phone", "relationship", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_full_name(self, value):
        return clean_name(value)

    def validate_email(self, value):
        return value.strip().lower()

    def validate_phone(self, value):
        return clean_phone(value)

    def validate_relationship(self, value):
        return clean_text(value, "Relationship")


# ─── REGISTRATION ────────────────────────────────────────────────────────────

class PublicSectorRegistrationSerializer(serializers.ModelSerializer):

    nominees = NomineeSerializer(many=True, required=False)

    class Meta:
        model = PublicSectorRegistration
        fields = [
            "id",
            "full_name",
            "email",
            "phone",
            "date_of_birth",
            "employer_name",
            "job_title",
            "work_email",
            "department",
            "agree_terms",
            "is_public_sector_worker",
            "confirm_accurate",
            "nominees",
            "status",
            "created_at",
        ]
        # email_sent is deliberately excluded so clients can't set it
        read_only_fields = ["id", "status", "created_at"]
        # Model defaults make these optional in DRF; force them to be sent
        extra_kwargs = {
            "agree_terms": {"required": True},
            "is_public_sector_worker": {"required": True},
            "confirm_accurate": {"required": True},
        }

    # ── Field validation ──

    def validate_full_name(self, value):
        return clean_name(value)

    def validate_email(self, value):
        return value.strip().lower()

    def validate_work_email(self, value):
        return value.strip().lower()

    def validate_phone(self, value):
        return clean_phone(value)

    def validate_employer_name(self, value):
        return clean_text(value, "Employer's name")

    def validate_job_title(self, value):
        return clean_text(value, "Job title")

    def validate_department(self, value):
        return clean_text(value, "Department name")

    def validate_date_of_birth(self, value):
        today = date.today()
        if value > today:
            raise serializers.ValidationError("Date of birth cannot be in the future.")
        age = today.year - value.year - (
            (today.month, today.day) < (value.month, value.day)
        )
        if age < 18:
            raise serializers.ValidationError("You must be at least 18 years old to register.")
        if age > 100:
            raise serializers.ValidationError("Enter a valid date of birth.")
        return value

    def validate_agree_terms(self, value):
        if value is not True:
            raise serializers.ValidationError(
                "You must agree to the terms and conditions to register."
            )
        return value

    def validate_is_public_sector_worker(self, value):
        if value is not True:
            raise serializers.ValidationError(
                "This offer is only available to public sector workers."
            )
        return value

    def validate_confirm_accurate(self, value):
        if value is not True:
            raise serializers.ValidationError(
                "Please confirm that the information provided is accurate."
            )
        return value

    # ── Cross-field validation ──

    def validate(self, attrs):
        errors = {}

        email = attrs.get("email")
        work_email = attrs.get("work_email")
        own_phone = digits_only(attrs.get("phone"))

        if email and work_email and email == work_email:
            errors["work_email"] = ["Work email must be different from your personal email."]

        nominees = attrs.get("nominees", [])

        if len(nominees) > MAX_NOMINEES:
            errors["nominees"] = [f"You can nominate up to {MAX_NOMINEES} people."]
        else:
            own_emails = {email, work_email}
            seen_emails, seen_phones = set(), set()
            nominee_errors = []

            for nominee in nominees:
                nominee_errors.append(
                    nominee_conflicts(nominee, own_emails, own_phone, seen_emails, seen_phones)
                )
                seen_emails.add(nominee["email"])
                seen_phones.add(digits_only(nominee["phone"]))

            # Same shape DRF uses for nested lists: one dict per nominee, in order
            if any(nominee_errors):
                errors["nominees"] = nominee_errors

        if errors:
            raise serializers.ValidationError(errors)

        return attrs

    # ── Save registration + nominees together ──

    @transaction.atomic
    def create(self, validated_data):
        nominees_data = validated_data.pop("nominees", [])

        registration = PublicSectorRegistration.objects.create(**validated_data)

        Nominee.objects.bulk_create(
            [Nominee(registration=registration, **n) for n in nominees_data]
        )

        return registration