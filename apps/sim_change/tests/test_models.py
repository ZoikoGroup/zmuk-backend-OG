from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.sim_change.models import (
    Direction, LineOperationLock, ProviderCommand, SimTypeChangeCase, SimTypeChangeEvent, StatusSource,
)

from .helpers import make_line, make_sim, make_user


def _case(line, **overrides):
    values = dict(owner=line.owner, line=line, direction=Direction.PSIM_TO_ESIM, from_type="psim", to_type="esim")
    values.update(overrides)
    return SimTypeChangeCase.objects.create(**values)


class CaseConstraintTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.line = make_line(self.user)

    def test_one_open_case_per_line(self):
        _case(self.line)
        with self.assertRaises(IntegrityError), transaction.atomic():
            _case(self.line)

    def test_new_case_allowed_after_previous_closed(self):
        _case(self.line, status=SimTypeChangeCase.Status.CANCELLED)
        _case(self.line, status=SimTypeChangeCase.Status.FAILED)
        _case(self.line)  # must not raise

    def test_reserved_sim_backs_one_open_case(self):
        sim = make_sim(esim=True)
        other_line = make_line(self.user)
        _case(self.line, new_sim=sim)
        with self.assertRaises(IntegrityError), transaction.atomic():
            _case(other_line, new_sim=sim)

    def test_from_and_to_type_must_differ(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            _case(self.line, from_type="psim", to_type="psim")

    def test_direction_from_types(self):
        self.assertEqual(Direction.for_types("psim", "esim"), Direction.PSIM_TO_ESIM)
        self.assertEqual(Direction.for_types("esim", "psim"), Direction.ESIM_TO_PSIM)
        with self.assertRaises(ValueError):
            Direction.for_types("esim", "esim")


class CommandConstraintTests(TestCase):
    def setUp(self):
        self.case = _case(make_line(make_user()))

    def _command(self, key, status=ProviderCommand.Status.PENDING):
        return ProviderCommand.objects.create(
            case=self.case, channel=ProviderCommand.Channel.MANUAL_AURIGA, idempotency_key=key, status=status,
        )

    def test_one_open_command_per_case(self):
        self._command("a")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._command("b", status=ProviderCommand.Status.UNKNOWN)

    def test_new_command_allowed_after_failed(self):
        self._command("a", status=ProviderCommand.Status.FAILED)
        self._command("b")  # must not raise

    def test_idempotency_key_unique(self):
        self._command("same", status=ProviderCommand.Status.FAILED)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._command("same", status=ProviderCommand.Status.CANCELLED)

    def test_only_operation_is_sim_card_swap(self):
        self.assertEqual(ProviderCommand.Operation.values, ["SIM_CARD_SWAP"])


class EventAppendOnlyTests(TestCase):
    def test_events_cannot_be_edited_or_deleted(self):
        case = _case(make_line(make_user()))
        event = SimTypeChangeEvent.objects.create(
            case=case, kind="case_opened", status_source=StatusSource.CUSTOMER, version=1,
        )
        event.reason = "changed"
        with self.assertRaises(ValueError):
            event.save()
        with self.assertRaises(ValueError):
            event.delete()


class LineLockConstraintTests(TestCase):
    def test_one_held_lock_per_line(self):
        line = make_line(make_user())
        LineOperationLock.objects.create(line=line, operation="sim_type_change", reference="a")
        with self.assertRaises(IntegrityError), transaction.atomic():
            LineOperationLock.objects.create(line=line, operation="suspension", reference="b")
