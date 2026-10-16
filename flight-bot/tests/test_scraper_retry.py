"""
Test della retry policy dello scheduler, del tracking dei fallimenti e dell'alert
amministrativo AGGREGATO.

Architettura sotto test (scheduler.py; lo scraper non ritenta mai):

    ciclo = primo passaggio su TUTTE le ricerche
            -> al massimo 2 passaggi di retry sulle SOLE ricerche fallite con un errore
               temporaneo (timeout, HTTP 429, HTTP 5xx), ciascuno dopo UNA sola attesa
               (~60 s poi ~180 s, jitter ±20%, tetto 180 s)
            -> le ricerche ancora in errore hanno un fallimento DEFINITIVO (+1)

    contatore per ricerca : fallimenti definitivi consecutivi (retry non contati);
                            si azzera con qualsiasi risposta valida, anche "nessun volo"
    alert amministrativo  : UNO per ciclo, aggregato, quando almeno una ricerca ha
                            raggiunto 3 fallimenti consecutivi; una sola volta finche'
                            nessuna ricerca e' piu' in errore persistente

Tutto offline: scraper/rete, Bot, alert di prezzo e attese sono mockati (il DB e' mockato
tranne nei test "ORM vero", che usano SQLite su file temporaneo). Nessuna attesa reale:
la fixture autouse in conftest.py fa fallire il test se si attende davvero.
"""
import asyncio
import collections
import inspect
import json
import logging
import os
import random
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import primp
import pytest
from fast_flights.exceptions import FlightsNotFound
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# config.py legge queste variabili all'IMPORT: valori finti, mai credenziali reali.
for _name, _value in {
    "TELEGRAM_BOT_TOKEN": "test-token-not-real",
    "TELEGRAM_CHAT_ID": "1",
    "TOPIC_PSA_CAG": "11",
    "TOPIC_CAG_PSA": "12",
    "TOPIC_MANUAL": "13",
    "DATABASE_URL": "sqlite://",
}.items():
    os.environ[_name] = _value

import scheduler  # noqa: E402
from models import Base, MonitoredSearch, MonitorType, PriceSnapshot  # noqa: E402
from scrapers import ScrapedPrice  # noqa: E402
from scrapers.google_flights import ScraperError, search_leg  # noqa: E402

DEPARTURE = datetime(2026, 12, 25)
CONSENT_URL = "https://consent.google.com/m?continue=https://www.google.com/travel/flights"
GOOGLE_ERROR_PAGE = (
    "<html><body><script class=\"ds:1\">"
    "AF_initDataCallback({key: 'ds:1', hash: '2', data:errorHasStatus: true, sideChannel: {}});"
    "</script></body></html>"
)
UNUSABLE_PAGE = "<html><body>traffico insolito rilevato</body></html>"
# Risposta VALIDA senza itinerari (parse() restituisce una lista vuota): "nessun volo".
_empty_payload = [None] * 8
_empty_payload[3] = [None]
_empty_payload[7] = [None, [[], []]]
VALID_EMPTY_PAGE = (
    "<html><body><script class=\"ds:1\">"
    f"AF_initDataCallback({{key: 'ds:1', hash: '2', data:{json.dumps(_empty_payload)}, sideChannel: {{}}}});"
    "</script></body></html>"
)
TECH_SECRET = "SECRET-TECH-DETAIL-98765"


# ---------------------------------------------------------------- errori e prezzi ----

def _error(message="errore tecnico", *, status=None, timed_out=False, cause=None):
    """ScraperError sollevata `from cause` come fa lo scraper, con gli attributi strutturati."""
    try:
        if cause is None:
            raise ScraperError(message, status_code=status, timed_out=timed_out)
        try:
            raise cause
        except Exception as exc:
            raise ScraperError(message, status_code=status, timed_out=timed_out) from exc
    except ScraperError as err:
        return err


def _timeout():
    return _error("Richiesta GET a Google fallita (TimeoutError)", timed_out=True,
                  cause=primp.TimeoutError("operation timed out"))


def _status_error(status):
    url = "https://example.test/x"
    return primp.StatusError(status, f"HTTP {status} for URL: {url}", url)


def _http(status):
    return _error(f"Google ha risposto con errore HTTP {status} alla richiesta GET",
                  status=status, cause=_status_error(status))


def _flights_not_found():
    return _error("Google Flights ha risposto con un errore (FlightsNotFound / errorHasStatus)",
                  cause=FlightsNotFound("no flights found; received error"))


def _parser_error():
    return _error("Analisi della pagina Google Flights fallita (AttributeError)",
                  cause=AttributeError("'NoneType' object has no attribute 'text'"))


def _consent_structure_error():
    return _error("Pagina di consenso Google senza nessun <form>")


def _price(value):
    return ScrapedPrice(
        price_eur=value, origin="PSA", destination="CAG",
        departure_date=DEPARTURE, return_date=DEPARTURE + timedelta(days=6),
        source="google_flights", departure_time="18:30", return_time="20:15",
    )


def _response(text="<html>ok</html>", url="https://www.google.com/travel/flights", status=200):
    response = MagicMock()
    response.text = text
    response.url = url
    response.status_code = status
    if status >= 400:
        response.raise_for_status.side_effect = _status_error(status)
    return response


class _LazyBodyResponse:
    """Come una risposta primp: la richiesta riesce, il corpo si legge dopo e puo' fallire."""

    def __init__(self, error, url="https://www.google.com/travel/flights"):
        self._error = error
        self.url = url
        self.status_code = 200

    def raise_for_status(self):
        return None

    @property
    def text(self):
        raise self._error


def _real_scraper_error(scenario):
    """ScraperError ottenuta eseguendo lo scraper VERO con il solo client HTTP mockato."""
    with patch("scrapers.google_flights._client") as client:
        if scenario == "timeout":
            client.get.side_effect = primp.TimeoutError("operation timed out")
        elif scenario == "dns_timeout":
            client.get.side_effect = primp.DNSTimeoutError("dns timed out")
        elif scenario == "connect_error":
            client.get.side_effect = primp.ConnectError("connection refused")
        elif scenario == "dns_error":
            client.get.side_effect = primp.DNSError("name not resolved")
        elif scenario.startswith("http_"):
            client.get.return_value = _response(text="<html>err</html>", status=int(scenario[5:]))
        elif scenario == "flights_not_found":
            client.get.return_value = _response(text=GOOGLE_ERROR_PAGE)
        elif scenario == "parser":
            client.get.return_value = _response(text=UNUSABLE_PAGE)
        elif scenario == "consent_no_form":
            client.get.return_value = _response(text="<html></html>", url=CONSENT_URL)
        elif scenario == "body_timeout":
            client.get.return_value = _LazyBodyResponse(primp.TimeoutError("operation timed out"))
        elif scenario == "body_timeout_consent_post":
            form = ('<html><body><form action="/save"><input type="hidden" name="gl" value="IT">'
                    '<button>Accetta tutto</button></form></body></html>')
            client.get.return_value = _response(text=form, url=CONSENT_URL)
            client.post.return_value = _LazyBodyResponse(primp.TimeoutError("operation timed out"))
        elif scenario == "consent_post_timeout":
            form = ('<html><body><form action="/save"><input type="hidden" name="gl" value="IT">'
                    '<button>Accetta tutto</button></form></body></html>')
            client.get.return_value = _response(text=form, url=CONSENT_URL)
            client.post.side_effect = primp.TimeoutError("operation timed out")
        else:
            raise ValueError(scenario)
        with pytest.raises(ScraperError) as excinfo:
            search_leg("PSA", "CAG", DEPARTURE)
    return excinfo.value


# ----------------------------------------------------------- 1. classificazione ----

class TestRetryClassification:
    """Quali errori si ritentano: solo dagli attributi strutturati di ScraperError."""

    RETRYABLE = ["timeout", "dns_timeout", "http_500", "http_502", "http_503", "http_504",
                 "http_429", "body_timeout", "body_timeout_consent_post"]
    NOT_RETRYABLE = ["http_403", "http_400", "http_404", "flights_not_found", "parser",
                     "consent_no_form", "connect_error", "dns_error"]

    @pytest.mark.parametrize("scenario", RETRYABLE)
    def test_temporary_errors_from_the_real_scraper_are_retryable(self, scenario):
        assert scheduler._is_retryable(_real_scraper_error(scenario)) is True

    @pytest.mark.parametrize("scenario", NOT_RETRYABLE)
    def test_other_errors_from_the_real_scraper_are_not_retryable(self, scenario):
        assert scheduler._is_retryable(_real_scraper_error(scenario)) is False

    def test_consent_post_timeout_is_classified_by_its_transport_error(self):
        """
        Interpretazione da confermare (vedi report): "consent error: no retry" vale per gli
        errori STRUTTURALI del consenso (nessun form/action); un timeout nel POST del consenso
        e' un timeout di trasporto.
        """
        assert scheduler._is_retryable(_real_scraper_error("consent_post_timeout")) is True

    @pytest.mark.parametrize("status,expected", [
        (429, True), (500, True), (501, True), (502, True), (503, True), (504, True), (599, True),
        (403, False), (400, False), (401, False), (404, False), (408, False), (499, False),
        (None, False),
    ])
    def test_http_status_boundaries(self, status, expected):
        assert scheduler._is_retryable(_error("errore", status=status)) is expected

    def test_timeout_flag_makes_the_error_retryable(self):
        assert scheduler._is_retryable(_error("errore", timed_out=True)) is True
        assert scheduler._is_retryable(_error("errore", timed_out=False)) is False

    @pytest.mark.parametrize("error", [
        _flights_not_found(), _parser_error(), _consent_structure_error(),
        _error("errore", cause=ValueError("x")), ScraperError("solo messaggio"),
    ], ids=["flights_not_found", "parser", "consent_structure", "unknown_cause", "bare"])
    def test_structural_and_unknown_errors_are_not_retryable(self, error):
        assert scheduler._is_retryable(error) is False

    def test_classification_ignores_message_and_cause(self):
        """Conta solo il dato strutturale: messaggio e causa non decidono."""
        misleading = _error("timeout HTTP 503 Service Unavailable 429", cause=AttributeError("x"))
        assert scheduler._is_retryable(misleading) is False
        neutral_but_timeout = _error("errore", timed_out=True, cause=AttributeError("x"))
        assert scheduler._is_retryable(neutral_but_timeout) is True

    def test_scheduler_does_not_depend_on_the_http_library(self):
        """L'accoppiamento a primp resta nello scraper: lo scheduler non lo importa."""
        assert not hasattr(scheduler, "primp")
        assert "primp" not in inspect.getsource(scheduler)


# ------------------------------------------------------------------- 2. backoff ----

class TestBackoff:

    def test_policy_constants(self):
        assert scheduler.SCRAPE_RETRY_BASE_DELAYS_SECONDS == (60, 180)
        assert scheduler.SCRAPE_RETRY_MAX_DELAY_SECONDS == 180
        assert scheduler.SCRAPE_RETRY_JITTER == pytest.approx(0.20)
        assert scheduler.ADMIN_ALERT_FAILURE_THRESHOLD == 3

    @pytest.mark.parametrize("factor,first,second", [
        (1.0, 60.0, 180.0),
        (0.8, 48.0, 144.0),
        (1.2, 72.0, 180.0),      # 180 * 1.2 = 216 -> tetto a 180
    ])
    def test_delays_for_fixed_jitter_factors(self, factor, first, second):
        with patch.object(scheduler.random, "uniform", return_value=factor) as uniform:
            assert scheduler._retry_delay(0) == pytest.approx(first)
            assert scheduler._retry_delay(1) == pytest.approx(second)
        uniform.assert_called_with(pytest.approx(0.8), pytest.approx(1.2))

    def test_jitter_is_applied(self):
        with patch.object(scheduler.random, "uniform", return_value=1.15):
            assert scheduler._retry_delay(0) == pytest.approx(69.0)

    def test_delays_stay_within_bounds_with_the_real_random_generator(self):
        state = random.getstate()
        try:
            random.seed(20261005)                       # deterministico
            first = [scheduler._retry_delay(0) for _ in range(2000)]
            second = [scheduler._retry_delay(1) for _ in range(2000)]
        finally:
            random.setstate(state)
        assert all(48.0 <= d <= 72.0 for d in first)
        assert all(144.0 <= d <= 180.0 for d in second)   # mai oltre il tetto
        assert len(set(first)) > 100 and len(set(second)) > 100
        assert min(first) < 55 and max(first) > 65

    def test_wait_is_cooperative_and_uses_asyncio_sleep(self, monkeypatch):
        sleep = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep)

        assert asyncio.iscoroutinefunction(scheduler._sleep)
        asyncio.run(scheduler._sleep(42))

        sleep.assert_awaited_once_with(42)


# ------------------------------------------------------------ infrastruttura di ciclo ----

def _search(search_id, origin="PSA", destination="CAG", return_date=True):
    return SimpleNamespace(
        id=search_id,
        flight_key=f"{origin}{destination}{search_id:04d}",
        origin=origin,
        destination=destination,
        departure_date=datetime.now() + timedelta(days=30 + search_id),   # univoca per ricerca
        return_date=(datetime.now() + timedelta(days=32 + search_id)) if return_date else None,
        monitor_type=MonitorType.MANUAL,
        telegram_topic_id=100 + search_id,
        earliest_departure_hour=None, latest_departure_hour=None,
        earliest_return_hour=None, latest_return_hour=None,
    )


class _TooManyAttempts(BaseException):
    """BaseException: il `except Exception` del ciclo NON la inghiotte, il test fallisce."""


def _scripted_scrape(searches, outcomes, events):
    """
    scrape_google finto: `outcomes[search.id]` = esiti per TENTATIVO (valore o eccezione).
    Un tentativo oltre il copione solleva _TooManyAttempts (retry di troppo = test rosso).
    """
    by_departure = {s.departure_date: s.id for s in searches}
    attempts = collections.Counter()

    def scrape(origin, destination, departure_date, return_date=None, **kwargs):
        search_id = by_departure[departure_date]
        attempts[search_id] += 1
        events.append(("scrape", search_id))
        script = outcomes[search_id]
        if attempts[search_id] > len(script):
            raise _TooManyAttempts(f"ricerca {search_id}: tentativo {attempts[search_id]} non previsto")
        outcome = script[attempts[search_id] - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    return scrape, attempts


def _bot():
    return MagicMock(send_message=AsyncMock())


def _run_cycle(searches, outcomes, *, bot=None, jitter=1.0, session=None, events=None,
               price_alert=None, commit_hook=None):
    """
    Esegue UN ciclo con DB (mock), Bot, scraper, alert di prezzo e attese mockati.
    Ritorna un oggetto con: session, price_alert, bot, events, attempts, delays, scrapes.
    """
    events = [] if events is None else events
    session = session or MagicMock()
    session.query.return_value.filter_by.return_value.all.return_value = searches
    def on_commit():
        if commit_hook:
            commit_hook(session)            # puo' sollevare per simulare un commit fallito
        events.append(("commit",))

    session.commit.side_effect = on_commit
    bot = bot or _bot()
    price_alert = price_alert or AsyncMock()
    scrape, attempts = _scripted_scrape(searches, outcomes, events)

    async def fake_sleep(seconds):
        events.append(("sleep", seconds))

    with patch.object(scheduler, "SessionLocal", return_value=session), \
         patch.object(scheduler, "Bot", return_value=bot), \
         patch.object(scheduler, "_sleep", new=fake_sleep), \
         patch.object(scheduler.random, "uniform", return_value=jitter), \
         patch.object(scheduler, "scrape_google", side_effect=scrape), \
         patch.object(scheduler, "maybe_send_alert", price_alert):
        asyncio.run(scheduler.run_scraping_cycle())
    return SimpleNamespace(
        session=session, price_alert=price_alert, bot=bot, events=events, attempts=attempts,
        delays=[e[1] for e in events if e[0] == "sleep"],
        scrapes=[e[1] for e in events if e[0] == "scrape"],
    )


def _snapshot_ids(cycle):
    return [call.args[0].search_id for call in cycle.session.add.call_args_list]


def _price_alert_ids(cycle):
    return [call.args[2].id for call in cycle.price_alert.call_args_list]


# --------------------------------------------------------------- 3. retry per CICLO ----

class TestBatchRetry:

    def test_all_searches_succeed_without_any_retry(self):
        searches = [_search(1), _search(2), _search(3)]

        cycle = _run_cycle(searches, {1: [_price(10)], 2: [_price(20)], 3: [None]})

        assert cycle.scrapes == [1, 2, 3]                 # una sola chiamata ciascuna
        assert cycle.delays == []
        assert _snapshot_ids(cycle) == [1, 2]             # None = nessun volo, nessuno snapshot
        assert scheduler._consecutive_failures == {}

    def test_only_the_failed_retryable_searches_are_retried(self):
        """A ok, B timeout, C ok, D 503: il retry riguarda SOLO B e D."""
        searches = [_search(1), _search(2), _search(3), _search(4)]
        outcomes = {1: [_price(10)], 2: [_timeout(), _price(22)], 3: [_price(30)],
                    4: [_http(503), _http(503), _http(503)]}

        cycle = _run_cycle(searches, outcomes)

        # eventi di scraping e attesa, senza i commit di dati
        flow = [e for e in cycle.events if e[0] != "commit"]
        assert flow == [
            ("scrape", 1), ("scrape", 2), ("scrape", 3), ("scrape", 4),
            ("sleep", pytest.approx(60.0)),
            ("scrape", 2), ("scrape", 4),
            ("sleep", pytest.approx(180.0)),
            ("scrape", 4),
        ]
        assert dict(cycle.attempts) == {1: 1, 2: 2, 3: 1, 4: 3}

    def test_successful_searches_are_stored_exactly_once(self):
        searches = [_search(1), _search(2), _search(3), _search(4)]
        outcomes = {1: [_price(10)], 2: [_timeout(), _price(22)], 3: [_price(30)],
                    4: [_http(503)] * 3}

        cycle = _run_cycle(searches, outcomes)

        assert sorted(_snapshot_ids(cycle)) == [1, 2, 3]  # nessun duplicato, D mai salvata
        assert sorted(_price_alert_ids(cycle)) == [1, 2, 3]
        assert [c.args[0].price_eur for c in cycle.session.add.call_args_list
                if c.args[0].search_id == 2] == [22]

    def test_search_recovering_during_a_retry_counts_as_a_success(self):
        scheduler._consecutive_failures[2] = 2

        cycle = _run_cycle([_search(1), _search(2)],
                           {1: [_price(10)], 2: [_timeout(), _price(22)]})

        assert 2 not in scheduler._consecutive_failures    # azzerata
        assert sorted(_snapshot_ids(cycle)) == [1, 2]

    def test_recoveries_at_different_rounds(self):
        """B recupera al 1° retry, C al 2°; D non recupera: al 2° retry restano C e D."""
        searches = [_search(1), _search(2), _search(3)]
        outcomes = {1: [_timeout(), _price(11)], 2: [_timeout(), _timeout(), _price(22)],
                    3: [_timeout(), _timeout(), _timeout()]}

        cycle = _run_cycle(searches, outcomes)

        assert cycle.scrapes == [1, 2, 3, 1, 2, 3, 2, 3]
        assert cycle.delays == [pytest.approx(60.0), pytest.approx(180.0)]
        assert sorted(_snapshot_ids(cycle)) == [1, 2]
        assert scheduler._consecutive_failures == {3: 1}

    def test_never_more_than_two_retries(self):
        cycle = _run_cycle([_search(1)], {1: [_timeout()] * 3})    # un 4° tentativo = test rosso

        assert cycle.attempts[1] == 3
        assert len(cycle.delays) == 2
        assert scheduler._consecutive_failures == {1: 1}

    @pytest.mark.parametrize("error_factory", [
        lambda: _http(403), lambda: _http(404), _flights_not_found, _parser_error,
        _consent_structure_error,
    ], ids=["http403", "http404", "flights_not_found", "parser", "consent_structure"])
    def test_non_retryable_error_is_not_retried(self, error_factory):
        cycle = _run_cycle([_search(1), _search(2)],
                           {1: [error_factory()], 2: [_price(5)]})   # un 2° tentativo = test rosso

        assert dict(cycle.attempts) == {1: 1, 2: 1}
        assert cycle.delays == []                                    # nessuna attesa
        assert scheduler._consecutive_failures == {1: 1}

    @pytest.mark.parametrize("error_factory", [
        _timeout, lambda: _http(429), lambda: _http(500), lambda: _http(503), lambda: _http(504),
    ], ids=["timeout", "http429", "http500", "http503", "http504"])
    def test_temporary_error_is_retried_once_after_the_first_wait(self, error_factory):
        cycle = _run_cycle([_search(1)], {1: [error_factory(), _price(9)]})

        assert cycle.attempts[1] == 2
        assert cycle.delays == [pytest.approx(60.0)]
        assert _snapshot_ids(cycle) == [1]

    def test_retry_error_that_is_not_retryable_stops_that_search(self):
        """Al retry l'errore diventa un 403: la ricerca non viene piu' ritentata."""
        cycle = _run_cycle([_search(1), _search(2)],
                           {1: [_timeout(), _http(403)], 2: [_timeout(), _timeout(), _price(1)]})

        assert dict(cycle.attempts) == {1: 2, 2: 3}
        assert scheduler._consecutive_failures == {1: 1}

    def test_waits_happen_once_per_round_not_once_per_search(self):
        """Il punto centrale: con 104 ricerche in timeout le attese restano DUE, non 208."""
        searches = [_search(i) for i in range(1, 105)]
        outcomes = {s.id: [_timeout()] * 3 for s in searches}

        cycle = _run_cycle(searches, outcomes, jitter=1.2)         # jitter massimo

        assert len(cycle.delays) == 2
        assert sum(cycle.delays) <= 60 * 1.2 + 180                 # <= 252 s per ciclo, qualunque N
        assert all(cycle.attempts[s.id] == 3 for s in searches)    # ma ogni ricerca ha 3 tentativi
        assert len(cycle.scrapes) == 104 * 3

    def test_retry_logging_is_a_summary_per_round_with_no_traceback(self, caplog):
        searches = [_search(1), _search(2), _search(3)]
        outcomes = {1: [_timeout(), _price(1)], 2: [_timeout()] * 3, 3: [_price(3)]}

        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            _run_cycle(searches, outcomes)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2 and all(r.exc_info is None for r in warnings)
        assert "2 ricerche su 3" in warnings[0].getMessage()
        assert "retry 1/2" in warnings[0].getMessage()
        assert "1 ricerche su 3" in warnings[1].getMessage()
        assert "retry 2/2" in warnings[1].getMessage()


class TestCycleIsolation:

    def test_a_failure_never_stops_the_other_searches(self):
        searches = [_search(1), _search(2), _search(3)]

        cycle = _run_cycle(searches, {1: [_price(1)], 2: [_timeout()] * 3, 3: [_price(3)]})

        assert sorted(_snapshot_ids(cycle)) == [1, 3]
        assert sorted(_price_alert_ids(cycle)) == [1, 3]

    def test_failed_searches_create_no_snapshot_and_no_price_alert(self):
        searches = [_search(1), _search(2)]

        cycle = _run_cycle(searches, {1: [_timeout()] * 3, 2: [_http(403)]})

        cycle.session.add.assert_not_called()
        cycle.price_alert.assert_not_called()

    def test_unexpected_exceptions_are_isolated_not_retried_and_not_counted(self, caplog):
        searches = [_search(1), _search(2), _search(3)]
        scheduler._consecutive_failures[2] = 2

        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            cycle = _run_cycle(searches, {1: [_price(1)], 2: [ValueError("bug")], 3: [_price(3)]})

        assert dict(cycle.attempts) == {1: 1, 2: 1, 3: 1}          # nessun retry
        assert cycle.delays == []
        assert sorted(_snapshot_ids(cycle)) == [1, 3]
        assert scheduler._consecutive_failures == {2: 2}           # ne' +1 ne' azzerato
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1 and "search_id=2" in errors[0].getMessage()
        assert isinstance(errors[0].exc_info[1], ValueError)

    def test_definitive_failure_is_logged_once_with_traceback_and_context(self, caplog):
        searches = [_search(1), _search(2, origin="CAG", destination="PSA")]
        last = _timeout()

        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            _run_cycle(searches, {1: [_price(1)], 2: [_timeout(), _timeout(), last]})

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1                                    # uno solo, non uno per tentativo
        message = errors[0].getMessage()
        assert "search_id=2" in message and "CAGPSA0002" in message
        assert "CAG" in message and "PSA" in message
        assert errors[0].exc_info[1] is last                       # traceback dell'ultimo errore
        assert str(last.__cause__) in caplog.text

    def test_the_next_cycle_runs_normally_after_a_failing_one(self):
        searches = [_search(1), _search(2)]

        first = _run_cycle(searches, {1: [_price(1)], 2: [_timeout()] * 3})
        second = _run_cycle(searches, {1: [_price(2)], 2: [_price(3)]})

        assert sorted(_snapshot_ids(first)) == [1]
        assert sorted(_snapshot_ids(second)) == [1, 2]
        assert scheduler._consecutive_failures == {}

    def test_past_searches_are_skipped_and_never_counted(self):
        past = _search(1)
        past.departure_date = datetime.now() - timedelta(days=2)
        scheduler._consecutive_failures[1] = 2

        cycle = _run_cycle([past, _search(2)], {1: [], 2: [_price(5)]})

        assert cycle.scrapes == [2]
        assert 1 not in scheduler._consecutive_failures            # contatore potato
        assert _snapshot_ids(cycle) == [2]


# --------------------------------------------------------- 4. failure tracking ----

class TestFailureTracking:

    def test_consecutive_failures_count_up(self):
        assert [scheduler._record_failure(1) for _ in range(3)] == [1, 2, 3]

    def test_success_resets_the_counter(self):
        scheduler._record_failure(1)
        scheduler._record_failure(1)

        scheduler._record_success(1)

        assert scheduler._consecutive_failures == {}
        assert scheduler._record_failure(1) == 1

    def test_different_searches_have_independent_counters(self):
        for search_id in (1, 1, 2):
            scheduler._record_failure(search_id)

        assert scheduler._consecutive_failures == {1: 2, 2: 1}
        scheduler._record_success(2)
        assert scheduler._consecutive_failures == {1: 2}

    def test_stale_counters_are_forgotten(self):
        scheduler._consecutive_failures.update({1: 3, 2: 1, 3: 2})

        scheduler._forget_stale_failures({2})

        assert scheduler._consecutive_failures == {2: 1}

    def test_retries_in_the_same_cycle_count_as_a_single_failure(self):
        _run_cycle([_search(1)], {1: [_timeout()] * 3})

        assert scheduler._consecutive_failures == {1: 1}

    def test_each_cycle_adds_exactly_one_failure(self):
        search = _search(1)
        for expected in (1, 2, 3):
            _run_cycle([search], {1: [_timeout()] * 3})
            assert scheduler._consecutive_failures == {1: expected}

    def test_non_retryable_failure_counts_once(self):
        _run_cycle([_search(1)], {1: [_http(403)]})

        assert scheduler._consecutive_failures == {1: 1}

    @pytest.mark.parametrize("success", [_price(80.0), None], ids=["price", "no_flights"])
    def test_any_valid_response_resets_the_counter(self, success):
        scheduler._consecutive_failures[1] = 2

        _run_cycle([_search(1)], {1: [success]})

        assert scheduler._consecutive_failures == {}

    def test_valid_response_resets_the_counter_even_if_storing_it_fails(self):
        """Lo scraper ha risposto: e' un successo dello scraper, anche se il salvataggio/alert poi fallisce."""
        scheduler._consecutive_failures[1] = 2
        failing_alert = AsyncMock(side_effect=ValueError("errore negli alert di prezzo"))

        # F2: l'errore e' isolato per ricerca (il ciclo termina) e registrato nei log
        _run_cycle([_search(1)], {1: [_price(5.0)]}, price_alert=failing_alert)

        assert scheduler._consecutive_failures == {}

    def test_success_after_retry_resets_the_counter(self):
        scheduler._consecutive_failures[1] = 2

        _run_cycle([_search(1)], {1: [_timeout(), _price(1)]})

        assert scheduler._consecutive_failures == {}

    def test_counters_are_isolated_between_searches_across_cycles(self):
        a, b = _search(1), _search(2)

        _run_cycle([a, b], {1: [_http(403)], 2: [_price(1)]})
        _run_cycle([a, b], {1: [_http(403)], 2: [_http(403)]})

        assert scheduler._consecutive_failures == {1: 2, 2: 1}

    def test_a_search_that_is_no_longer_checked_loses_its_counter(self):
        scheduler._consecutive_failures.update({1: 3, 2: 1})

        _run_cycle([_search(2)], {2: [_http(403)]})               # la ricerca 1 non c'e' piu'

        assert scheduler._consecutive_failures == {2: 2}


# ---------------------------------------------------------- 5. alert AGGREGATO ----

def _failing_cycle(searches, bot, error_factory=lambda: _http(403), attempts=1):
    """Un ciclo in cui TUTTE le ricerche falliscono (`attempts`: 1 se non ritentabile, 3 se temporaneo)."""
    return _run_cycle(searches, {s.id: [error_factory() for _ in range(attempts)] for s in searches},
                      bot=bot)


class TestAggregatedAdminAlert:

    def test_no_alert_below_the_threshold(self):
        bot = _bot()
        searches = [_search(1)]

        _failing_cycle(searches, bot)
        _failing_cycle(searches, bot)

        bot.send_message.assert_not_awaited()
        assert scheduler._consecutive_failures == {1: 2}

    def test_alert_when_a_search_reaches_three_consecutive_failures(self):
        bot = _bot()
        searches = [_search(1)]

        for _ in range(3):
            _failing_cycle(searches, bot)

        bot.send_message.assert_awaited_once()
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == scheduler.TELEGRAM_CHAT_ID == 1
        assert "message_thread_id" not in kwargs                  # topic generale

    def test_no_burst_of_alerts_for_a_global_outage(self):
        """104 ricerche in errore per molti cicli: UN solo messaggio in totale."""
        bot = _bot()
        searches = [_search(i) for i in range(1, 105)]

        for _ in range(6):
            _failing_cycle(searches, bot, error_factory=_timeout, attempts=3)

        assert bot.send_message.await_count == 1
        assert "104 ricerche hanno fallito" in bot.send_message.await_args.kwargs["text"]

    def test_alert_is_aggregated_and_counts_only_persistent_failures(self):
        bot = _bot()
        failing = [_search(i) for i in range(1, 8)]               # 7 ricerche sempre in errore
        healthy = [_search(i) for i in range(8, 20)]              # 12 ricerche che riescono
        outcomes = {**{s.id: [_http(403)] for s in failing}, **{s.id: [_price(1)] for s in healthy}}
        for _ in range(3):
            outcomes = {**{s.id: [_http(403)] for s in failing},
                        **{s.id: [_price(1)] for s in healthy}}
            _run_cycle(failing + healthy, outcomes, bot=bot)

        assert bot.send_message.await_count == 1
        assert "7 ricerche hanno fallito almeno 3 controlli consecutivi" in (
            bot.send_message.await_args.kwargs["text"])

    def test_no_duplicate_alert_in_the_following_cycles(self):
        bot = _bot()
        searches = [_search(1), _search(2)]
        for _ in range(3):
            _failing_cycle(searches, bot)
        assert bot.send_message.await_count == 1

        for _ in range(5):
            _failing_cycle(searches, bot)

        assert bot.send_message.await_count == 1
        assert scheduler._admin_alert_sent is True

    def test_retries_alone_never_trigger_an_alert(self):
        bot = _bot()

        _run_cycle([_search(1)], {1: [_timeout()] * 3}, bot=bot)  # 3 tentativi, 1 solo fallimento

        bot.send_message.assert_not_awaited()

    def test_partial_recovery_does_not_reset_the_alert_state(self):
        """Meta' delle ricerche recupera, l'altra meta' continua a fallire: nessun reset, nessun nuovo alert."""
        bot = _bot()
        searches = [_search(i) for i in range(1, 11)]
        for _ in range(3):
            _failing_cycle(searches, bot)
        assert bot.send_message.await_count == 1

        def partial():
            return {s.id: [_price(1)] if s.id <= 5 else [_http(403)] for s in searches}

        _run_cycle(searches, partial(), bot=bot)

        assert scheduler._admin_alert_sent is True                 # lo scraper NON e' "recuperato"
        assert sorted(scheduler._consecutive_failures) == [6, 7, 8, 9, 10]
        assert bot.send_message.await_count == 1
        for _ in range(3):
            _run_cycle(searches, partial(), bot=bot)
        assert bot.send_message.await_count == 1                   # ancora nessun nuovo alert

    def test_full_recovery_resets_and_a_new_streak_alerts_again(self):
        bot = _bot()
        searches = [_search(1), _search(2)]
        for _ in range(3):
            _failing_cycle(searches, bot)
        assert bot.send_message.await_count == 1 and scheduler._admin_alert_sent is True

        _run_cycle(searches, {1: [_price(1)], 2: [None]}, bot=bot)    # tutte rispondono (anche "nessun volo")

        assert scheduler._admin_alert_sent is False
        assert scheduler._consecutive_failures == {}
        for _ in range(2):
            _failing_cycle(searches, bot)
        assert bot.send_message.await_count == 1                   # nuova serie: ancora sotto soglia
        _failing_cycle(searches, bot)
        assert bot.send_message.await_count == 2                   # 3° della nuova serie

    def test_expired_chronic_search_does_not_block_the_recovery(self):
        """Una ricerca in errore persistente che scade/viene disattivata non tiene l'allarme acceso."""
        bot = _bot()
        stuck, healthy = _search(1), _search(2)
        for _ in range(3):
            _run_cycle([stuck, healthy], {1: [_http(403)], 2: [_price(1)]}, bot=bot)
        assert scheduler._admin_alert_sent is True

        _run_cycle([healthy], {2: [_price(1)]}, bot=bot)           # la ricerca 1 non e' piu' attiva

        assert scheduler._admin_alert_sent is False

    def test_alert_is_sent_at_the_end_of_the_cycle_after_every_search(self):
        events = []
        bot = _bot()
        bot.send_message.side_effect = lambda **kwargs: events.append(("alert",))
        searches = [_search(1), _search(2), _search(3)]
        scheduler._consecutive_failures[1] = 2                     # al 3° fallimento: soglia

        cycle = _run_cycle(searches, {1: [_http(403)], 2: [_price(2)], 3: [_price(3)]},
                           bot=bot, events=events)

        assert events.count(("alert",)) == 1
        assert events[-1] == ("alert",)                            # dopo l'ultimo scraping e commit
        assert cycle.scrapes == [1, 2, 3]
        assert sorted(_snapshot_ids(cycle)) == [2, 3]              # le altre ricerche proseguono comunque

    def test_alert_text_has_only_appropriate_information(self):
        bot = _bot()
        error = _error(f"Google ha risposto con un errore (FlightsNotFound / errorHasStatus: "
                       f"{TECH_SECRET})", cause=FlightsNotFound(TECH_SECRET))
        searches = [_search(1), _search(2, origin="CAG", destination="PSA")]
        for _ in range(3):
            _run_cycle(searches, {1: [error], 2: [error]}, bot=bot)

        text = bot.send_message.await_args.kwargs["text"]
        assert text.startswith("⚠️ Scraper Google Flights")
        assert "2 ricerche hanno fallito almeno 3 controlli consecutivi." in text
        assert "PSA → CAG (1)" in text and "CAG → PSA (1)" in text
        assert text.endswith("Il monitoraggio continua automaticamente.")
        for forbidden in ("ScraperError", "FlightsNotFound", "errorHasStatus", "Traceback",
                          "HTTP", "primp", "timeout", "search_id", "Exception", TECH_SECRET):
            assert forbidden.lower() not in text.lower(), forbidden

    def test_technical_details_stay_in_the_logs(self, caplog):
        bot = _bot()
        error = _error(f"errore {TECH_SECRET}", cause=FlightsNotFound(TECH_SECRET))

        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            for _ in range(3):
                _run_cycle([_search(1)], {1: [error]}, bot=bot)

        assert TECH_SECRET in caplog.text
        assert TECH_SECRET not in bot.send_message.await_args.kwargs["text"]

    def test_text_for_a_single_search_uses_the_singular(self):
        text = scheduler._admin_alert_text([_search(1)])

        assert "1 ricerca ha fallito almeno 3 controlli consecutivi." in text
        assert "PSA → CAG (1)" in text

    def test_route_summary_is_sorted_by_count_and_capped(self):
        searches = [_search(i, origin="PSA", destination="CAG") for i in range(1, 6)]       # 5
        searches += [_search(i, origin="CAG", destination="PSA") for i in range(6, 9)]      # 3
        for n, (o, d) in enumerate([("AAA", "BBB"), ("CCC", "DDD"), ("EEE", "FFF"),
                                    ("GGG", "HHH")], start=9):                              # 1 ciascuna
            searches.append(_search(n, origin=o, destination=d))

        text = scheduler._admin_alert_text(searches)

        assert "12 ricerche hanno fallito" in text
        assert text.index("PSA → CAG (5)") < text.index("CAG → PSA (3)")
        assert text.count("→") == 5                                 # al massimo 5 rotte elencate
        assert "e altre 1 rotte" in text                            # 6 rotte distinte: 1 omessa

    def test_alert_send_failure_does_not_break_the_cycle_and_is_retried(self, caplog):
        bot = MagicMock(send_message=AsyncMock(side_effect=RuntimeError("telegram giu'")))
        searches = [_search(1), _search(2)]
        scheduler._consecutive_failures[1] = 2

        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            cycle = _run_cycle(searches, {1: [_http(403)], 2: [_price(9)]}, bot=bot)

        bot.send_message.assert_awaited_once()                      # tentato
        assert _snapshot_ids(cycle) == [2]                          # il ciclo e' stato completato
        assert scheduler._admin_alert_sent is False                 # NON risulta inviato...
        assert any("alert amministrativo" in r.getMessage() and r.exc_info
                   for r in caplog.records if r.levelno >= logging.ERROR)

        bot.send_message.side_effect = None                         # ...Telegram torna su
        _run_cycle(searches, {1: [_http(403)], 2: [_price(9)]}, bot=bot)
        assert bot.send_message.await_count == 2                    # riprovato al ciclo dopo
        assert scheduler._admin_alert_sent is True
        _run_cycle(searches, {1: [_http(403)], 2: [_price(9)]}, bot=bot)
        assert bot.send_message.await_count == 2                    # poi niente duplicati

    def test_no_searches_at_all_resets_the_state(self):
        scheduler._admin_alert_sent = True

        _run_cycle([], {})

        assert scheduler._admin_alert_sent is False


# ------------------------------------------------------------- 6. sessione DB ----

class TestDatabaseSession:

    def test_transaction_is_released_after_loading_and_before_every_wait(self):
        searches = [_search(1), _search(2)]

        cycle = _run_cycle(searches, {1: [_timeout()] * 3, 2: [_price(5)]})

        events = cycle.events
        assert events[0] == ("commit",)                              # subito dopo il caricamento
        for index, event in enumerate(events):
            if event[0] == "sleep":
                assert events[index - 1] == ("commit",), "attesa con transazione aperta"
        assert [e[0] for e in events].count("sleep") == 2

    def test_real_orm_no_connection_is_held_while_scraping_or_waiting(self, tmp_path):
        """ORM e pool veri (SQLite su file): durante lo scraping e le attese nessuna connessione in uso."""
        engine = create_engine(f"sqlite:///{tmp_path / 'cycle.db'}", pool_pre_ping=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)      # come db.py
        with Session() as setup:
            for i in (1, 2):
                setup.add(MonitoredSearch(
                    origin="PSA", destination="CAG",
                    departure_date=datetime.now() + timedelta(days=30 + i),
                    return_date=datetime.now() + timedelta(days=32 + i),
                    monitor_type=MonitorType.MANUAL, telegram_topic_id=7, active=True))
            setup.commit()
            ids = [s.id for s in setup.query(MonitoredSearch).order_by(MonitoredSearch.id)]

        in_use = {"scrape": [], "wait": []}
        script = {ids[0]: [_timeout(), _price(70.0)], ids[1]: [_timeout()] * 3}
        attempts = collections.Counter()

        def scrape(origin, destination, departure_date, return_date=None, **kwargs):
            in_use["scrape"].append(engine.pool.checkedout())
            with Session() as peek:                                      # identifica la ricerca
                search_id = peek.query(MonitoredSearch.id).filter_by(
                    departure_date=departure_date).scalar()
            attempts[search_id] += 1
            outcome = script[search_id][attempts[search_id] - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        async def fake_sleep(seconds):
            in_use["wait"].append(engine.pool.checkedout())

        with patch.object(scheduler, "SessionLocal", Session), \
             patch.object(scheduler, "Bot", return_value=_bot()), \
             patch.object(scheduler, "_sleep", new=fake_sleep), \
             patch.object(scheduler, "scrape_google", side_effect=scrape), \
             patch.object(scheduler, "maybe_send_alert", AsyncMock()):
            asyncio.run(scheduler.run_scraping_cycle())

        assert len(in_use["scrape"]) == 5 and len(in_use["wait"]) == 2
        assert set(in_use["scrape"]) == {0}, in_use                       # mai una connessione trattenuta
        assert set(in_use["wait"]) == {0}, in_use
        assert engine.pool.checkedout() == 0                               # e a fine ciclo
        with Session() as check:                                          # i dati veri sono stati salvati
            snapshots = check.query(PriceSnapshot).all()
            assert [(s.search_id, s.price_eur) for s in snapshots] == [(ids[0], 70.0)]
        assert scheduler._consecutive_failures == {ids[1]: 1}
        engine.dispose()

    def test_real_orm_aggregated_alert_over_several_cycles(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'alert.db'}")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        with Session() as setup:
            for i in range(1, 7):
                setup.add(MonitoredSearch(
                    origin="PSA" if i <= 4 else "CAG", destination="CAG" if i <= 4 else "PSA",
                    departure_date=datetime.now() + timedelta(days=30 + i), return_date=None,
                    monitor_type=MonitorType.MANUAL, telegram_topic_id=7, active=True))
            setup.commit()
        bot = _bot()

        def scrape(*args, **kwargs):
            raise _http(503)

        for _ in range(4):
            with patch.object(scheduler, "SessionLocal", Session), \
                 patch.object(scheduler, "Bot", return_value=bot), \
                 patch.object(scheduler, "_sleep", new=AsyncMock()), \
                 patch.object(scheduler, "scrape_google", side_effect=scrape), \
                 patch.object(scheduler, "maybe_send_alert", AsyncMock()):
                asyncio.run(scheduler.run_scraping_cycle())

        assert bot.send_message.await_count == 1
        text = bot.send_message.await_args.kwargs["text"]
        assert "6 ricerche hanno fallito" in text
        assert "PSA → CAG (4)" in text and "CAG → PSA (2)" in text
        engine.dispose()


# ------------------------------------------------------------- 7. event loop ----

class TestEventLoopStaysResponsive:

    def test_a_concurrent_task_keeps_running_during_the_backoff(self):
        """L'attesa del backoff cede il controllo all'event loop: il bot continua a rispondere."""
        yields = 5
        waits = []

        async def cooperative_wait(seconds):
            waits.append(seconds)
            for _ in range(yields):
                await asyncio.sleep(0)               # cede il controllo, senza attendere

        async def scenario():
            beats = []

            async def heartbeat():
                while True:
                    beats.append(1)
                    await asyncio.sleep(0)

            task = asyncio.create_task(heartbeat())
            searches = [_search(1)]
            session = MagicMock()
            session.query.return_value.filter_by.return_value.all.return_value = searches
            scrape, _ = _scripted_scrape(searches, {1: [_timeout(), _price(1)]}, [])
            with patch.object(scheduler, "SessionLocal", return_value=session), \
                 patch.object(scheduler, "Bot"), \
                 patch.object(scheduler, "_sleep", new=cooperative_wait), \
                 patch.object(scheduler.random, "uniform", return_value=1.0), \
                 patch.object(scheduler, "scrape_google", side_effect=scrape), \
                 patch.object(scheduler, "maybe_send_alert", AsyncMock()):
                await scheduler.run_scraping_cycle()
            task.cancel()
            return len(beats)

        beats = asyncio.run(scenario())

        assert waits == [pytest.approx(60.0)]
        assert beats >= yields                       # il heartbeat e' avanzato durante l'attesa

    def test_the_wait_is_never_a_blocking_sleep(self):
        """`_sleep` e' una coroutine e il modulo non usa time.sleep (guardia anche in conftest)."""
        assert asyncio.iscoroutinefunction(scheduler._sleep)
        assert "time.sleep" not in inspect.getsource(scheduler)


# ------------------------------------- F1: timeout nel corpo = errore ritentabile ----

def _run_cycle_real_scraper(searches, client):
    """
    Un ciclo con lo scraper VERO (scheduler.scrape_google NON mockato): si simula solo la rete
    (`scrapers.google_flights._client`). Ritorna (eventi, sessione mock, alert di prezzo, bot).
    """
    events = []
    session = MagicMock()
    session.query.return_value.filter_by.return_value.all.return_value = searches
    session.commit.side_effect = lambda: events.append(("commit",))
    bot, price_alert = _bot(), AsyncMock()

    async def fake_sleep(seconds):
        events.append(("sleep", seconds))

    with patch.object(scheduler, "SessionLocal", return_value=session), \
         patch.object(scheduler, "Bot", return_value=bot), \
         patch.object(scheduler, "_sleep", new=fake_sleep), \
         patch.object(scheduler.random, "uniform", return_value=1.0), \
         patch("scrapers.google_flights._client", client), \
         patch.object(scheduler, "maybe_send_alert", price_alert):
        asyncio.run(scheduler.run_scraping_cycle())
    return SimpleNamespace(events=events, session=session, price_alert=price_alert, bot=bot,
                           delays=[e[1] for e in events if e[0] == "sleep"])


class TestBodyReadTimeoutIsRetried:
    """Il timeout durante la lettura del corpo viene ritentato come ogni altro timeout."""

    def test_body_timeout_then_valid_response_is_retried_and_recovers(self):
        search = _search(1, return_date=False)                   # solo andata: 1 richiesta per tentativo
        client = MagicMock()
        client.get.side_effect = [_LazyBodyResponse(primp.TimeoutError("operation timed out")),
                                  _response(text=VALID_EMPTY_PAGE)]
        scheduler._consecutive_failures[1] = 2

        cycle = _run_cycle_real_scraper([search], client)

        assert client.get.call_count == 2                        # ritentata davvero
        assert cycle.delays == [pytest.approx(60.0)]             # dopo la prima attesa
        assert scheduler._consecutive_failures == {}             # risposta valida: azzerato

    def test_body_timeout_on_every_attempt_is_one_definitive_failure(self, caplog):
        search = _search(1, return_date=False)
        client = MagicMock()
        client.get.side_effect = [_LazyBodyResponse(primp.TimeoutError("operation timed out"))
                                  for _ in range(3)]

        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            cycle = _run_cycle_real_scraper([search], client)

        assert client.get.call_count == 3                        # 1 tentativo + 2 retry, non di piu'
        assert cycle.delays == [pytest.approx(60.0), pytest.approx(180.0)]
        assert scheduler._consecutive_failures == {1: 1}
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert errors[0].exc_info[1].timed_out is True
        assert isinstance(errors[0].exc_info[1].__cause__, primp.TimeoutError)

    def test_body_timeout_on_the_return_leg_of_a_round_trip_is_retried(self):
        """Andata valida, timeout nel CORPO della risposta del ritorno: si ritenta tutta la ricerca."""
        search = _search(1)                                      # andata e ritorno: 2 richieste
        flight = SimpleNamespace(
            price="50.00", airlines=["Ryanair"],
            flights=[SimpleNamespace(departure=SimpleNamespace(time=(18, 30)))])
        client = MagicMock()
        client.get.side_effect = [
            _response(text="<html>andata</html>"),                                   # 1° tentativo, andata ok
            _LazyBodyResponse(primp.TimeoutError("operation timed out")),            #               ritorno: timeout nel corpo
            _response(text="<html>andata</html>"),                                   # 2° tentativo, andata ok
            _response(text="<html>ritorno</html>"),                                  #               ritorno ok
        ]

        with patch("scrapers.google_flights.parse", side_effect=lambda html: [flight]):
            cycle = _run_cycle_real_scraper([search], client)

        assert client.get.call_count == 4                        # 2 richieste per tentativo, 2 tentativi
        assert cycle.delays == [pytest.approx(60.0)]
        stored = [c.args[0] for c in cycle.session.add.call_args_list]
        assert [(snap.search_id, snap.price_eur) for snap in stored] == [(1, 100.0)]   # 50 + 50
        assert scheduler._consecutive_failures == {}

    def test_non_timeout_body_error_is_still_not_retried(self):
        """Fuori scope di F1 (nessuna nuova policy): un corpo troncato resta non ritentabile."""
        search = _search(1, return_date=False)
        client = MagicMock()
        client.get.side_effect = [_LazyBodyResponse(primp.DecodeError("error decoding response body"))]

        cycle = _run_cycle_real_scraper([search], client)

        assert client.get.call_count == 1                        # nessun retry
        assert cycle.delays == []
        assert scheduler._consecutive_failures == {1: 1}


# --------------------------- F2: un errore nel salvataggio/alert isola UNA ricerca ----

def _alert_failing_for(*failing_ids):
    """alert di prezzo che solleva (un'eccezione per ricerca, conservata in .raised) per gli id indicati."""
    raised = {}

    def alert(session, bot, search, snapshot):
        if search.id in failing_ids:
            raised[search.id] = ValueError(f"alert fallito per la ricerca {search.id}")
            raise raised[search.id]

    mock = AsyncMock(side_effect=alert)
    mock.raised = raised
    return mock


class TestStoreResultIsolation:

    def test_first_search_store_failure_does_not_stop_the_second(self):
        searches = [_search(1), _search(2)]
        alert = _alert_failing_for(1)

        cycle = _run_cycle(searches, {1: [_price(10)], 2: [_price(20)]}, price_alert=alert)

        assert cycle.scrapes == [1, 2]                           # la 2ª ricerca e' stata comunque eseguita
        assert _snapshot_ids(cycle) == [1, 2]
        assert [c.args[2].id for c in alert.call_args_list] == [1, 2]
        cycle.session.close.assert_called_once()

    def test_failure_in_the_middle_lets_the_following_searches_run(self):
        searches = [_search(i) for i in range(1, 5)]
        alert = _alert_failing_for(2)

        cycle = _run_cycle(searches, {s.id: [_price(10 * s.id)] for s in searches}, price_alert=alert)

        assert cycle.scrapes == [1, 2, 3, 4]
        assert _snapshot_ids(cycle) == [1, 2, 3, 4]
        assert [c.args[2].id for c in alert.call_args_list] == [1, 2, 3, 4]

    def test_store_failure_is_logged_with_the_search_and_the_original_traceback(self, caplog):
        searches = [_search(1), _search(2, origin="CAG", destination="PSA"), _search(3)]
        alert = _alert_failing_for(2)

        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            _run_cycle(searches, {s.id: [_price(5)] for s in searches}, price_alert=alert)

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "search_id=2" in errors[0].getMessage() and "CAGPSA0002" in errors[0].getMessage()
        assert errors[0].exc_info[1] is alert.raised[2]          # traceback dell'errore ORIGINALE

    def test_retry_bookkeeping_survives_store_failures_of_other_searches(self):
        """Gli errori di salvataggio (anche durante un passaggio di retry) non fanno perdere i retry ne' i contatori."""
        searches = [_search(i) for i in range(1, 5)]
        scheduler._consecutive_failures[3] = 2                   # la 3 e' a un passo dall'errore persistente
        alert = _alert_failing_for(1, 2)                         # 2 fallisce al 1° passaggio, 1 al retry
        bot = _bot()
        outcomes = {1: [_timeout(), _price(11)],                 # recupera al retry (e il suo store fallisce)
                    2: [_price(22)],                             # store che fallisce al 1° passaggio
                    3: [_timeout()] * 3,                         # esaurisce i retry
                    4: [_price(44)]}

        cycle = _run_cycle(searches, outcomes, price_alert=alert, bot=bot)

        assert dict(cycle.attempts) == {1: 2, 2: 1, 3: 3, 4: 1}  # nessun tentativo perso
        assert cycle.delays == [pytest.approx(60.0), pytest.approx(180.0)]
        assert scheduler._consecutive_failures == {3: 3}         # fallimento definitivo contato
        bot.send_message.assert_awaited_once()                   # e l'alert aggregato e' stato valutato

    def test_no_false_snapshot_or_price_alert_as_a_consequence(self):
        searches = [_search(1), _search(2), _search(3)]
        alert = _alert_failing_for(1)

        cycle = _run_cycle(searches, {1: [_price(10)], 2: [_http(403)], 3: [_price(30)]},
                           price_alert=alert)

        assert _snapshot_ids(cycle) == [1, 3]                    # solo risultati veri, la 2 (errore) no
        assert [c.args[2].id for c in alert.call_args_list] == [1, 3]
        assert scheduler._consecutive_failures == {2: 1}

    def test_commit_failure_rolls_back_and_the_following_searches_still_work(self, caplog):
        searches = [_search(i) for i in range(1, 5)]
        session = MagicMock()
        session.is_active = True
        failed = {"done": False}

        def failing_commit(sess):
            added = sess.add.call_args_list
            if added and added[-1].args[0].search_id == 2 and not failed["done"]:
                failed["done"] = True
                sess.is_active = False                           # come SQLAlchemy dopo un commit fallito
                raise RuntimeError("flush failed")

        session.rollback.side_effect = lambda: setattr(session, "is_active", True)
        alert = AsyncMock()

        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            cycle = _run_cycle(searches, {s.id: [_price(10 * s.id)] for s in searches},
                               session=session, price_alert=alert, commit_hook=failing_commit)

        session.rollback.assert_called_once()
        assert cycle.scrapes == [1, 2, 3, 4]
        assert [c.args[2].id for c in alert.call_args_list] == [1, 3, 4]   # la 2: commit fallito, nessun alert
        assert any("search_id=2" in r.getMessage() and r.exc_info for r in caplog.records
                   if r.levelno >= logging.ERROR)

    def test_session_is_not_rolled_back_when_it_is_still_active(self):
        """Un errore di Telegram dopo il commit non invalida la sessione: niente rollback (farebbe scadere gli oggetti)."""
        session = MagicMock()
        session.is_active = True

        _run_cycle([_search(1), _search(2)], {1: [_price(1)], 2: [_price(2)]},
                   session=session, price_alert=_alert_failing_for(1))

        session.rollback.assert_not_called()

    def test_rollback_failure_does_not_abort_the_cycle(self, caplog):
        session = MagicMock()
        session.is_active = False                                # sessione gia' invalidata
        session.rollback.side_effect = RuntimeError("database irraggiungibile")

        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            cycle = _run_cycle([_search(1), _search(2)], {1: [_price(1)], 2: [_price(2)]},
                               session=session, price_alert=_alert_failing_for(1))

        assert cycle.scrapes == [1, 2]
        assert any("Rollback della sessione" in r.getMessage() for r in caplog.records
                   if r.levelno >= logging.ERROR)

    def test_task_cancellation_is_never_swallowed(self):
        """CancelledError non e' un'Exception: l'isolamento per ricerca non deve inghiottirla."""
        alert = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            _run_cycle([_search(1), _search(2)], {1: [_price(1)], 2: [_price(2)]}, price_alert=alert)

    def test_valid_response_counts_as_success_even_when_the_store_fails(self):
        scheduler._consecutive_failures[1] = 2

        _run_cycle([_search(1)], {1: [_price(5)]}, price_alert=_alert_failing_for(1))

        assert scheduler._consecutive_failures == {}


# ------------------------- F2 + F4 con SQLAlchemy REALE (SQLite su file temporaneo) ----

def _real_db(tmp_path, count, expire_on_commit=False, date_offset=0):
    engine = create_engine(f"sqlite:///{tmp_path / 'real.db'}", pool_pre_ping=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=expire_on_commit)
    with Session() as setup:
        for i in range(1, count + 1):
            setup.add(MonitoredSearch(
                origin="PSA", destination="CAG",
                departure_date=datetime.now() + timedelta(days=30 + i + date_offset),
                return_date=datetime.now() + timedelta(days=32 + i + date_offset),
                monitor_type=MonitorType.MANUAL, telegram_topic_id=7, active=True,
                flight_key=f"PSACAG{i:04d}"))
        setup.commit()
        ids = [x.id for x in setup.query(MonitoredSearch).order_by(MonitoredSearch.id)]
    return engine, Session, ids


def _real_cycle(Session, engine, ids, script, *, in_use=None, price_alert=None, snapshot_factory=None,
                bot=None):
    """
    Un ciclo con ORM e pool VERI. `script[id]` = esiti per tentativo. Se `in_use` e' una lista di
    dict, registra le connessioni occupate a ogni chiamata allo scraper e a ogni attesa.
    `price_alert=None` usa il VERO alerts.maybe_send_alert.
    """
    attempts = collections.Counter()
    in_use = {"scrape": [], "wait": []} if in_use is None else in_use

    def scrape(origin, destination, departure_date, return_date=None, **kwargs):
        in_use["scrape"].append(engine.pool.checkedout())        # misura PRIMA di toccare il DB
        with Session() as peek:
            search_id = peek.query(MonitoredSearch.id).filter_by(departure_date=departure_date).scalar()
        attempts[search_id] += 1
        outcome = script[search_id][attempts[search_id] - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def fake_sleep(seconds):
        in_use["wait"].append(engine.pool.checkedout())

    patches = [patch.object(scheduler, "SessionLocal", Session),
               patch.object(scheduler, "Bot", return_value=bot or _bot()),
               patch.object(scheduler, "_sleep", new=fake_sleep),
               patch.object(scheduler, "scrape_google", side_effect=scrape)]
    if price_alert is not None:
        patches.append(patch.object(scheduler, "maybe_send_alert", price_alert))
    if snapshot_factory is not None:
        patches.append(patch.object(scheduler, "PriceSnapshot", snapshot_factory))
    for p in patches:
        p.start()
    try:
        asyncio.run(scheduler.run_scraping_cycle())
    finally:
        for p in reversed(patches):
            p.stop()
    return in_use, attempts


class TestRealSessionBehaviour:

    def test_failed_commit_is_rolled_back_and_the_cycle_goes_on(self, tmp_path, caplog):
        """
        Un commit che fallisce davvero (IntegrityError su colonna NOT NULL) lascia la sessione
        inattiva: SENZA rollback ogni ricerca successiva fallirebbe con PendingRollbackError.
        """
        engine, Session, ids = _real_db(tmp_path, 3)
        real_snapshot = PriceSnapshot

        def snapshot_factory(**kwargs):
            if kwargs["search_id"] == ids[0]:
                kwargs["price_eur"] = None                       # viola NOT NULL: il commit fallisce
            return real_snapshot(**kwargs)

        script = {ids[0]: [_price(10.0)], ids[1]: [_price(20.0)], ids[2]: [_price(30.0)]}
        alert = AsyncMock()

        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            in_use, _ = _real_cycle(Session, engine, ids, script, price_alert=alert,
                                    snapshot_factory=snapshot_factory)

        with Session() as check:
            saved = sorted((s.search_id, s.price_eur) for s in check.query(PriceSnapshot).all())
        assert saved == [(ids[1], 20.0), (ids[2], 30.0)]         # la 1ª non salvata, le altre si'
        assert [c.args[2].id for c in alert.call_args_list] == [ids[1], ids[2]]   # nessun alert per la 1ª
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(messages) == 1 and f"search_id={ids[0]}" in messages[0]
        assert not any("Scraping fallito" in m for m in messages)  # nessuna cascata (PendingRollbackError)
        assert set(in_use["scrape"]) == {0}, in_use              # e nessuna connessione trattenuta
        assert engine.pool.checkedout() == 0
        engine.dispose()


# ------------------------- F4: nessuna connessione DB trattenuta durante operazioni lente ----

class TestConnectionReleasedBeforeSlowOperations:

    def test_connection_is_released_right_before_every_scrape_call(self):
        searches = [_search(i) for i in range(1, 5)]
        outcomes = {1: [_timeout(), _price(11)], 2: [_price(22)], 3: [_timeout()] * 3, 4: [_http(403)]}

        cycle = _run_cycle(searches, outcomes)

        scrape_positions = [i for i, e in enumerate(cycle.events) if e[0] == "scrape"]
        # tentativi: ricerca 1 -> 2, ricerca 2 -> 1, ricerca 3 -> 3, ricerca 4 -> 1
        assert len(scrape_positions) == 2 + 1 + 3 + 1
        for position in scrape_positions:
            assert cycle.events[position - 1] == ("commit",), (position, cycle.events)

    def test_connection_is_released_before_the_admin_alert(self):
        events = []
        bot = _bot()
        bot.send_message.side_effect = lambda **kwargs: events.append(("alert",))
        scheduler._consecutive_failures[1] = 2

        _run_cycle([_search(1)], {1: [_http(403)]}, bot=bot, events=events)

        assert events[-2:] == [("commit",), ("alert",)]

    @pytest.mark.parametrize("expire_on_commit", [False, True], ids=["expire_False", "expire_True"])
    def test_real_orm_with_real_alerts_holds_no_connection_while_scraping_or_waiting(
            self, tmp_path, expire_on_commit):
        """
        ORM e pool veri, e il VERO alerts.maybe_send_alert (che interroga il DB e lascia una
        transazione aperta). Prima del fix: [0, 1, 1, 1, ...]. Ora 0 ovunque, anche se la
        configurazione della sessione cambiasse (expire_on_commit=True): gli attributi della
        ricerca si leggono PRIMA di rilasciare la connessione.
        """
        engine, Session, ids = _real_db(tmp_path, 4, expire_on_commit=expire_on_commit)
        script = {ids[0]: [_price(100.0)],                       # successo (alert reale valutato)
                  ids[1]: [_timeout(), _price(90.0)],            # recupera al retry
                  ids[2]: [_price(80.0)],
                  ids[3]: [_http(403)]}                          # errore non ritentabile

        in_use, attempts = _real_cycle(Session, engine, ids, script)     # alerts REALI

        assert len(in_use["scrape"]) == 5 and len(in_use["wait"]) == 1
        assert set(in_use["scrape"]) == {0}, in_use
        assert set(in_use["wait"]) == {0}, in_use
        assert engine.pool.checkedout() == 0
        with Session() as check:
            assert sorted(s.search_id for s in check.query(PriceSnapshot).all()) == sorted(ids[:3])
        engine.dispose()

    def test_connection_stays_released_after_a_failed_store_expired_the_objects(self, tmp_path):
        """
        Dopo un rollback gli oggetti scadono e rileggerli riaprirebbe una transazione: il
        rilascio subito prima dello scraping deve valere anche in questo percorso di errore.
        """
        engine, Session, ids = _real_db(tmp_path, 3)
        real_snapshot = PriceSnapshot

        def snapshot_factory(**kwargs):
            if kwargs["search_id"] == ids[0]:
                kwargs["price_eur"] = None
            return real_snapshot(**kwargs)

        script = {i: [_price(10.0 + n)] for n, i in enumerate(ids)}

        in_use, _ = _real_cycle(Session, engine, ids, script, price_alert=AsyncMock(),
                                snapshot_factory=snapshot_factory)

        assert in_use["scrape"] == [0, 0, 0], in_use
        engine.dispose()


class TestAdminAlertAfterARollback:

    def test_no_connection_is_held_while_sending_the_admin_alert_even_after_a_failed_commit(self, tmp_path):
        """
        Il commit fallisce sull'ULTIMA ricerca, quando quelle in errore persistente sono gia'
        state scansionate: il rollback fa scadere anche loro. I dati dell'alert vanno quindi
        letti PRIMA di rilasciare la connessione, altrimenti la rilettura riaprirebbe una
        transazione che resterebbe aperta durante l'invio (misurato: 1 connessione).
        """
        engine, Session, ids = _real_db(tmp_path, 4)
        scheduler._consecutive_failures.update({ids[0]: 2, ids[1]: 2})   # 1 e 2 arrivano a 3 fallimenti
        seen = {}
        bot = MagicMock()

        async def send(**kwargs):
            seen["in_use"] = engine.pool.checkedout()
            seen["text"] = kwargs["text"]

        bot.send_message = AsyncMock(side_effect=send)
        real_snapshot = PriceSnapshot

        def snapshot_factory(**kwargs):
            if kwargs["search_id"] == ids[3]:
                kwargs["price_eur"] = None                       # commit fallito davvero + rollback
            return real_snapshot(**kwargs)

        script = {ids[0]: [_http(403)], ids[1]: [_http(403)], ids[2]: [_price(30.0)], ids[3]: [_price(40.0)]}

        in_use, _ = _real_cycle(Session, engine, ids, script, bot=bot, price_alert=AsyncMock(),
                                snapshot_factory=snapshot_factory)

        assert seen["in_use"] == 0, seen                         # nessuna connessione durante l'invio
        assert "2 ricerche hanno fallito" in seen["text"]        # i dati sono stati letti correttamente
        assert set(in_use["scrape"]) == {0}
        engine.dispose()

    def test_only_the_chronic_searches_are_read_for_the_alert(self):
        """Non si rileggono (N+1) tutte le ricerche: l'alert guarda solo quelle in errore persistente."""
        searches = [_search(i) for i in range(1, 6)]
        scheduler._consecutive_failures[2] = 3
        reads = collections.Counter()

        class Tracked:
            def __init__(self, inner): self._inner = inner
            def __getattr__(self, name):
                reads[(self._inner.id, name)] += 1
                return getattr(self._inner, name)

        tracked = [Tracked(s) for s in searches]
        tracked_by_id = {s.id: t for s, t in zip(searches, tracked)}

        asyncio.run(scheduler._update_admin_alert(MagicMock(), _bot(), tracked_by_id))

        read_ids = {search_id for (search_id, _name) in reads}
        assert read_ids == {2}
