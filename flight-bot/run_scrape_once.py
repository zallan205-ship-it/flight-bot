"""
Forza un ciclo di scraping adesso, invece di aspettare che scatti da solo
(ogni 180 minuti). Utile per verificare che tutto funzioni end-to-end senza
aspettare ore. Scrive nel database e può mandare alert su Telegram esattamente
come farebbe il ciclo automatico.

Di default testa solo i primi LIMITE monitoraggi attivi, non tutti e 104 in
una volta — 104 richieste reali a Google tutte insieme, al primo vero test,
rischiano di farti rallentare/bloccare temporaneamente. Alza LIMITE (o mettilo
a None per "tutti") solo dopo aver verificato che i primi vanno bene.

Uso: python run_scrape_once.py
"""
import asyncio
import logging
from datetime import datetime

from db import SessionLocal
from models import MonitoredSearch
from config import WEEKEND_DEPARTURE_MIN_HOUR, WEEKEND_RETURN_MIN_HOUR
from models import MonitorType
from scrapers.google_flights import scrape_price as scrape_google
from alerts import maybe_send_alert
from telegram import Bot
from config import TELEGRAM_BOT_TOKEN

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

LIMITE = 3  # numero di monitoraggi da testare. Metti a None per farli tutti.


async def main():
    session = SessionLocal()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        query = session.query(MonitoredSearch).filter_by(active=True)
        searches = query.limit(LIMITE).all() if LIMITE else query.all()

        print(f"Testo {len(searches)} monitoraggi...\n")

        for search in searches:
            days_before = (search.departure_date - datetime.now()).days
            print(f"- {search.flight_key}: {search.origin} -> {search.destination}, "
                  f"andata {search.departure_date:%d/%m/%Y}...")

            if search.monitor_type == MonitorType.AUTO_WEEKEND:
                earliest_departure_hour = WEEKEND_DEPARTURE_MIN_HOUR
                earliest_return_hour = WEEKEND_RETURN_MIN_HOUR if search.return_date else None
            else:
                earliest_departure_hour = None
                earliest_return_hour = None

            try:
                result = scrape_google(
                    search.origin, search.destination,
                    search.departure_date, search.return_date,
                    earliest_departure_hour=earliest_departure_hour,
                    earliest_return_hour=earliest_return_hour,
                )
            except Exception as e:
                print(f"  ERRORE: {e}")
                continue

            if result is None:
                print("  Nessun risultato (volo non ancora rilasciato, o nessun volo nella fascia oraria)")
                continue

            print(f"  Trovato: {result.price_eur}€ alle {result.departure_time}")

            from models import PriceSnapshot
            snapshot = PriceSnapshot(
                search_id=search.id, source="google_flights",
                price_eur=result.price_eur, days_before_departure=days_before,
                departure_time=result.departure_time, return_time=result.return_time,
            )
            session.add(snapshot)
            session.commit()

            await maybe_send_alert(session, bot, search, snapshot)

        print("\nFatto. Controlla il database con la query SQL di prima per vedere le righe salvate.")
    finally:
        session.close()


if __name__ == "__main__":
    asyncio.run(main())
