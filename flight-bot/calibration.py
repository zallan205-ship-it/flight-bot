"""
Auto-calibrazione della finestra ottimale d'acquisto per ciascun periodo rosso.

Logica: per ogni rotta + periodo rosso, guarda tutti i price_snapshot storici
delle ricerche i cui departure_date cade in quel periodo. Trova i prezzi minimi
osservati per ciascuna ricerca (cioè: qual è stato il prezzo più basso mai visto
per quel volo specifico) e a che `days_before_departure` sono stati rilevati.
La finestra calibrata è il range che copre la maggioranza di questi minimi
(percentile 25-75, per non farsi distorcere da un singolo outlier).

Finché il numero di campioni è sotto RedPeriod.min_samples_for_calibration,
si continuano a usare i default di config.py.
"""
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import RED_PERIODS, RedPeriod
from models import MonitoredSearch, PriceSnapshot, RedPeriodCalibration


def _find_red_period(date: datetime) -> RedPeriod | None:
    for period in RED_PERIODS:
        start = (period.month_start, period.day_start)
        end = (period.month_end, period.day_end)
        md = (date.month, date.day)
        if start <= end:
            if start <= md <= end:
                return period
        else:
            # periodo a cavallo d'anno, es. Natale 15/12 - 07/01
            if md >= start or md <= end:
                return period
    return None


def recalibrate(session: Session) -> None:
    """Da chiamare periodicamente (es. 1 volta a settimana) dallo scheduler."""
    searches = session.execute(
        select(MonitoredSearch).where(MonitoredSearch.red_period_name.is_not(None))
    ).scalars().all()

    # raggruppa per (periodo, origin, destination)
    groups: dict[tuple[str, str, str], list[int]] = defaultdict(list)  # -> lista di days_before al minimo prezzo

    for search in searches:
        snapshots = session.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.search_id == search.id)
            .order_by(PriceSnapshot.price_eur.asc())
        ).scalars().all()
        if not snapshots:
            continue
        cheapest = snapshots[0]
        key = (search.red_period_name, search.origin, search.destination)
        groups[key].append(cheapest.days_before_departure)

    for (period_name, origin, destination), days_list in groups.items():
        period_config = next((p for p in RED_PERIODS if p.name == period_name), None)
        if period_config is None:
            continue
        if len(days_list) < period_config.min_samples_for_calibration:
            continue  # non ancora abbastanza dati, resta sul default

        days_list.sort()
        n = len(days_list)
        p25 = days_list[int(n * 0.25)]
        p75 = days_list[min(int(n * 0.75), n - 1)]

        existing = session.execute(
            select(RedPeriodCalibration).where(
                RedPeriodCalibration.red_period_name == period_name,
                RedPeriodCalibration.origin == origin,
                RedPeriodCalibration.destination == destination,
            )
        ).scalar_one_or_none()

        if existing:
            existing.days_before_min = p25
            existing.days_before_max = p75
            existing.sample_count = n
        else:
            session.add(RedPeriodCalibration(
                red_period_name=period_name,
                origin=origin,
                destination=destination,
                days_before_min=p25,
                days_before_max=p75,
                sample_count=n,
            ))

    session.commit()


def get_optimal_window(session: Session, red_period_name: str, origin: str,
                        destination: str) -> tuple[int, int, bool]:
    """
    Ritorna (days_before_min, days_before_max, is_calibrated).
    is_calibrated=False significa che si sta usando il default di config.py.
    """
    calibration = session.execute(
        select(RedPeriodCalibration).where(
            RedPeriodCalibration.red_period_name == red_period_name,
            RedPeriodCalibration.origin == origin,
            RedPeriodCalibration.destination == destination,
        )
    ).scalar_one_or_none()

    if calibration:
        return calibration.days_before_min, calibration.days_before_max, True

    default = next((p for p in RED_PERIODS if p.name == red_period_name), None)
    if default is None:
        return 60, 100, False  # fallback generico
    return default.days_before_min, default.days_before_max, False
