"""
Scheduler principale (APScheduler). Tre job:

1. discover_new_weekends (giornaliero): genera i MonitoredSearch per i prossimi
   weekend PSA-CAG/CAG-PSA man mano che rientrano nell'orizzonte di tracking.
   Questo è anche il punto in cui, appena una compagnia "rilascia" un volo per
   una nuova data (cioè appena Google Flights/eDreams iniziano a restituire un
   prezzo invece di "nessun volo trovato"), la ricerca passa da assente a
   monitorata: non serve un job separato, basta creare comunque la riga
   MonitoredSearch e lasciare che il primo scrape la trovi "vuota" finché il
   volo non è ancora pubblicato, poi valorizzata appena esce.
2. run_scraping_cycle (ogni SCRAPE_INTERVAL_MINUTES_*): per ogni ricerca attiva,
   interroga entrambi gli scraper, salva gli snapshot, valuta gli alert.
3. weekly_recalibration (settimanale): richiama calibration.recalibrate.
"""
import asyncio
import logging
import random
from datetime import datetime, timedelta

import primp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot

from config import (
    AUTO_ROUTES, WEEKEND_DEPARTURE_WEEKDAY, WEEKEND_DEPARTURE_MIN_HOUR,
    WEEKEND_RETURN_WEEKDAY, WEEKEND_RETURN_MIN_HOUR, WEEKENDS_HORIZON_TO_TRACK,
    SCRAPE_INTERVAL_MINUTES_AUTO, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
)
from db import SessionLocal
from models import MonitoredSearch, PriceSnapshot, MonitorType, build_flight_key
from scrapers.google_flights import scrape_price as scrape_google, ScraperError
from alerts import maybe_send_alert
from calibration import recalibrate, _find_red_period

logger = logging.getLogger("flight_bot.scheduler")

# --- Retry degli errori tecnici TEMPORANEI dello scraper ----------------------------
# La decisione "questo errore va ritentato?" e' una policy applicativa: sta qui, non
# nello scraper (che si limita a sollevare ScraperError).

# Attesa prima del 2° e del 3° tentativo: al massimo 2 retry dopo il tentativo iniziale.
SCRAPE_RETRY_BASE_DELAYS_SECONDS = (60, 180)
SCRAPE_RETRY_MAX_DELAY_SECONDS = 180     # tetto, jitter compreso
SCRAPE_RETRY_JITTER = 0.20               # ±20% sull'attesa base

# Alert amministrativo al N-esimo fallimento DEFINITIVO consecutivo della stessa ricerca.
ADMIN_ALERT_FAILURE_THRESHOLD = 3

# Fallimenti definitivi consecutivi per ricerca (chiave: MonitoredSearch.id). Solo in
# memoria, per scelta: si azzera al riavvio del processo. Nessuna persistenza su DB.
_consecutive_failures: dict[int, int] = {}


def _is_retryable(error: ScraperError) -> bool:
    """
    True solo per errori tecnici verosimilmente TEMPORANEI: timeout, HTTP 429, HTTP 5xx.
    Tutto il resto NON si ritenta: HTTP 403 e altri 4xx, FlightsNotFound (risposta di
    errore di Google), errori del parser, errori strutturali del consenso, errori di
    connessione/DNS non di timeout, cause sconosciute.

    La classificazione guarda il TIPO della causa originale (`__cause__`), mai il testo
    del messaggio. Lo status HTTP e' `args[0]` di primp.StatusError (int): layout
    osservato con primp 2.0.1, non documentato. Se non e' un int NON si ritenta
    (scelta prudente) e lo si segnala nei log.
    """
    cause = error.__cause__
    if isinstance(cause, primp.TimeoutError):      # include DNSTimeoutError
        return True
    if isinstance(cause, primp.StatusError):
        status = cause.args[0] if cause.args else None
        if not isinstance(status, int):
            logger.warning("primp.StatusError senza status HTTP leggibile (args=%r): "
                           "nessun retry", cause.args)
            return False
        return status == 429 or 500 <= status <= 599
    return False


def _retry_delay(retry_index: int) -> float:
    """Attesa (secondi) prima del retry `retry_index` (0 = primo): base ±jitter, con tetto."""
    base = SCRAPE_RETRY_BASE_DELAYS_SECONDS[retry_index]
    jittered = base * random.uniform(1 - SCRAPE_RETRY_JITTER, 1 + SCRAPE_RETRY_JITTER)
    return min(SCRAPE_RETRY_MAX_DELAY_SECONDS, jittered)


async def _sleep(seconds: float) -> None:
    """Attesa NON bloccante (cede l'event loop al bot). Punto unico da mockare nei test."""
    await asyncio.sleep(seconds)


async def _scrape_with_retry(search: MonitoredSearch, **scrape_kwargs):
    """
    Esegue lo scraping di UNA ricerca: tentativo iniziale piu' al massimo
    len(SCRAPE_RETRY_BASE_DELAYS_SECONDS) retry, solo per errori ritentabili.
    Con un errore non ritentabile, o esauriti i retry, rilancia l'ultimo ScraperError.
    Un risultato valido (anche "nessun volo" = None) ritorna subito: nessun retry.
    """
    max_retries = len(SCRAPE_RETRY_BASE_DELAYS_SECONDS)
    retries_done = 0
    while True:
        try:
            return scrape_google(search.origin, search.destination,
                                 search.departure_date, search.return_date, **scrape_kwargs)
        except ScraperError as error:
            if retries_done >= max_retries or not _is_retryable(error):
                raise
            delay = _retry_delay(retries_done)
            retries_done += 1
            logger.warning(
                "Errore tecnico temporaneo per search_id=%s (%s); retry %d/%d tra %.0fs",
                search.id, error, retries_done, max_retries, delay,
            )
            await _sleep(delay)


def _record_failure(search_id: int) -> int:
    """Registra un fallimento DEFINITIVO della ricerca; ritorna il conteggio consecutivo."""
    _consecutive_failures[search_id] = _consecutive_failures.get(search_id, 0) + 1
    return _consecutive_failures[search_id]


def _record_success(search_id: int) -> None:
    """La ricerca e' tornata a rispondere (anche con 'nessun volo'): azzera il conteggio."""
    _consecutive_failures.pop(search_id, None)


async def _send_admin_alert(bot: Bot, search: MonitoredSearch, failures: int) -> None:
    """
    Alert amministrativo (best effort): solo contesto della ricerca, NESSUN dettaglio
    tecnico (quelli restano nei log). Un errore nell'invio non deve interrompere il ciclo.
    """
    dates = search.departure_date.strftime("%d/%m/%Y")
    if search.return_date:
        dates += " - " + search.return_date.strftime("%d/%m/%Y")
    text = (
        "⚠️ Scraper Google Flights\n\n"
        f"{failures} controlli consecutivi falliti per la ricerca "
        f"{search.origin} → {search.destination} ({dates}).\n\n"
        "Il monitoraggio continua automaticamente."
    )
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID,
                               message_thread_id=search.telegram_topic_id, text=text)
    except Exception:
        logger.exception("Invio dell'alert amministrativo fallito per search_id=%s", search.id)


def _next_weekend_dates(n_weekends: int) -> list[tuple[datetime, datetime]]:
    """Genera le prossime n coppie (partenza sabato sera, ritorno domenica sera)."""
    results = []
    today = datetime.now()
    days_until_saturday = (WEEKEND_DEPARTURE_WEEKDAY - today.weekday()) % 7
    first_saturday = today + timedelta(days=days_until_saturday)

    for i in range(n_weekends):
        saturday = first_saturday + timedelta(weeks=i)
        departure = saturday.replace(hour=WEEKEND_DEPARTURE_MIN_HOUR, minute=0, second=0, microsecond=0)
        sunday = saturday + timedelta(days=1)
        ret = sunday.replace(hour=WEEKEND_RETURN_MIN_HOUR, minute=0, second=0, microsecond=0)
        results.append((departure, ret))
    return results


def discover_new_weekends():
    session = SessionLocal()
    try:
        for departure, ret in _next_weekend_dates(WEEKENDS_HORIZON_TO_TRACK):
            red_period = _find_red_period(departure)
            for route in AUTO_ROUTES:
                exists = session.query(MonitoredSearch).filter_by(
                    origin=route["origin"],
                    destination=route["destination"],
                    departure_date=departure,
                    return_date=ret,
                    monitor_type=MonitorType.AUTO_WEEKEND,
                ).first()
                if exists:
                    continue
                new_search = MonitoredSearch(
                    origin=route["origin"],
                    destination=route["destination"],
                    departure_date=departure,
                    return_date=ret,
                    monitor_type=MonitorType.AUTO_WEEKEND,
                    telegram_topic_id=route["topic_id"],
                    red_period_name=red_period.name if red_period else None,
                )
                session.add(new_search)
                session.flush()  # assegna l'id senza fare un commit vero e proprio
                new_search.flight_key = build_flight_key(new_search)
        session.commit()
        logger.info("discover_new_weekends: sincronizzazione completata")
    finally:
        session.close()


async def run_scraping_cycle():
    session = SessionLocal()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        active_searches = session.query(MonitoredSearch).filter_by(active=True).all()
        for search in active_searches:
            days_before = (search.departure_date - datetime.now()).days
            if days_before < 0:
                continue  # volo nel passato, da disattivare (pulizia periodica separata)

            # Il filtro sull'orario vale in due casi: i weekend automatici
            # (fascia serale fissa da config) e i monitoraggi nati da /cerca
            # (fascia stretta attorno al volo scelto, salvata sulla singola
            # ricerca). I monitoraggi manuali "normali" (senza passare da
            # /cerca) non hanno nessun filtro, come da requisito originale.
            if search.monitor_type == MonitorType.AUTO_WEEKEND:
                earliest_departure_hour = WEEKEND_DEPARTURE_MIN_HOUR
                latest_departure_hour = None
                earliest_return_hour = WEEKEND_RETURN_MIN_HOUR if search.return_date else None
                latest_return_hour = None
                direct_only = True  # richiesto: i weekend automatici considerano solo voli senza scali
            else:
                earliest_departure_hour = search.earliest_departure_hour
                latest_departure_hour = search.latest_departure_hour
                earliest_return_hour = search.earliest_return_hour
                latest_return_hour = search.latest_return_hour
                direct_only = False

            try:
                result = await _scrape_with_retry(
                    search,
                    earliest_departure_hour=earliest_departure_hour,
                    latest_departure_hour=latest_departure_hour,
                    earliest_return_hour=earliest_return_hour,
                    latest_return_hour=latest_return_hour,
                    direct_only=direct_only,
                )
            except ScraperError:
                # Errore tecnico dello scraper (rete, HTTP, consenso, parsing,
                # risposta di errore di Google): NON e' "nessun volo". Si salta
                # solo QUESTA ricerca, con il dettaglio completo nei log
                # (traceback con causa originale): nessuno snapshot, nessun alert.
                logger.exception(
                    "Errore tecnico dello scraper per search_id=%s (%s, %s -> %s, "
                    "partenza %s, ritorno %s)",
                    search.id, search.flight_key, search.origin, search.destination,
                    search.departure_date, search.return_date,
                )
                # Fallimento DEFINITIVO (retry gia' esauriti o errore non ritentabile).
                failures = _record_failure(search.id)
                if failures == ADMIN_ALERT_FAILURE_THRESHOLD:   # una sola volta per serie
                    await _send_admin_alert(bot, search, failures)
                continue
            except Exception:
                logger.exception("Scraping fallito per search_id=%s", search.id)
                continue

            _record_success(search.id)   # risposta valida (anche "nessun volo"): azzera la serie

            if result is None:
                # Volo non ancora "rilasciato" dalla compagnia, o nessun risultato
                # nella fascia oraria richiesta.
                continue

            snapshot = PriceSnapshot(
                search_id=search.id,
                source="google_flights",
                price_eur=result.price_eur,
                days_before_departure=days_before,
                departure_time=result.departure_time,
                return_time=result.return_time,
            )
            session.add(snapshot)
            session.commit()

            await maybe_send_alert(session, bot, search, snapshot)
    finally:
        session.close()


def deactivate_expired_searches():
    """
    Disattiva (active=False) i monitoraggi il cui volo è ormai nel passato.
    Non li cancella dal DB: restano come storico utile per le baseline
    (baseline.py, calibration.py) anche dopo essere scaduti.
    """
    session = SessionLocal()
    try:
        reference_date = datetime.now()
        expired = session.query(MonitoredSearch).filter(
            MonitoredSearch.active == True,  # noqa: E712
            MonitoredSearch.departure_date < reference_date,
        ).all()
        for search in expired:
            search.active = False
        session.commit()
        if expired:
            logger.info("Disattivati %d monitoraggi scaduti", len(expired))
    finally:
        session.close()


def weekly_recalibration():
    session = SessionLocal()
    try:
        recalibrate(session)
        logger.info("Ricalibrazione periodi rossi completata")
    finally:
        session.close()


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(discover_new_weekends, "cron", hour=3, minute=0)
    scheduler.add_job(deactivate_expired_searches, "cron", hour=3, minute=30)
    scheduler.add_job(run_scraping_cycle, "interval", minutes=SCRAPE_INTERVAL_MINUTES_AUTO)
    scheduler.add_job(weekly_recalibration, "cron", day_of_week="mon", hour=4, minute=0)
    scheduler.start()
    return scheduler
