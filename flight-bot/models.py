"""
Schema del database (PostgreSQL su un provider cloud, es. Supabase/Neon/Railway).

Tabelle:
- monitored_search: ogni combinazione tratta+data+ora sotto monitoraggio,
  sia auto (weekend PSA-CAG/CAG-PSA) sia manuale.
- price_snapshot: ogni prezzo rilevato dallo scraping, con fonte e timestamp.
  È la tabella che alimenta sia gli alert sia la calibrazione.
- red_period_calibration: risultato del ricalcolo automatico della finestra
  ottimale d'acquisto per rotta+periodo rosso, una volta che c'è abbastanza storico.
- alert_sent: log degli alert già inviati, per evitare duplicati sullo stesso ribasso.
"""
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean, ForeignKey, Enum, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class MonitorType(PyEnum):
    AUTO_WEEKEND = "auto_weekend"
    MANUAL = "manual"


class MonitoredSearch(Base):
    __tablename__ = "monitored_search"

    id = Column(Integer, primary_key=True)
    # Chiave leggibile univoca, es. "PSACAG0491" (origine+destinazione+ID),
    # assegnata subito dopo l'inserimento (serve l'id, quindi non può essere
    # nota prima del primo salvataggio). Usata nei comandi /monitora <chiave>
    # e mostrata in ogni alert per poter identificare/promuovere un volo.
    flight_key = Column(String(32), unique=True, nullable=True)
    origin = Column(String(3), nullable=False)          # IATA, es. PSA
    destination = Column(String(3), nullable=False)      # IATA, es. CAG
    departure_date = Column(DateTime, nullable=False)    # data (+ora se rilevante) andata
    return_date = Column(DateTime, nullable=True)        # null = solo andata
    monitor_type = Column(Enum(MonitorType), nullable=False)
    telegram_topic_id = Column(Integer, nullable=False)
    active = Column(Boolean, default=True)
    # Se il volo rientra in un periodo rosso, salviamo quale (per calibrazione e alert).
    red_period_name = Column(String(64), nullable=True)
    # True per i monitoraggi manuali sempre, e per i voli AUTO_WEEKEND "promossi"
    # con /monitora <chiave>: abilita gli alert di RINCARO (non solo ribasso) e
    # sposta gli alert futuri sul topic dei monitoraggi manuali.
    full_monitoring = Column(Boolean, default=False)
    # Fascia oraria stretta, impostata solo per i monitoraggi nati da /cerca
    # (scelta di un volo specifico tra quelli mostrati coi bottoni): invece di
    # tracciare il prezzo più basso di tutta la giornata, si restringe alla
    # fascia attorno all'orario scelto. NULL = nessuna restrizione (comportamento
    # normale di un /monitora classico).
    earliest_departure_hour = Column(Integer, nullable=True)
    latest_departure_hour = Column(Integer, nullable=True)
    earliest_return_hour = Column(Integer, nullable=True)
    latest_return_hour = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    price_snapshots = relationship("PriceSnapshot", back_populates="search")

    __table_args__ = (
        UniqueConstraint(
            "origin", "destination", "departure_date", "return_date", "monitor_type",
            name="uq_monitored_search_route_dates"
        ),
    )


class PriceSnapshot(Base):
    __tablename__ = "price_snapshot"

    id = Column(Integer, primary_key=True)
    search_id = Column(Integer, ForeignKey("monitored_search.id"), nullable=False)
    source = Column(String(32), nullable=False, default="google_flights")
    price_eur = Column(Float, nullable=False)
    days_before_departure = Column(Integer, nullable=False)  # calcolato al momento dello snapshot
    departure_time = Column(String(5), nullable=True)  # es. "19:35", orario reale rilevato
    return_time = Column(String(5), nullable=True)      # es. "20:10", solo se andata+ritorno
    scraped_at = Column(DateTime, default=datetime.utcnow)

    search = relationship("MonitoredSearch", back_populates="price_snapshots")


class RedPeriodCalibration(Base):
    __tablename__ = "red_period_calibration"

    id = Column(Integer, primary_key=True)
    red_period_name = Column(String(64), nullable=False)
    origin = Column(String(3), nullable=False)
    destination = Column(String(3), nullable=False)
    days_before_min = Column(Integer, nullable=False)
    days_before_max = Column(Integer, nullable=False)
    sample_count = Column(Integer, nullable=False)
    last_calibrated_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "red_period_name", "origin", "destination",
            name="uq_calibration_period_route"
        ),
    )


class AlertSent(Base):
    __tablename__ = "alert_sent"

    id = Column(Integer, primary_key=True)
    search_id = Column(Integer, ForeignKey("monitored_search.id"), nullable=False)
    price_snapshot_id = Column(Integer, ForeignKey("price_snapshot.id"), nullable=False)
    sent_at = Column(DateTime, default=datetime.utcnow)


def build_flight_key(search: MonitoredSearch) -> str:
    """
    Genera la chiave leggibile univoca per un MonitoredSearch, es. "PSACAG0491".
    Richiede che `search.id` sia già stato assegnato dal DB (quindi va chiamata
    dopo un session.flush() o session.commit(), mai su un oggetto non ancora salvato).
    """
    if search.id is None:
        raise ValueError("build_flight_key richiede un MonitoredSearch già salvato (con id)")
    return f"{search.origin}{search.destination}{search.id:04d}"
