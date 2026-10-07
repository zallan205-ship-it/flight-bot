"""
Test della gestione degli errori tecnici dello scraper nel LIVELLO APPLICATIVO:
il comando Telegram /cerca (bot.py) e il ciclo di scraping (scheduler.py).

Contratto:

    risposta valida senza voli  -> normale caso "nessun volo" (invariato)
    ScraperError                -> problema tecnico:
        /cerca     : all'utente un messaggio GENERICO, nessun dettaglio tecnico;
                     il dettaglio completo (traceback con causa) resta nei log
        scheduler  : la singola ricerca viene saltata e loggata con contesto;
                     le altre ricerche continuano; nessuno snapshot, nessun alert
    altre eccezioni             -> comportamento preesistente, NON nascoste

Tutti i test sono offline: lo scraper, il database, il Bot Telegram e gli alert
sono mockati. Le coroutine si eseguono con asyncio.run (nessuna dipendenza
aggiuntiva, es. pytest-asyncio).
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import primp
import pytest
from fast_flights.exceptions import FlightsNotFound

# config.py legge queste variabili all'IMPORT (KeyError se mancano) e db.py crea
# l'engine all'import. Valori finti, impostati prima di importare bot/scheduler:
# i test non usano mai credenziali, token o database reali.
for _name, _value in {
    "TELEGRAM_BOT_TOKEN": "test-token-not-real",
    "TELEGRAM_CHAT_ID": "1",
    "TOPIC_PSA_CAG": "11",
    "TOPIC_CAG_PSA": "12",
    "TOPIC_MANUAL": "13",
    "DATABASE_URL": "sqlite://",
}.items():
    os.environ[_name] = _value

import bot  # noqa: E402
import scheduler  # noqa: E402
from models import MonitorType, PriceSnapshot  # noqa: E402
from scrapers import FlightOption, ScrapedPrice  # noqa: E402
from scrapers.google_flights import ScraperError  # noqa: E402


SEARCHING_MESSAGE = "Cerco voli senza scali, un momento..."
GENERIC_ERROR_MESSAGE = (
    "Non sono riuscito a recuperare i voli in questo momento. Riprova più tardi."
)

# Nessuno di questi deve mai arrivare all'utente Telegram (confronto case-insensitive).
FORBIDDEN_IN_USER_TEXT = (
    "ScraperError", "FlightsNotFound", "errorHasStatus", "Traceback",
    "primp", "HTTP", "parse", "timed out", "consenso", "Google Flights ha risposto",
)


def _scraper_error(message, cause, **attributes):
    """ScraperError REALE sollevata `from cause` (come fa lo scraper), non assemblata a mano."""
    try:
        try:
            raise cause
        except Exception as exc:
            raise ScraperError(message, **attributes) from exc
    except ScraperError as err:
        return err


TECHNICAL_ERRORS = {
    "flights_not_found": lambda: _scraper_error(
        "Google Flights ha risposto con un errore (FlightsNotFound / errorHasStatus: "
        "no flights found; received error); non e' una risposta valida senza voli",
        FlightsNotFound("no flights found; received error")),
    "http_503": lambda: _scraper_error(
        "Google ha risposto con errore HTTP 503 alla richiesta GET",
        primp.StatusError(503, "HTTP 503 Service Unavailable for URL: https://example.test/x",
                          "https://example.test/x"),
        status_code=503),
    "timeout": lambda: _scraper_error(
        "Richiesta GET a Google fallita (TimeoutError)",
        primp.TimeoutError("operation timed out"),
        timed_out=True),
    "parser": lambda: _scraper_error(
        "Analisi della pagina Google Flights fallita (AttributeError)",
        AttributeError("'NoneType' object has no attribute 'text'")),
}


@pytest.fixture(params=sorted(TECHNICAL_ERRORS))
def technical_error(request):
    return TECHNICAL_ERRORS[request.param]()


# ------------------------------------------------------------------ bot ----

def _update_and_context(args):
    reply = AsyncMock()
    update = SimpleNamespace(message=SimpleNamespace(reply_text=reply))
    context = SimpleNamespace(args=args)
    return update, context, reply


def _run_cerca(args, **search_options_mock):
    """Esegue bot.cerca con search_options mockata. Ritorna (reply_mock, search_options_mock)."""
    update, context, reply = _update_and_context(args)
    with patch.object(bot, "search_options", **search_options_mock) as mock_search:
        asyncio.run(bot.cerca(update, context))
    return reply, mock_search


def _texts(reply):
    return [call.args[0] for call in reply.call_args_list]


ONE_WAY_ARGS = ["psa", "cag", "25-12-2026"]
ROUND_TRIP_ARGS = ["psa", "cag", "25-12-2026", "31-12-2026"]


class TestCercaSuccessIsUnchanged:
    """A. /cerca senza errori: comportamento preesistente invariato."""

    def test_one_way_options_are_shown_with_buttons(self):
        options = [
            FlightOption(price_eur=50.0, departure_time="18:30", return_time=None, airlines=["Ryanair"]),
            FlightOption(price_eur=65.4, departure_time="07:05", return_time=None, airlines=["ITA Airways"]),
        ]

        reply, mock_search = _run_cerca(ONE_WAY_ARGS, return_value=options)

        mock_search.assert_called_once_with(
            "PSA", "CAG", datetime(2026, 12, 25), None, max_results=bot.SEARCH_MAX_OPTIONS)
        texts = _texts(reply)
        assert texts[0] == SEARCHING_MESSAGE
        assert texts[1] == "Voli senza scali PSA → CAG, 25/12/2026 — scegline uno da monitorare:"
        buttons = [row[0] for row in reply.call_args_list[1].kwargs["reply_markup"].inline_keyboard]
        assert [b.text for b in buttons] == ["18:30 · 50€", "07:05 · 65€"]
        assert [b.callback_data for b in buttons] == [
            "sel|PSA|CAG|20261225|18:30|-|-",
            "sel|PSA|CAG|20261225|07:05|-|-",
        ]

    def test_round_trip_options_show_both_times(self):
        options = [FlightOption(price_eur=120.0, departure_time="18:30", return_time="20:15",
                                airlines=["Ryanair", "Wizz Air"])]

        reply, mock_search = _run_cerca(ROUND_TRIP_ARGS, return_value=options)

        mock_search.assert_called_once_with(
            "PSA", "CAG", datetime(2026, 12, 25), datetime(2026, 12, 31),
            max_results=bot.SEARCH_MAX_OPTIONS)
        texts = _texts(reply)
        assert texts[1] == ("Voli senza scali PSA → CAG, 25/12/2026 - 31/12/2026 "
                            "— scegline uno da monitorare:")
        button = reply.call_args_list[1].kwargs["reply_markup"].inline_keyboard[0][0]
        assert button.text == "18:30 → 20:15 · 120€"
        assert button.callback_data == "sel|PSA|CAG|20261225|18:30|20261231|20:15"

    def test_valid_response_without_flights_keeps_the_no_flights_message(self, caplog):
        with caplog.at_level(logging.INFO, logger="flight_bot.bot"):
            reply, _ = _run_cerca(ONE_WAY_ARGS, return_value=[])

        texts = _texts(reply)
        assert texts[0] == SEARCHING_MESSAGE
        assert texts[1].startswith("Nessun volo senza scali trovato per questa data")
        assert texts[1] != GENERIC_ERROR_MESSAGE
        assert "reply_markup" not in reply.call_args_list[1].kwargs
        # Un risultato valido senza voli NON e' un errore: niente log di errore.
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


class TestCercaScraperError:
    """B / C. /cerca con ScraperError: messaggio generico all'utente, dettaglio nei log."""

    def test_error_is_not_propagated_and_user_gets_only_the_generic_message(self, technical_error):
        reply, _ = _run_cerca(ONE_WAY_ARGS, side_effect=technical_error)  # asyncio.run non solleva

        assert _texts(reply) == [SEARCHING_MESSAGE, GENERIC_ERROR_MESSAGE]
        assert "reply_markup" not in reply.call_args_list[1].kwargs

    def test_round_trip_error_is_handled_the_same_way(self, technical_error):
        reply, _ = _run_cerca(ROUND_TRIP_ARGS, side_effect=technical_error)

        assert _texts(reply) == [SEARCHING_MESSAGE, GENERIC_ERROR_MESSAGE]

    def test_no_technical_detail_reaches_the_user(self, technical_error):
        reply, _ = _run_cerca(ONE_WAY_ARGS, side_effect=technical_error)

        user_text = "\n".join(_texts(reply)).lower()
        for forbidden in FORBIDDEN_IN_USER_TEXT:
            assert forbidden.lower() not in user_text, forbidden
        assert str(technical_error).lower() not in user_text
        assert str(technical_error.__cause__).lower() not in user_text

    def test_error_is_not_reported_as_no_flights(self, technical_error):
        reply, _ = _run_cerca(ONE_WAY_ARGS, side_effect=technical_error)

        assert not any(t.startswith("Nessun volo") for t in _texts(reply))

    def test_technical_detail_is_logged_with_traceback_and_context(self, technical_error, caplog):
        with caplog.at_level(logging.INFO, logger="flight_bot.bot"):
            _run_cerca(ONE_WAY_ARGS, side_effect=technical_error)

        records = [r for r in caplog.records if r.name == "flight_bot.bot"]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.ERROR
        # traceback allegato e riferito proprio all'errore sollevato
        assert record.exc_info is not None and record.exc_info[1] is technical_error
        # contesto utile per diagnosticare: rotta e data
        assert "PSA" in record.getMessage() and "CAG" in record.getMessage()
        assert "2026-12-25" in record.getMessage()
        # il testo tecnico, e la causa originale, sono nel log formattato
        assert str(technical_error) in caplog.text
        assert str(technical_error.__cause__) in caplog.text


class TestCercaOtherErrorsAreNotHidden:
    """E. Gli errori NON previsti non vengono assorbiti dal messaggio generico."""

    @pytest.mark.parametrize("error", [ValueError("boom"), KeyError("chiave"), RuntimeError("boom")],
                             ids=["ValueError", "KeyError", "RuntimeError"])
    def test_unexpected_error_keeps_the_existing_behaviour(self, error):
        reply, _ = _run_cerca(ONE_WAY_ARGS, side_effect=error)

        texts = _texts(reply)
        assert texts[0] == SEARCHING_MESSAGE
        # comportamento preesistente (invariato in questo task): vedi report
        assert texts[1] == f"Errore durante la ricerca: {error}"
        assert texts[1] != GENERIC_ERROR_MESSAGE

    def test_plain_runtime_error_is_not_mistaken_for_scraper_error(self):
        """ScraperError estende RuntimeError: il contrario non deve valere."""
        assert issubclass(ScraperError, RuntimeError)
        reply, _ = _run_cerca(ONE_WAY_ARGS, side_effect=RuntimeError("altro problema"))

        assert _texts(reply)[1] == "Errore durante la ricerca: altro problema"


# ------------------------------------------------------------ scheduler ----

def _search(search_id, origin="PSA", destination="CAG", monitor_type=MonitorType.MANUAL):
    return SimpleNamespace(
        id=search_id,
        flight_key=f"{origin}{destination}{search_id:04d}",
        origin=origin,
        destination=destination,
        departure_date=datetime.now() + timedelta(days=30 + search_id),   # univoca per ricerca
        return_date=datetime.now() + timedelta(days=32 + search_id),
        monitor_type=monitor_type,
        telegram_topic_id=100 + search_id,
        earliest_departure_hour=None,
        latest_departure_hour=None,
        earliest_return_hour=None,
        latest_return_hour=None,
    )


def _price(price):
    return ScrapedPrice(
        price_eur=price, origin="PSA", destination="CAG",
        departure_date=datetime(2026, 12, 25), return_date=datetime(2026, 12, 31),
        source="google_flights", departure_time="18:30", return_time="20:15",
    )


def _run_cycle(searches, scrape_results):
    """
    Esegue scheduler.run_scraping_cycle con DB, Bot, scraper, alert e ATTESE mockati.
    `scrape_results`: un valore/eccezione per ricerca (stessa posizione di `searches`);
    l'esito vale per OGNI tentativo di quella ricerca (anche se lo scheduler ritenta).
    Ritorna (session, scrape_mock, alert_mock).
    """
    session = MagicMock()
    session.query.return_value.filter_by.return_value.all.return_value = searches
    alert = AsyncMock()
    outcome_by_departure = {s.departure_date: r for s, r in zip(searches, scrape_results)}

    def scrape_outcome(origin, destination, departure_date, return_date=None, **kwargs):
        outcome = outcome_by_departure[departure_date]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    with patch.object(scheduler, "SessionLocal", return_value=session), \
         patch.object(scheduler, "Bot"), \
         patch.object(scheduler, "_sleep", new=AsyncMock()), \
         patch.object(scheduler, "scrape_google", side_effect=scrape_outcome) as scrape, \
         patch.object(scheduler, "maybe_send_alert", alert):
        asyncio.run(scheduler.run_scraping_cycle())
    return session, scrape, alert


def _snapshot_commits(session):
    """
    Quanti snapshot sono stati salvati: ogni `add` deve essere seguito SUBITO da un
    `commit` (il ciclo fa anche altri commit senza dati: rilascio della transazione).
    """
    names = [call[0] for call in session.mock_calls]
    for i, name in enumerate(names):
        if name == "add":
            assert names[i + 1] == "commit", "snapshot aggiunto ma non committato"
    return sum(1 for i, name in enumerate(names) if name == "commit" and names[i - 1] == "add")


def _attempted_search_ids(scrape, searches):
    """Id delle ricerche per cui lo scraper e' stato chiamato almeno una volta (retry inclusi)."""
    by_departure = {s.departure_date: s.id for s in searches}
    return {by_departure[call.args[2]] for call in scrape.call_args_list}


def _snapshots(session):
    return [call.args[0] for call in session.add.call_args_list]


def _alerted_search_ids(alert):
    return [call.args[2].id for call in alert.call_args_list]


class TestSchedulerScraperError:
    """D. Una ricerca che fallisce con ScraperError non ferma il ciclo."""

    def test_error_in_the_middle_does_not_stop_the_other_searches(self, technical_error):
        searches = [_search(1), _search(2), _search(3)]

        session, scrape, alert = _run_cycle(searches, [_price(50.0), technical_error, _price(60.0)])

        assert _attempted_search_ids(scrape, searches) == {1, 2, 3}   # la terza ricerca viene eseguita
        snapshots = _snapshots(session)
        assert [(s.search_id, s.price_eur) for s in snapshots] == [(1, 50.0), (3, 60.0)]
        assert all(isinstance(s, PriceSnapshot) for s in snapshots)
        assert _snapshot_commits(session) == 2
        assert _alerted_search_ids(alert) == [1, 3]
        session.close.assert_called_once()

    def test_error_on_the_first_search_does_not_stop_the_cycle(self, technical_error):
        searches = [_search(1), _search(2)]
        session, scrape, alert = _run_cycle(searches, [technical_error, _price(70.0)])

        assert _attempted_search_ids(scrape, searches) == {1, 2}
        assert [(s.search_id, s.price_eur) for s in _snapshots(session)] == [(2, 70.0)]
        assert _alerted_search_ids(alert) == [2]

    def test_all_searches_failing_creates_no_data_and_sends_no_alert(self, technical_error):
        searches = [_search(1), _search(2)]
        session, scrape, alert = _run_cycle(
            searches, [technical_error, TECHNICAL_ERRORS["timeout"]()])

        assert _attempted_search_ids(scrape, searches) == {1, 2}
        session.add.assert_not_called()                     # nessun dato falso
        assert _snapshot_commits(session) == 0              # nessun commit di dati
        alert.assert_not_called()                           # nessun alert di prezzo
        session.close.assert_called_once()

    def test_error_is_logged_with_context_and_traceback(self, technical_error, caplog):
        searches = [_search(1), _search(2, origin="CAG", destination="PSA"), _search(3)]

        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            _run_cycle(searches, [_price(50.0), technical_error, _price(60.0)])

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1                             # solo la ricerca fallita
        record = errors[0]
        message = record.getMessage()
        assert record.name == "flight_bot.scheduler"
        assert "search_id=2" in message
        assert "CAGPSA0002" in message                      # flight_key della ricerca fallita
        assert "CAG" in message and "PSA" in message        # rotta
        assert record.exc_info is not None and record.exc_info[1] is technical_error
        assert str(technical_error) in caplog.text          # dettaglio tecnico nel log
        assert str(technical_error.__cause__) in caplog.text  # causa originale nel log

    def test_error_is_not_treated_as_no_flights(self, technical_error, caplog):
        """Valid empty (None) = silenzioso; ScraperError = errore registrato."""
        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            _run_cycle([_search(1), _search(2)], [None, technical_error])

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1 and "search_id=2" in errors[0].getMessage()

    def test_valid_no_flights_result_is_unchanged(self, caplog):
        """Risultato valido senza voli (None): nessun dato, nessun alert, nessun errore loggato."""
        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            session, scrape, alert = _run_cycle(
                [_search(1), _search(2), _search(3)], [_price(50.0), None, _price(60.0)])

        assert scrape.call_count == 3
        assert [s.search_id for s in _snapshots(session)] == [1, 3]
        assert _alerted_search_ids(alert) == [1, 3]
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_automatic_weekend_searches_are_isolated_too(self, technical_error):
        searches = [_search(1, monitor_type=MonitorType.AUTO_WEEKEND),
                    _search(2, origin="CAG", destination="PSA", monitor_type=MonitorType.AUTO_WEEKEND)]

        session, scrape, alert = _run_cycle(searches, [technical_error, _price(80.0)])

        assert [s.search_id for s in _snapshots(session)] == [2]
        assert _alerted_search_ids(alert) == [2]

    def test_past_flights_are_skipped_without_calling_the_scraper(self):
        """Comportamento preesistente del ciclo, non toccato dalla modifica."""
        past = _search(1)
        past.departure_date = datetime.now() - timedelta(days=2)

        # un esito per ricerca; quello della ricerca nel passato non viene mai usato
        session, scrape, alert = _run_cycle([past, _search(2)], [None, _price(55.0)])

        assert scrape.call_count == 1
        assert [s.search_id for s in _snapshots(session)] == [2]


class TestSchedulerOtherErrorsAreNotHidden:
    """E. Errori non previsti: comportamento preesistente (isolati per ricerca, loggati con traceback)."""

    @pytest.mark.parametrize("error", [ValueError("boom"), KeyError("chiave"), RuntimeError("boom")],
                             ids=["ValueError", "KeyError", "RuntimeError"])
    def test_unexpected_error_is_logged_with_traceback_and_cycle_continues(self, error, caplog):
        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            session, scrape, alert = _run_cycle(
                [_search(1), _search(2), _search(3)], [_price(50.0), error, _price(60.0)])

        assert scrape.call_count == 3
        assert [s.search_id for s in _snapshots(session)] == [1, 3]
        assert _alerted_search_ids(alert) == [1, 3]
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "search_id=2" in errors[0].getMessage()
        assert errors[0].exc_info is not None and errors[0].exc_info[1] is error
        # Un errore imprevisto NON va etichettato come errore tecnico dello scraper
        # (ScraperError estende RuntimeError: un RuntimeError qualsiasi non e' un ScraperError).
        assert "scraper" not in errors[0].getMessage().lower()

    def test_errors_in_the_alert_step_are_not_swallowed(self):
        """Il boundary per-ricerca copre SOLO lo scraping: un errore negli alert si propaga come prima."""
        session = MagicMock()
        session.query.return_value.filter_by.return_value.all.return_value = [_search(1)]
        alert = AsyncMock(side_effect=ValueError("errore negli alert"))

        with patch.object(scheduler, "SessionLocal", return_value=session), \
             patch.object(scheduler, "Bot"), \
             patch.object(scheduler, "scrape_google", side_effect=[_price(50.0)]), \
             patch.object(scheduler, "maybe_send_alert", alert):
            with pytest.raises(ValueError, match="errore negli alert"):
                asyncio.run(scheduler.run_scraping_cycle())

        session.close.assert_called_once()                  # la sessione si chiude comunque


# ------------------------------------------------ catena reale (solo rete mockata) ---

# Pagina con payload di errore di Google: il parser reale di fast-flights solleva
# FlightsNotFound, lo scraper la converte in ScraperError.
GOOGLE_ERROR_PAGE = (
    "<html><body><script class=\"ds:1\">"
    "AF_initDataCallback({key: 'ds:1', hash: '2', data:errorHasStatus: true, sideChannel: {}});"
    "</script></body></html>"
)


def _http_response(text):
    response = MagicMock()
    response.text = text
    response.url = "https://www.google.com/travel/flights"
    response.status_code = 200
    return response


class TestRealScraperChainOnlyNetworkMocked:
    """
    scraper VERO (search_options / scrape_price, parser e _request reali) con il
    solo client HTTP mockato: l'errore tecnico attraversa tutta la catena fino
    al comportamento applicativo.
    """

    def test_cerca_google_error_payload_gives_generic_message_and_detail_in_logs(self, caplog):
        update, context, reply = _update_and_context(ONE_WAY_ARGS)
        with patch("scrapers.google_flights._client") as client, \
             caplog.at_level(logging.INFO, logger="flight_bot.bot"):
            client.get.return_value = _http_response(GOOGLE_ERROR_PAGE)
            asyncio.run(bot.cerca(update, context))

        assert _texts(reply) == [SEARCHING_MESSAGE, GENERIC_ERROR_MESSAGE]
        user_text = "\n".join(_texts(reply)).lower()
        assert "flightsnotfound" not in user_text and "errorhasstatus" not in user_text
        assert "FlightsNotFound" in caplog.text             # il dettaglio e' nel log

    def test_cerca_network_timeout_gives_generic_message(self, caplog):
        update, context, reply = _update_and_context(ROUND_TRIP_ARGS)
        with patch("scrapers.google_flights._client") as client, \
             caplog.at_level(logging.INFO, logger="flight_bot.bot"):
            client.get.side_effect = primp.TimeoutError("operation timed out")
            asyncio.run(bot.cerca(update, context))

        assert _texts(reply) == [SEARCHING_MESSAGE, GENERIC_ERROR_MESSAGE]
        record = [r for r in caplog.records if r.name == "flight_bot.bot"][0]
        assert isinstance(record.exc_info[1], ScraperError)
        assert isinstance(record.exc_info[1].__cause__, primp.TimeoutError)

    def test_scheduler_cycle_survives_real_scraper_errors_without_data_or_alerts(self, caplog):
        session = MagicMock()
        session.query.return_value.filter_by.return_value.all.return_value = [_search(1), _search(2)]
        alert = AsyncMock()
        with patch.object(scheduler, "SessionLocal", return_value=session), \
             patch.object(scheduler, "Bot"), \
             patch.object(scheduler, "_sleep", new=AsyncMock()) as sleep, \
             patch.object(scheduler, "maybe_send_alert", alert), \
             patch("scrapers.google_flights._client") as client, \
             caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            # 1° passaggio: ricerca 1 in timeout (ritentabile), ricerca 2 con errore di Google
            # (non ritentabile: fallimento subito). Poi la sola ricerca 1 va in timeout ai
            # due passaggi di retry.
            client.get.side_effect = [
                primp.TimeoutError("operation timed out"),
                _http_response(GOOGLE_ERROR_PAGE),
                primp.TimeoutError("operation timed out"),
                primp.TimeoutError("operation timed out"),
            ]
            asyncio.run(scheduler.run_scraping_cycle())      # nessuna eccezione

        assert client.get.call_count == 4                    # ricerca 1 x3 tentativi + ricerca 2 x1
        assert sleep.await_count == 2                        # attese mockate: nessuna attesa reale
        session.add.assert_not_called()
        alert.assert_not_called()
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        # un solo errore definitivo per ricerca (in ordine di chiusura: la 2 subito, la 1 dopo i retry)
        assert sorted(("search_id=1" in r.getMessage(), "search_id=2" in r.getMessage())
                      for r in errors) == [(False, True), (True, False)]
        assert "FlightsNotFound" in caplog.text
