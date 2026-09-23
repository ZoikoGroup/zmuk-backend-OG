"""Backfill `Sim.esim_qr_image` from an already-stored `esim_qr_data_url`.

For SIMs whose QR was fetched *before* MEDIA_ROOT/MEDIA_URL were configured,
`esim_qr_data_url` (the base64 text) is already saved but `esim_qr_image`
(the file) never got written. This re-saves the file from that existing
base64 data — no Transatel call needed, so it's safe to run repeatedly.

    python manage.py backfill_esim_qr_image
    python manage.py backfill_esim_qr_image --iccid 89443042333290956010
    python manage.py backfill_esim_qr_image --force   # also overwrite existing files
"""

from __future__ import annotations

import base64

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.sims.models import Sim


def _decode_png(data_url: str) -> bytes | None:
    if not data_url or "base64," not in data_url:
        return None
    try:
        return base64.b64decode(data_url.split("base64,", 1)[1])
    except Exception:
        return None


class Command(BaseCommand):
    help = "Backfill Sim.esim_qr_image from an already-stored esim_qr_data_url."

    def add_arguments(self, parser):
        parser.add_argument("--iccid", type=str, default="")
        parser.add_argument(
            "--force", action="store_true",
            help="Re-save even if esim_qr_image is already set.",
        )

    def handle(self, *args, **opts):
        qs = Sim.objects.exclude(esim_qr_data_url="").filter(
            Q(type_of_sim__icontains="euicc") | Q(type_of_sim__icontains="esim")
        )
        if not opts["force"]:
            qs = qs.filter(esim_qr_image="")
        if opts["iccid"]:
            qs = qs.filter(iccid=opts["iccid"])

        total = qs.count()
        if not total:
            self.stdout.write("Nothing to backfill.")
            return

        saved = skipped = 0
        for sim in qs.iterator():
            png = _decode_png(sim.esim_qr_data_url)
            if not png:
                self.stderr.write(self.style.WARNING(
                    f"{sim.iccid}: esim_qr_data_url present but not decodable — skipped"
                ))
                skipped += 1
                continue
            sim.esim_qr_image.save(f"{sim.iccid}.png", ContentFile(png), save=True)
            saved += 1
            self.stdout.write(self.style.SUCCESS(f"{sim.iccid}: image saved"))

        self.stdout.write(f"Done. saved={saved} skipped={skipped} of {total}.")
