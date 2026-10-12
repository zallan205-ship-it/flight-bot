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
from collections import Counter
from datetime import datetime, timedelta

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
#
# I retry sono per CICLO, non per ricerca: il ciclo controlla TUTTE le ricerche, poi
# ritenta -- tutte insieme -- SOLO quelle fallite con un errore temporaneo, dopo una
# sola attesa. Un'interruzione generalizzata costa quindi al piu' due attese per ciclo
# (~60 s + ~180 s), non due attese per ogni ricerca (con 104 ricerche: ore).

# Attesa prima del 1° e del 2° passaggio di retry: al massimo 2 retry per ciclo.
SCRAPE_RETRY_BASE_DELAYS_SECONDS = (60, 180)
SCRAPE_RETRY_MAX_DELAY_SECONDS = 180     # tetto, jitter compreso
SCRAPE_RETRY_JITTER = 0.20               # ±20% sull'attesa base

# Fallimenti consecutivi PER RICERCA (chiave: MonitoredSearch.id). Un fallimento e'
# DEFINITIVO: la ricerca non e' riuscita in un ciclo nemmeno dopo i retry. Tre tentativi
# nello stesso ciclo valgono 1 fallimento. Si azzera con qualsiasi risposta valida, anche
# "nessun volo". Solo in memoria, per scelta: si azzera al riavvio del processo.
_consecutive_failures: dict[int, int] = {}

# Una ricerca e' in "errore persistente" dopo ADMIN_ALERT_FAILURE_THRESHOLD fallimenti
# definitivi consecutivi. L'alert amministrativo e' UNO SOLO per ciclo, aggregato su tutte
# le ricerche in errore persistente, e non si ripete finche' lo scraper non "recupera".
ADMIN_ALERT_FAILURE_THRESHOLD = 3
ADMIN_ALERT_MAX_ROUTES = 5               # rotte elencate nel messaggio
# Stato GLOBALE dell'alert (distinto dai contatori per ricerca): True = alert gia' inviato
# per l'attuale serie di errori. Torna False solo quando NESSUNA ricerca e' piu' in errore
# persistente: se una parte delle ricerche recupera e le altre continuano a fallire,
# lo scraper non e' "recuperato" e non si invia un nuovo alert.
_admin_alert_sent = False


def _is_retryable(error: ScraperError) -> bool:
    """
    True solo per errori tecnici verosimilmente TEMPORANEI: timeout, HTTP 429, HTTP 5xx.
    Tutto il resto NON si ritenta: HTTP 403 e altri 4xx, FlightsNotFound (risposta di
    errore di Google), errori del parser, errori strutturali del consenso, errori di
    connessione/DNS non di timeout, errori senza informazioni.

    Usa solo gli attributi strutturati di ScraperError (`timed_out`, `status_code`):
    non guarda il testo del messaggio ne' i dettagli della libreria HTTP.
    """
    if error.timed_out:
        return True
    status = error.status_code
    return status is not None and (status == 429 or 500 <= status <= 599)


def _retry_delay(retry_index: int) -> float:
    """Attesa (secondi) prima del passaggio di retry `retry_index` (0 = primo): base ±jitter, con tetto."""
    base = SCRAPE_RETRY_BASE_DELAYS_SECONDS[retry_index]
    jittered = base * random.uniform(1 - SCRAPE_RETRY_JITTER, 1 + SCRAPE_RETRY_JITTER)
    return min(SCRAPE_RETRY_MAX_DELAY_SECONDS, jittered)


async def _sleep(seconds: float) -> None:
    """Attesa NON bloccante (cede l'event loop al bot). Punto unico da mockare nei test."""
    await asyncio.sleep(seconds)


def _record_failure(search_id: int) -> int:
    """Registra un fallimento DEFINITIVO della ricerca; ritorna il conteggio consecutivo."""
    _consecutive_failures[search_id] = _consecutive_failures.get(search_id, 0) + 1
    return _consecutive_failures[search_id]


def _record_success(search_id: int) -> None:
    """La ricerca e' tornata a rispondere (anche con 'nessun volo'): azzera il conteggio."""
    _consecutive_failures.pop(search_id, None)


def _forget_stale_failures(current_ids: set[int]) -> None:
    """Dimentica i contatori delle ricerche che non vengono piu' controllate (scadute, disattivate)."""
    for search_id in list(_consecutive_failures):
        if search_id not in current_ids:
            del _consecutive_failures[search_id]


def _admin_alert_text(chronic: list[MonitoredSearch]) -> str:
    """Testo dell'alert aggregato: numero di ricerche e riepilogo per rotta. Nessun dettaglio tecnico."""
    count = len(chronic)
    subject = "1 ricerca ha fallito" if count == 1 else f"{count} ricerche hanno fallito"
    routes = sorted(Counter((s.origin, s.destination) for s in chronic).items(),
                    key=lambda item: (-item[1], item[0]))
    shown = ", ".join(f"{origin} → {destination} ({n})"
                      for (origin, destination), n in routes[:ADMIN_ALERT_MAX_ROUTES])
    if len(routes) > ADMIN_ALERT_MAX_ROUTES:
        shown += f" e altre {len(routes) - ADMIN_ALERT_MAX_ROUTES} rotte"
    return (
        "⚠️ Scraper Google Flights\n\n"
        f"{subject} almeno {ADMIN_ALERT_FAILURE_THRESHOLD} controlli consecutivi.\n"
        f"Rotte: {shown}.\n\n"
        "Il monitoraggio continua automaticamente."
    )


async def _send_admin_alert(bot: Bot, text: str, searches_count: int) -> bool:
    """
    Invia l'alert amministrativo aggregato (best effort). Ritorna True se inviato.
    Un errore nell'invio non interrompe il ciclo: si registra nei log e si riprovera' al
    ciclo successivo (l'alert non risulta ancora inviato). Nessun message_thread_id: nei
    gruppi con topic il messaggio va nel topic generale.
    """
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    except Exception:
        logger.exception("Invio dell'alert amministrativo fallito (%d ricerche in errore)",
                         searches_count)
        return False
    return True


async def _update_admin_alert(session, bot: Bot, searches_by_id: dict) -> None:
    """A fine ciclo: un solo alert aggregato per serie di errori; rilancio solo a recupero completo."""
    global _admin_alert_sent
    chronic = [searches_by_id[search_id] for search_id, failures in _consecutive_failures.items()
               if failures >= ADMIN_ALERT_FAILURE_THRESHOLD and search_id in searches_by_id]
    if not chronic:
        _admin_alert_sent = False          # nessuna ricerca in errore persistente: ripristinato
        return
    if _admin_alert_sent:
        return
    # Prima si LEGGONO i dati delle ricerche (se gli oggetti sono scaduti dopo un rollback
    # servono query), poi si rilascia la connessione e SOLO DOPO si fa l'invio lento.
    text = _admin_alert_text(chronic)
    _release_connection(session)
    if await _send_admin_alert(bot, text, len(chronic)):
        _admin_alert_sent = True


def _release_connection(session) -> None:
    """
    Chiude la transazione aperta, cosi' la connessione torna al pool PRIMA di un'operazione
    lenta (scraping bloccante, attesa dei retry, invio dell'alert amministrativo). Senza
    modifiche pendenti e' un commit "a vuoto" (con expire_on_commit=False gli oggetti
    caricati restano utilizzabili); senza transazione in corso non acquisisce nulla.
    """
    session.commit()


def _recover_session(session) -> None:
    """
    Dopo un errore nel salvataggio di UNA ricerca: se la transazione e' stata invalidata
    (es. commit fallito) serve un rollback, altrimenti ogni operazione successiva solleva
    PendingRollbackError e fa fallire a cascata tutte le ricerche seguenti. Se la sessione
    e' ancora attiva (es. errore di Telegram dopo il commit) non si tocca nulla: un rollback
    farebbe scadere gli oggetti e li costringerebbe a rileggere dal DB.
    """
    try:
        if not session.is_active:
            session.rollback()
    except Exception:
        logger.exception("Rollback della sessione dopo un errore di salvataggio fallito")


def _scrape_search(session, search: MonitoredSearch):
    """UNA chiamata allo scraper per la ricerca (puo' sollevare ScraperError)."""
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

    # Legge ORA i dati della ricerca (potrebbe servire una rilettura dal DB se l'oggetto e'
    # scaduto) e SUBITO DOPO rilascia la connessione: la chiamata che segue e' sincrona e
    # puo' durare decine di secondi, non deve tenere occupata una connessione DB.
    origin, destination = search.origin, search.destination
    departure_date, return_date = search.departure_date, search.return_date
    _release_connection(session)

    return scrape_google(
        origin, destination,
        departure_date, return_date,
        earliest_departure_hour=earliest_departure_hour,
        latest_departure_hour=latest_departure_hour,
        earliest_return_hour=earliest_return_hour,
        latest_return_hour=latest_return_hour,
        direct_only=direct_only,
    )


async def _store_result(session, bot: Bot, search: MonitoredSearch, result) -> None:
    """Risposta valida: salva lo snapshot e invia l'eventuale alert di prezzo."""
    if result is None:
        # Volo non ancora "rilasciato" dalla compagnia, o nessun risultato
        # nella fascia oraria richiesta.
        return

    snapshot = PriceSnapshot(
        search_id=search.id,
        source="google_flights",
        price_eur=result.price_eur,
        days_before_departure=(search.departure_date - datetime.now()).days,
        departure_time=result.departure_time,
        return_time=result.return_time,
    )
    session.add(snapshot)
    session.commit()

    await maybe_send_alert(session, bot, search, snapshot)


def _log_definitive_failure(search: MonitoredSearch, error: ScraperError) -> None:
    """Fallimento DEFINITIVO del controllo: dettaglio completo nei log e +1 al contatore."""
    logger.error(
        "Errore tecnico dello scraper per search_id=%s (%s, %s -> %s, "
        "partenza %s, ritorno %s)",
        search.id, search.flight_key, search.origin, search.destination,
        search.departure_date, search.return_date,
        exc_info=error,     # traceback con la causa originale
    )
    _record_failure(search.id)


async def _run_pass(session, bot: Bot, searches: list[MonitoredSearch]):
    """
    Un passaggio: UNA chiamata allo scraper per ciascuna ricerca. Isola gli errori:
    una ricerca che fallisce non ferma le altre.
      - risposta valida (anche "nessun volo"): azzera il contatore, salva e avvisa;
      - ScraperError NON ritentabile: fallimento definitivo subito;
      - ScraperError ritentabile: non ancora un fallimento, ritorna (ricerca, errore);
      - qualunque altra eccezione: bug, si logga e si salta (non conta, non si ritenta).
    Anche il salvataggio/alert di UNA ricerca e' isolato: un errore li' (DB, Telegram) si
    logga con la ricerca interessata e non interrompe le altre ne' i retry in corso.
    """
    retryable: list[tuple[MonitoredSearch, ScraperError]] = []
    for search in searches:
        try:
            result = _scrape_search(session, search)
        except ScraperError as error:
            # Errore tecnico dello scraper: NON e' "nessun volo". Nessuno snapshot e
            # nessun alert di prezzo per questa ricerca.
            if _is_retryable(error):
                retryable.append((search, error))
            else:
                _log_definitive_failure(search, error)
            continue
        except Exception:
            logger.exception("Scraping fallito per search_id=%s", search.id)
            continue

        _record_success(search.id)   # risposta valida (anche "nessun volo"): azzera la serie
        # Letti ORA, finche' l'oggetto e' caricato: dopo un commit fallito SQLAlchemy fa scadere
        # tutti gli oggetti e rileggerli su una sessione inattiva solleverebbe PendingRollbackError
        # proprio nel gestore che deve isolare l'errore.
        label = (search.id, search.flight_key, search.origin, search.destination)
        try:
            await _store_result(session, bot, search, result)
        except Exception:
            logger.exception(
                "Salvataggio del risultato o alert di prezzo fallito per search_id=%s "
                "(%s, %s -> %s)", *label,
            )
            _recover_session(session)
    return retryable


async def run_scraping_cycle():
    session = SessionLocal()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        active_searches = session.query(MonitoredSearch).filter_by(active=True).all()
        # Chiude subito la transazione di lettura: la connessione torna al pool e non
        # resta "idle in transaction" durante lo scraping e le attese dei retry.
        # (expire_on_commit=False: gli oggetti caricati restano utilizzabili.)
        _release_connection(session)

        # Voli nel passato: da disattivare (pulizia periodica separata), non si controllano.
        to_check = [s for s in active_searches if (s.departure_date - datetime.now()).days >= 0]
        searches_by_id = {s.id: s for s in to_check}      # ids letti ora, a oggetti ancora caricati
        _forget_stale_failures(set(searches_by_id))

        # Primo passaggio su TUTTE le ricerche, poi al massimo 2 passaggi di retry sulle
        # sole ricerche fallite con un errore temporaneo (mai su quelle riuscite).
        retryable = await _run_pass(session, bot, to_check)
        for retry_index in range(len(SCRAPE_RETRY_BASE_DELAYS_SECONDS)):
            if not retryable:
                break
            delay = _retry_delay(retry_index)
            logger.warning(
                "%d ricerche su %d con errore tecnico temporaneo: retry %d/%d tra %.0fs",
                len(retryable), len(to_check), retry_index + 1,
                len(SCRAPE_RETRY_BASE_DELAYS_SECONDS), delay,
            )
            _release_connection(session)          # nessuna transazione aperta durante l'attesa
            await _sleep(delay)
            retryable = await _run_pass(session, bot, [s for s, _ in retryable])

        # Retry esauriti: fallimento definitivo (+1 al contatore, una volta per ciclo).
        for search, error in retryable:
            _log_definitive_failure(search, error)

        await _update_admin_alert(session, bot, searches_by_id)
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
