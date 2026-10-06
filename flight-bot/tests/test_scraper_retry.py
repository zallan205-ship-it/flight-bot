"""
Test della retry policy dello scheduler (scheduler.py), del tracking dei fallimenti
consecutivi per ricerca e dell'alert amministrativo.

Policy sotto test (livello APPLICATIVO: lo scraper non ritenta mai):

    RETRY    : timeout, HTTP 429, HTTP 5xx
    NO RETRY : HTTP 403 (e altri 4xx), FlightsNotFound, errori del parser,
               errori strutturali del consenso, errori di connessione/DNS non di
               timeout, cause sconosciute
    massimo 2 retry (3 tentativi), attese ~60 s e ~180 s con jitter ±20% e tetto 180 s
    un "fallimento" = fallimento DEFINITIVO di una ricerca in un ciclo (dopo i retry)
    alert amministrativo al 3° fallimento consecutivo della STESSA ricerca (una sola volta)

Tutto offline: scraper/rete, DB, Bot, alert di prezzo e attese sono mockati. Nessun
sleep reale: un'attesa reale > 0 fa fallire il test (fixture autouse in conftest.py).
"""
import asyncio
import logging
import os
import random
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import primp
import pytest
from fast_flights.exceptions import FlightsNotFound

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
from scrapers import ScrapedPrice  # noqa: E402
from scrapers.google_flights import (  # noqa: E402
    ScraperError, _submit_consent_form, search_leg,
)

DEPARTURE = datetime(2026, 12, 25)
CONSENT_URL = "https://consent.google.com/m?continue=https://www.google.com/travel/flights"
GOOGLE_ERROR_PAGE = (
    "<html><body><script class=\"ds:1\">"
    "AF_initDataCallback({key: 'ds:1', hash: '2', data:errorHasStatus: true, sideChannel: {}});"
    "</script></body></html>"
)
UNUSABLE_PAGE = "<html><body>traffico insolito rilevato</body></html>"


# ------------------------------------------------------------- costruzione errori ----

def _scraper_error(message, cause=None):
    """ScraperError sollevata `from cause`, come fa lo scraper (cause=None: nessuna causa)."""
    try:
        if cause is None:
            raise ScraperError(message)
        try:
            raise cause
        except Exception as exc:
            raise ScraperError(message) from exc
    except ScraperError as err:
        return err


def _status_error(status):
    """primp.StatusError con il layout REALE osservato: args = (status:int, messaggio, url)."""
    url = "https://example.test/x"
    return primp.StatusError(status, f"HTTP {status} for URL: {url}", url)


def _response(text="<html>ok</html>", url="https://www.google.com/travel/flights", status=200):
    response = MagicMock()
    response.text = text
    response.url = url
    response.status_code = status
    if status >= 400:
        response.raise_for_status.side_effect = _status_error(status)
    return response


def _real_scraper_error(scenario):
    """
    ScraperError ottenuta eseguendo lo scraper VERO (search_leg / consenso), con il
    solo client HTTP mockato: la catena `__cause__` e' quella reale.
    """
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
    """Quali errori si ritentano. Classificazione sul TIPO della causa, mai sul testo."""

    RETRYABLE = ["timeout", "dns_timeout", "http_500", "http_502", "http_503", "http_504",
                 "http_429"]
    NOT_RETRYABLE = ["http_403", "http_400", "http_404", "flights_not_found", "parser",
                     "consent_no_form", "connect_error", "dns_error"]

    @pytest.mark.parametrize("scenario", RETRYABLE)
    def test_temporary_errors_from_the_real_scraper_are_retryable(self, scenario):
        assert scheduler._is_retryable(_real_scraper_error(scenario)) is True

    @pytest.mark.parametrize("scenario", NOT_RETRYABLE)
    def test_other_errors_from_the_real_scraper_are_not_retryable(self, scenario):
        assert scheduler._is_retryable(_real_scraper_error(scenario)) is False

    def test_consent_post_timeout_is_classified_by_its_transport_cause(self):
        """
        Decisione da confermare (vedi report): il brief dice "consent error: no retry",
        inteso come errore STRUTTURALE del consenso (nessun form / nessuna action: causa
        assente). Un timeout nel POST del consenso e' invece un timeout di trasporto.
        """
        assert scheduler._is_retryable(_real_scraper_error("consent_post_timeout")) is True

    @pytest.mark.parametrize("status,expected", [
        (429, True), (500, True), (501, True), (502, True), (503, True), (504, True), (599, True),
        (403, False), (400, False), (401, False), (404, False), (408, False), (499, False),
    ])
    def test_http_status_boundaries(self, status, expected):
        error = _scraper_error(f"errore HTTP {status}", _status_error(status))
        assert scheduler._is_retryable(error) is expected

    @pytest.mark.parametrize("cause", [
        AttributeError("'NoneType' object has no attribute 'text'"), IndexError("fuori range"),
        KeyError("chiave"), TypeError("tipo"), ValueError("valore"),
        FlightsNotFound("no flights found; received error"),
    ], ids=lambda c: type(c).__name__)
    def test_parser_and_unknown_causes_are_not_retryable(self, cause):
        assert scheduler._is_retryable(_scraper_error("analisi fallita", cause)) is False

    def test_error_without_cause_is_not_retryable(self):
        assert scheduler._is_retryable(_scraper_error("consenso senza form")) is False

    def test_classification_ignores_the_message_text(self):
        """Il testo dice 'timeout 503' ma la causa e' un errore del parser: NO retry."""
        misleading = _scraper_error("timeout HTTP 503 Service Unavailable 429",
                                    AttributeError("x"))
        assert scheduler._is_retryable(misleading) is False
        """... e viceversa: messaggio neutro, causa timeout: SI' retry."""
        neutral = _scraper_error("errore", primp.TimeoutError("t"))
        assert scheduler._is_retryable(neutral) is True

    def test_unreadable_http_status_is_not_retried_and_is_logged(self, caplog):
        """Layout inatteso di primp (args[0] non e' un int): scelta prudente + log."""
        odd = _scraper_error("errore HTTP", primp.StatusError("HTTP 503 Service Unavailable"))

        with caplog.at_level(logging.WARNING, logger="flight_bot.scheduler"):
            assert scheduler._is_retryable(odd) is False

        assert any("status HTTP" in r.getMessage() for r in caplog.records)

    def test_status_error_without_args_is_not_retryable(self):
        assert scheduler._is_retryable(_scraper_error("errore", primp.StatusError())) is False


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
        # il jitter e' estratto dall'intervallo [1-20%, 1+20%]
        uniform.assert_called_with(pytest.approx(0.8), pytest.approx(1.2))

    def test_jitter_is_applied(self):
        with patch.object(scheduler.random, "uniform", return_value=1.15):
            assert scheduler._retry_delay(0) == pytest.approx(69.0)
            assert scheduler._retry_delay(0) != 60.0

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
        assert len(set(first)) > 100 and len(set(second)) > 100   # il jitter varia davvero
        assert min(first) < 55 and max(first) > 65        # ~60 s, non fisso

    def test_wait_is_cooperative_and_uses_asyncio_sleep(self, monkeypatch):
        sleep = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep)

        assert asyncio.iscoroutinefunction(scheduler._sleep)
        asyncio.run(scheduler._sleep(42))

        sleep.assert_awaited_once_with(42)


# ------------------------------------------------------ 3. sequenza dei tentativi ----

def _search(search_id, origin="PSA", destination="CAG"):
    return SimpleNamespace(
        id=search_id,
        flight_key=f"{origin}{destination}{search_id:04d}",
        origin=origin,
        destination=destination,
        departure_date=datetime.now() + timedelta(days=30 + search_id),
        return_date=datetime.now() + timedelta(days=32 + search_id),
        monitor_type=scheduler.MonitorType.MANUAL,
        telegram_topic_id=100 + search_id,
        earliest_departure_hour=None, latest_departure_hour=None,
        earliest_return_hour=None, latest_return_hour=None,
    )


def _price(value):
    return ScrapedPrice(
        price_eur=value, origin="PSA", destination="CAG",
        departure_date=datetime(2026, 12, 25), return_date=datetime(2026, 12, 31),
        source="google_flights", departure_time="18:30", return_time="20:15",
    )


def _timeout():
    return _scraper_error("Richiesta GET a Google fallita (TimeoutError)",
                          primp.TimeoutError("operation timed out"))


def _http(status):
    return _scraper_error(f"Google ha risposto con errore HTTP {status}", _status_error(status))


def _scrape_sequence(outcomes):
    """
    side_effect per scrape_google: esiti nell'ordine delle chiamate. Se lo scheduler
    chiama piu' volte del previsto il test FALLISCE (non si blocca, non cicla).
    """
    calls = []

    def scrape(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) > len(outcomes):
            raise AssertionError(f"scrape_google chiamata {len(calls)} volte: troppi tentativi")
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    scrape.calls = calls
    return scrape


def _retry(outcomes, jitter=1.0):
    """Esegue _scrape_with_retry. Ritorna (risultato_o_eccezione, scrape_fn, sleep_mock)."""
    scrape = _scrape_sequence(outcomes)
    sleep = AsyncMock()
    search = _search(1)
    with patch.object(scheduler, "scrape_google", side_effect=scrape), \
         patch.object(scheduler, "_sleep", new=sleep), \
         patch.object(scheduler.random, "uniform", return_value=jitter):
        try:
            result = asyncio.run(scheduler._scrape_with_retry(search, direct_only=True))
        except ScraperError as error:
            result = error
    return result, scrape, sleep


def _delays(sleep):
    return [call.args[0] for call in sleep.await_args_list]


class TestRetrySequence:

    def test_success_does_not_retry(self):
        result, scrape, sleep = _retry([_price(50.0)])

        assert result.price_eur == 50.0
        assert len(scrape.calls) == 1
        sleep.assert_not_awaited()

    def test_valid_no_flights_does_not_retry(self):
        result, scrape, sleep = _retry([None])

        assert result is None
        assert len(scrape.calls) == 1
        sleep.assert_not_awaited()

    @pytest.mark.parametrize("error", [_timeout(), _http(500), _http(503), _http(429)],
                             ids=["timeout", "http500", "http503", "http429"])
    def test_temporary_error_is_retried_and_the_retry_can_succeed(self, error):
        result, scrape, sleep = _retry([error, _price(55.0)])

        assert result.price_eur == 55.0
        assert len(scrape.calls) == 2
        assert _delays(sleep) == [pytest.approx(60.0)]

    @pytest.mark.parametrize("error", [
        _http(403), _http(404),
        _scraper_error("risposta di errore", FlightsNotFound("no flights found; received error")),
        _scraper_error("analisi fallita", AttributeError("x")),
        _scraper_error("consenso senza form"),
    ], ids=["http403", "http404", "flights_not_found", "parser", "consent_structure"])
    def test_non_retryable_error_fails_immediately(self, error):
        result, scrape, sleep = _retry([error, _price(55.0)])

        assert result is error                      # propagato com'e', nessun retry
        assert len(scrape.calls) == 1
        sleep.assert_not_awaited()

    def test_second_retry_runs_when_the_first_retry_fails(self):
        result, scrape, sleep = _retry([_timeout(), _http(503), _price(60.0)])

        assert result.price_eur == 60.0             # il 2° retry (3° tentativo) riesce
        assert len(scrape.calls) == 3
        assert _delays(sleep) == [pytest.approx(60.0), pytest.approx(180.0)]

    def test_all_attempts_fail_gives_definitive_failure_with_the_last_error(self):
        errors = [_timeout(), _http(503), _http(429)]
        result, scrape, sleep = _retry(errors)

        assert result is errors[-1]
        assert len(scrape.calls) == 3
        assert len(_delays(sleep)) == 2

    def test_never_more_than_two_retries(self):
        """Anche se lo scraper continuasse a fallire: 1 tentativo + 2 retry, poi stop."""
        result, scrape, sleep = _retry([_timeout()] * 3)    # una 4ª chiamata farebbe fallire il test

        assert isinstance(result, ScraperError)
        assert len(scrape.calls) == 3
        assert sleep.await_count == 2

    def test_a_non_retryable_error_during_a_retry_stops_the_retries(self):
        forbidden = _http(403)
        result, scrape, sleep = _retry([_timeout(), forbidden, _price(60.0)])

        assert result is forbidden
        assert len(scrape.calls) == 2
        assert sleep.await_count == 1

    def test_every_attempt_uses_the_same_arguments(self):
        _, scrape, _ = _retry([_timeout(), _timeout(), _price(1.0)])

        assert len({repr(c) for c in scrape.calls}) == 1
        args, kwargs = scrape.calls[0]
        assert args[:2] == ("PSA", "CAG") and kwargs == {"direct_only": True}

    def test_retry_waits_are_logged_without_a_traceback(self, caplog):
        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            _retry([_timeout(), _price(1.0)])

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "search_id=1" in warnings[0].getMessage()
        assert "retry 1/2" in warnings[0].getMessage()
        assert warnings[0].exc_info is None


# --------------------------------------------------------- 4. failure tracking ----

class TestFailureTracking:

    def test_consecutive_failures_count_up(self):
        assert scheduler._record_failure(1) == 1
        assert scheduler._record_failure(1) == 2
        assert scheduler._record_failure(1) == 3

    def test_success_resets_the_counter(self):
        scheduler._record_failure(1)
        scheduler._record_failure(1)

        scheduler._record_success(1)

        assert 1 not in scheduler._consecutive_failures
        assert scheduler._record_failure(1) == 1

    def test_success_without_previous_failures_is_harmless(self):
        scheduler._record_success(99)
        assert scheduler._consecutive_failures == {}

    def test_different_searches_have_independent_counters(self):
        scheduler._record_failure(1)
        scheduler._record_failure(1)
        scheduler._record_failure(2)

        assert scheduler._consecutive_failures == {1: 2, 2: 1}

        scheduler._record_success(2)                 # il successo di B non tocca A

        assert scheduler._consecutive_failures == {1: 2}


# ---------------------------------------------------------- 5. ciclo end-to-end ----

def _run_cycle(searches, outcomes_by_search, bot=None, sleep=None):
    """
    Esegue un ciclo con DB, Bot, scraper, alert di prezzo e attese mockati.
    `outcomes_by_search[i]`: lista di esiti (uno per tentativo) della ricerca i.
    Ritorna (session, price_alert_mock, sleep_mock, bot_mock).
    """
    session = MagicMock()
    session.query.return_value.filter_by.return_value.all.return_value = searches
    price_alert = AsyncMock()
    sleep = sleep or AsyncMock()
    bot = bot or MagicMock(send_message=AsyncMock())
    scrapers = {s.departure_date: _scrape_sequence(outcomes_by_search[i])
                for i, s in enumerate(searches)}

    def scrape(origin, destination, departure_date, return_date=None, **kwargs):
        return scrapers[departure_date](origin, destination, departure_date, return_date, **kwargs)

    with patch.object(scheduler, "SessionLocal", return_value=session), \
         patch.object(scheduler, "Bot", return_value=bot), \
         patch.object(scheduler, "_sleep", new=sleep), \
         patch.object(scheduler.random, "uniform", return_value=1.0), \
         patch.object(scheduler, "scrape_google", side_effect=scrape), \
         patch.object(scheduler, "maybe_send_alert", price_alert):
        asyncio.run(scheduler.run_scraping_cycle())
    return session, price_alert, sleep, bot


def _snapshot_ids(session):
    return [call.args[0].search_id for call in session.add.call_args_list]


def _price_alert_ids(price_alert):
    return [call.args[2].id for call in price_alert.call_args_list]


class TestSchedulerCycleWithRetry:

    def test_search_recovering_after_a_retry_produces_the_normal_result(self):
        searches = [_search(1)]

        session, price_alert, sleep, _ = _run_cycle(searches, [[_timeout(), _price(70.0)]])

        assert [(s.args[0].search_id, s.args[0].price_eur)
                for s in session.add.call_args_list] == [(1, 70.0)]
        assert _price_alert_ids(price_alert) == [1]          # alert di prezzo normale
        assert sleep.await_count == 1
        assert scheduler._consecutive_failures == {}

    def test_definitive_failure_creates_no_snapshot_and_no_price_alert(self):
        searches = [_search(1)]

        session, price_alert, sleep, _ = _run_cycle(searches, [[_timeout()] * 3])

        session.add.assert_not_called()                      # nessuno snapshot falso
        session.commit.assert_not_called()
        price_alert.assert_not_called()                      # nessun alert di prezzo falso
        assert sleep.await_count == 2                        # 2 retry prima di arrendersi

    def test_definitive_failure_does_not_stop_the_following_searches(self):
        searches = [_search(1), _search(2), _search(3)]

        session, price_alert, sleep, _ = _run_cycle(
            searches, [[_timeout()] * 3, [_price(50.0)], [_http(403)]])

        assert _snapshot_ids(session) == [2]
        assert _price_alert_ids(price_alert) == [2]
        assert scheduler._consecutive_failures == {1: 1, 3: 1}

    def test_a_failure_in_the_middle_lets_later_searches_run(self):
        searches = [_search(1), _search(2), _search(3)]

        session, price_alert, _, _ = _run_cycle(
            searches, [[_price(50.0)], [_http(503)] * 3, [_price(60.0)]])

        assert _snapshot_ids(session) == [1, 3]
        assert _price_alert_ids(price_alert) == [1, 3]

    def test_retries_of_one_search_count_as_a_single_failure(self):
        """3 tentativi falliti nello stesso ciclo = 1 fallimento (non 3): evita alert immediati."""
        _run_cycle([_search(1)], [[_timeout()] * 3])

        assert scheduler._consecutive_failures == {1: 1}

    def test_non_retryable_failure_counts_too(self):
        _run_cycle([_search(1)], [[_http(403)]])

        assert scheduler._consecutive_failures == {1: 1}

    def test_valid_no_flights_resets_the_counter(self):
        scheduler._consecutive_failures[1] = 2

        _run_cycle([_search(1)], [[None]])

        assert scheduler._consecutive_failures == {}

    def test_price_result_resets_the_counter(self):
        scheduler._consecutive_failures[1] = 2

        _run_cycle([_search(1)], [[_price(80.0)]])

        assert scheduler._consecutive_failures == {}

    def test_success_after_retry_resets_the_counter(self):
        scheduler._consecutive_failures[1] = 2

        _run_cycle([_search(1)], [[_timeout(), _price(80.0)]])

        assert scheduler._consecutive_failures == {}

    def test_unexpected_exceptions_neither_count_nor_reset(self):
        scheduler._consecutive_failures[1] = 2

        _run_cycle([_search(1), _search(2)], [[ValueError("bug")], [_price(1.0)]])

        assert scheduler._consecutive_failures == {1: 2}     # invariato

    def test_counters_are_per_search_across_cycles(self):
        a, b = _search(1), _search(2)

        _run_cycle([a, b], [[_http(403)], [_price(1.0)]])
        _run_cycle([a, b], [[_http(403)], [_http(403)]])

        assert scheduler._consecutive_failures == {1: 2, 2: 1}

    def test_attempts_do_not_block_the_event_loop_during_the_wait(self):
        """
        L'attesa del backoff cede il controllo all'event loop: un task concorrente
        (il bot) continua a girare mentre il ciclo aspetta il retry.
        """
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
            session = MagicMock()
            session.query.return_value.filter_by.return_value.all.return_value = [_search(1)]
            with patch.object(scheduler, "SessionLocal", return_value=session), \
                 patch.object(scheduler, "Bot"), \
                 patch.object(scheduler, "_sleep", new=cooperative_wait), \
                 patch.object(scheduler.random, "uniform", return_value=1.0), \
                 patch.object(scheduler, "scrape_google",
                              side_effect=_scrape_sequence([_timeout(), _price(1.0)])), \
                 patch.object(scheduler, "maybe_send_alert", AsyncMock()):
                await scheduler.run_scraping_cycle()
            task.cancel()
            return len(beats)

        beats = asyncio.run(scenario())

        assert waits == [pytest.approx(60.0)]
        assert beats >= yields                       # il heartbeat e' avanzato durante l'attesa


# ----------------------------------------------------------- 6. alert amministrativo ----

TECH_SECRET = "SECRET-TECH-DETAIL-98765"


def _failing_cycle(bot, searches=None, error_factory=None):
    searches = searches or [_search(1)]
    factory = error_factory or (lambda: _http(403))
    return _run_cycle(searches, [[factory()] for _ in searches], bot=bot)


class TestAdminAlert:

    def _bot(self):
        return MagicMock(send_message=AsyncMock())

    def test_no_alert_on_the_first_and_second_failure(self):
        bot = self._bot()

        _failing_cycle(bot)
        _failing_cycle(bot)

        bot.send_message.assert_not_awaited()
        assert scheduler._consecutive_failures == {1: 2}

    def test_alert_on_the_third_consecutive_failure(self):
        bot = self._bot()

        for _ in range(3):
            _failing_cycle(bot)

        bot.send_message.assert_awaited_once()
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == scheduler.TELEGRAM_CHAT_ID == 1
        assert kwargs["message_thread_id"] == 101           # topic della ricerca

    def test_no_duplicate_alerts_from_the_fourth_failure_on(self):
        bot = self._bot()

        for _ in range(7):
            _failing_cycle(bot)

        assert bot.send_message.await_count == 1
        assert scheduler._consecutive_failures == {1: 7}

    def test_success_resets_and_a_new_streak_alerts_again(self):
        bot = self._bot()
        for _ in range(3):
            _failing_cycle(bot)
        assert bot.send_message.await_count == 1

        _run_cycle([_search(1)], [[_price(10.0)]], bot=bot)       # la ricerca torna a funzionare
        for _ in range(2):
            _failing_cycle(bot)
        assert bot.send_message.await_count == 1                  # serie nuova: ancora a 2

        _failing_cycle(bot)
        assert bot.send_message.await_count == 2                  # 3° della nuova serie

    def test_alert_text_has_only_appropriate_information(self):
        bot = self._bot()
        error = _scraper_error(
            f"Google ha risposto con un errore (FlightsNotFound / errorHasStatus: {TECH_SECRET})",
            FlightsNotFound(TECH_SECRET))
        # errore NON ritentabile e con testo tecnico "riconoscibile"
        for _ in range(3):
            _run_cycle([_search(1)], [[error]], bot=bot)

        text = bot.send_message.await_args.kwargs["text"]
        assert text.startswith("⚠️ Scraper Google Flights")
        assert "3 controlli consecutivi falliti" in text
        assert "PSA → CAG" in text
        assert _search(1).departure_date.strftime("%d/%m/%Y") in text
        assert "Il monitoraggio continua automaticamente." in text
        for forbidden in ("ScraperError", "FlightsNotFound", "errorHasStatus", "Traceback",
                          "HTTP", "primp", "timeout", "search_id", TECH_SECRET, "Exception"):
            assert forbidden.lower() not in text.lower(), forbidden

    def test_technical_details_stay_in_the_logs(self, caplog):
        bot = self._bot()
        error = _scraper_error(f"errore {TECH_SECRET}", FlightsNotFound(TECH_SECRET))

        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            for _ in range(3):
                _run_cycle([_search(1)], [[error]], bot=bot)

        assert TECH_SECRET in caplog.text                  # dettaglio completo nel log
        assert TECH_SECRET not in bot.send_message.await_args.kwargs["text"]

    def test_round_trip_dates_are_shown_in_the_alert(self):
        bot = self._bot()
        for _ in range(3):
            _failing_cycle(bot)

        text = bot.send_message.await_args.kwargs["text"]
        s = _search(1)
        assert f"({s.departure_date:%d/%m/%Y} - {s.return_date:%d/%m/%Y})" in text

    def test_one_way_search_shows_only_the_departure_date(self):
        bot = self._bot()
        one_way = _search(1)
        one_way.return_date = None
        for _ in range(3):
            _run_cycle([one_way], [[_http(403)]], bot=bot)

        text = bot.send_message.await_args.kwargs["text"]
        assert f"({one_way.departure_date:%d/%m/%Y})." in text

    def test_each_search_alerts_on_its_own_counter(self):
        bot = self._bot()
        a, b = _search(1), _search(2)

        for i in range(3):
            # B fallisce solo nei primi 2 cicli, poi recupera
            b_outcome = [_http(403)] if i < 2 else [_price(5.0)]
            _run_cycle([a, b], [[_http(403)], b_outcome], bot=bot)

        assert bot.send_message.await_count == 1
        assert bot.send_message.await_args.kwargs["message_thread_id"] == 101   # solo la ricerca A
        assert scheduler._consecutive_failures == {1: 3}

    def test_retries_alone_never_trigger_an_alert(self):
        bot = self._bot()

        _run_cycle([_search(1)], [[_timeout()] * 3], bot=bot)    # 3 tentativi, 1 solo fallimento

        bot.send_message.assert_not_awaited()

    def test_alert_failure_does_not_break_the_cycle(self, caplog):
        bot = MagicMock(send_message=AsyncMock(side_effect=RuntimeError("telegram giu'")))
        scheduler._consecutive_failures[1] = 2
        searches = [_search(1), _search(2)]

        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            session, price_alert, _, _ = _run_cycle(
                searches, [[_http(403)], [_price(9.0)]], bot=bot)

        bot.send_message.assert_awaited_once()                 # tentato una volta
        assert _snapshot_ids(session) == [2]                   # la ricerca 2 prosegue
        assert _price_alert_ids(price_alert) == [2]
        assert any("alert amministrativo" in r.getMessage() and r.exc_info
                   for r in caplog.records if r.levelno >= logging.ERROR)

    def test_failing_search_does_not_stop_the_cycle_at_the_alert_threshold(self):
        bot = self._bot()
        scheduler._consecutive_failures[1] = 2

        session, price_alert, _, _ = _run_cycle(
            [_search(1), _search(2)], [[_http(403)], [_price(7.0)]], bot=bot)

        bot.send_message.assert_awaited_once()
        assert _snapshot_ids(session) == [2]
        assert _price_alert_ids(price_alert) == [2]

    def test_no_price_alert_or_snapshot_for_the_failing_search(self):
        bot = self._bot()
        scheduler._consecutive_failures[1] = 2

        session, price_alert, _, _ = _run_cycle([_search(1)], [[_http(403)]], bot=bot)

        bot.send_message.assert_awaited_once()                 # alert amministrativo...
        session.add.assert_not_called()                        # ...ma nessun dato falso
        price_alert.assert_not_called()                        # ...e nessun alert di prezzo
