"""Bill model (Decimal, 2 dp, half-up). Itemised total equals the final bill total unless a defect is applied."""

from __future__ import annotations

import datetime as dt
import random
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from synth.entities import CATALOG

Q = Decimal("0.01")


def d(x: Any) -> Decimal:
    return Decimal(str(x)).quantize(Q, ROUND_HALF_UP)


def line(desc: str, qty: int, rate: Decimal, category: str, day: dt.date) -> dict[str, Any]:
    return {
        "description": desc,
        "qty": qty,
        "unit_price": d(rate),
        "amount": d(rate * qty),
        "category": category,
        "service_date": day,
    }


def hospital_lines(
    rng: random.Random, proc: dict[str, Any], admitted: dt.date, discharged: dt.date
) -> list[dict[str, Any]]:
    days = max(1, (discharged - admitted).days)
    t = CATALOG["tariffs"]
    out = [
        line(
            "Room rent general ward",
            days,
            d(rng.randrange(*t["room_per_day"], 100)),
            "room",
            admitted,
        )
    ]
    if proc["surgery"]:
        out.append(
            line("ICU charges", 1, d(rng.randrange(*t["icu_per_day"], 100)), "icu", admitted)
        )
        out.append(
            line(
                "OT charges operation theatre",
                1,
                d(rng.randrange(15000, 35000, 500)),
                "surgery",
                admitted,
            )
        )
        out.append(
            line("Surgeon fees", 1, d(rng.randrange(20000, 50000, 500)), "surgery", admitted)
        )
        out.append(
            line(
                "Anaesthesia charges",
                1,
                d(rng.randrange(6000, 15000, 500)),
                "anaesthesia",
                admitted,
            )
        )
    out.append(
        line("Consultation fees", days, d(rng.randrange(500, 1500, 50)), "consultation", admitted)
    )
    out.append(line("Blood test CBC", 2, d(rng.randrange(300, 700, 10)), "investigation", admitted))
    for c in rng.sample(CATALOG["consumables"], 3):
        out.append(
            line(c, rng.randrange(1, 6), d(rng.randrange(40, 400, 5)), "consumable", admitted)
        )
    if proc["implant"]:
        out.append(
            line(
                "Implant " + proc["code"].replace("_", " "),
                1,
                d(rng.randrange(*proc["price"], 1000) // 2),
                "implant",
                admitted,
            )
        )
    return out


def pharmacy_lines(rng: random.Random, n: int, day: dt.date) -> list[dict[str, Any]]:
    meds = CATALOG["medicines"]
    return [
        line(
            meds[i % len(meds)] + (f" #{i // len(meds) + 1}" if i >= len(meds) else ""),
            rng.randrange(1, 12),
            d(rng.randrange(8, 400)),
            "medicine",
            day,
        )
        for i in range(n)
    ]


def totals(lines: list[dict[str, Any]], discount: Decimal = Decimal(0)) -> dict[str, Decimal]:
    gross = sum((ln["amount"] for ln in lines), Decimal(0))
    return {"gross": gross, "discounts": d(discount), "claimed": gross - d(discount)}
