"""
Calcolo delle baseline storiche usate per giudicare se un prezzo è conveniente,
non solo "sceso rispetto a prima".

Due baseline distinte, per lo stesso motivo per cui teniamo separata la
calibrazione dei periodi rossi in calibration.py: mescolare prezzi di alta e
bassa stagione produce medie e minimi fuorvianti (l'alta stagione gonfia la
media, la bassa stagione stabilisce un minimo irraggiungibile fuori da quel
periodo). Quindi:

- baseline "periodo normale": prezzi della stessa rotta, ultimi N mesi,
  ESCLUDENDO i voli che cadono in un periodo rosso
- baseline "periodo rosso": prezzi di voli della stessa rotta che cadono nello
  STESSO periodo rosso (es. solo altri Natali), nelle ultime N edizioni/anni
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import (
    NORMAL_BASELINE_MONTHS, RED_PERIOD_BASELINE_YEARS,
    MIN_SAMPLES_FOR_BASELINE, GOOD_PRICE_MARGIN_PCT,
)
from models import MonitoredSearch, PriceSnapshot


class PriceTier(Enum):
    INSUFFICIENT_DATA = "insufficient_data"
    RECORD_LOW = "record_low"       # nuovo minimo storico
    GOOD_PRICE = "good_price"       # vicino al minimo, buon prezzo
    IN_LINE = "in_line"             # nella media
    ABOVE_AVERAGE = "above_average"  # sopra la media


@dataclass
class Baseline:
    tier: PriceTier
    sample_count: int
    avg_price: float | None = None
    min_price: float | None = None
    is_red_period: bool = False
    red_period_name: str | None = None


def _judge(price: float, avg_price: float, min_price: float) -> PriceTier:
    if price <= min_price:
        return PriceTier.RECORD_LOW
    if price <= min_price * (1 + GOOD_PRICE_MARGIN_PCT / 100):
        return PriceTier.GOOD_PRICE
    if price <= avg_price:
        return PriceTier.IN_LINE
    return PriceTier.ABOVE_AVERAGE


def get_baseline(session: Session, origin: str, destination: str, price: float,
                  red_period_name: str | None) -> Baseline:
    """
    Punto di ingresso unico: sceglie automaticamente la baseline giusta
    (periodo rosso o periodo normale) in base a se il volo valutato cade
    in un periodo rosso.
    """
    if red_period_name:
        return _red_period_baseline(session, origin, destination, price, red_period_name)
    return _normal_baseline(session, origin, destination, price)


def _normal_baseline(session: Session, origin: str, destination: str, price: float) -> Baseline:
    cutoff = datetime.now() - timedelta(days=NORMAL_BASELINE_MONTHS * 30)

    rows = session.execute(
        select(PriceSnapshot.price_eur)
        .join(MonitoredSearch, PriceSnapshot.search_id == MonitoredSearch.id)
        .where(
            MonitoredSearch.origin == origin,
            MonitoredSearch.destination == destination,
            MonitoredSearch.red_period_name.is_(None),
            PriceSnapshot.scraped_at >= cutoff,
        )
    ).scalars().all()

    return _build_baseline(rows, price, is_red_period=False)


def _red_period_baseline(session: Session, origin: str, destination: str, price: float,
                          red_period_name: str) -> Baseline:
    cutoff = datetime.now() - timedelta(days=RED_PERIOD_BASELINE_YEARS * 365)

    rows = session.execute(
        select(PriceSnapshot.price_eur)
        .join(MonitoredSearch, PriceSnapshot.search_id == MonitoredSearch.id)
        .where(
            MonitoredSearch.origin == origin,
            MonitoredSearch.destination == destination,
            MonitoredSearch.red_period_name == red_period_name,
            MonitoredSearch.departure_date >= cutoff,
        )
    ).scalars().all()

    return _build_baseline(rows, price, is_red_period=True, red_period_name=red_period_name)


def _build_baseline(rows: list[float], price: float, is_red_period: bool,
                     red_period_name: str | None = None) -> Baseline:
    count = len(rows)
    if count < MIN_SAMPLES_FOR_BASELINE:
        return Baseline(
            tier=PriceTier.INSUFFICIENT_DATA, sample_count=count,
            is_red_period=is_red_period, red_period_name=red_period_name,
        )

    avg_price = sum(rows) / count
    min_price = min(rows)
    tier = _judge(price, avg_price, min_price)

    return Baseline(
        tier=tier, sample_count=count, avg_price=avg_price, min_price=min_price,
        is_red_period=is_red_period, red_period_name=red_period_name,
    )
