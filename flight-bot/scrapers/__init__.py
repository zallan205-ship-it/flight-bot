from dataclasses import dataclass
from datetime import datetime


@dataclass
class ScrapedPrice:
    price_eur: float
    origin: str
    destination: str
    departure_date: datetime
    return_date: datetime | None
    source: str
    departure_time: str | None = None  # es. "19:35", orario reale del volo di andata
    return_time: str | None = None     # es. "20:10", orario reale del volo di ritorno


@dataclass
class FlightOption:
    """Una singola opzione di volo (senza scali) restituita da /cerca."""
    price_eur: float
    departure_time: str
    return_time: str | None
    airlines: list[str]
