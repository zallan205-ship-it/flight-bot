"""
Test di integrazione: lo scraping sincrono NON deve bloccare l'event loop.

Percorsi coperti: il job run_scraping_cycle (scheduler) e il comando /cerca (bot), che usano lo
stesso runner a un solo thread (scrape_runner). Si dimostra che:
  - il loop continua a servire altre coroutine mentre lo scraper e' in corso;
  - ciclo e /cerca contemporanei non fanno mai due chiamate attive insieme;
  - nel thread dello scraper non transitano ne' sessioni ne' oggetti ORM;
  - le eccezioni attraversano il thread (tipo, timed_out, status_code, __cause__) e la
    classificazione dei retry non cambia;
  - annullamento, arresto e uso dopo l'arresto hanno un comportamento esplicito.

Handshake con threading.Event, nessun time.sleep. ATTENZIONE: dentro la funzione di scraping
finta NON si solleva mai un'asserzione (il `except Exception` del ciclo la inghiottirebbe e il
test passerebbe per sbaglio): si REGISTRA l'esito e lo si verifica nel corpo del test.
"""
import asyncio
import logging
import os
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, event
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

import bot  # noqa: E402
import main  # noqa: E402
import scheduler  # noqa: E402
from models import Base, MonitoredSearch, MonitorType  # noqa: E402
from scrape_runner import ScrapeRunnerClosed  # noqa: E402
from scrapers import ScrapedPrice  # noqa: E402
from scrapers.google_flights import ScraperError  # noqa: E402

GENERIC_ERROR = "Non sono riuscito a recuperare i voli in questo momento. Riprova più tardi."
UNAVAILABLE = "Il servizio di ricerca non è momentaneamente disponibile. Riprova tra poco."


# ---------------------------------------------------------------- infrastruttura ----

def _search(search_id, return_date=False):
    return SimpleNamespace(
        id=search_id,
        flight_key=f"PSACAG{search_id:04d}",
        origin="PSA",
        destination="CAG",
        departure_date=datetime.now() + timedelta(days=30 + search_id),
        return_date=(datetime.now() + timedelta(days=32 + search_id)) if return_date else None,
        monitor_type=MonitorType.MANUAL,
        telegram_topic_id=100 + search_id,
        earliest_departure_hour=None, latest_departure_hour=None,
        earliest_return_hour=None, latest_return_hour=None,
    )


def _price(value):
    return ScrapedPrice(
        price_eur=value, origin="PSA", destination="CAG",
        departure_date=datetime(2026, 12, 25), return_date=None,
        source="google_flights", departure_time="18:30", return_time=None,
    )


async def _run_cycle(searches, scrape, *, session=None, price_alert=None):
    """Un ciclo con DB, Bot e attese mockati; `scrape` e' la funzione di scraping (gira nel thread)."""
    session = session or MagicMock()
    session.query.return_value.filter_by.return_value.all.return_value = searches
    sleep = AsyncMock()
    with patch.object(scheduler, "SessionLocal", return_value=session), \
         patch.object(scheduler, "Bot", return_value=MagicMock(send_message=AsyncMock())), \
         patch.object(scheduler, "_sleep", new=sleep), \
         patch.object(scheduler, "scrape_google", side_effect=scrape), \
         patch.object(scheduler, "maybe_send_alert", price_alert or AsyncMock()):
        await scheduler.run_scraping_cycle()
    return SimpleNamespace(session=session, sleep=sleep)


async def _cerca(search_options, args=("psa", "cag", "25-12-2026")):
    reply = AsyncMock()
    update = SimpleNamespace(message=SimpleNamespace(reply_text=reply))
    with patch.object(bot, "search_options", side_effect=search_options):
        await bot.cerca(update, SimpleNamespace(args=list(args)))
    return [call.args[0] for call in reply.call_args_list]


async def _wait(event_, timeout=5):
    assert await asyncio.to_thread(event_.wait, timeout), "evento non ricevuto"


def _count_submissions(runner, monkeypatch):
    submitted = []
    real_submit = runner._executor.submit

    def counting_submit(fn, *args, **kwargs):
        submitted.append(1)
        return real_submit(fn, *args, **kwargs)

    monkeypatch.setattr(runner._executor, "submit", counting_submit)
    return submitted


async def _until_submitted(submitted, count, limit=20000):
    for _ in range(limit):
        if len(submitted) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"solo {len(submitted)} richieste sottomesse su {count}")


# -------------------------------------------- 1-2. il loop resta libero durante lo scraping ----

class TestEventLoopStaysResponsive:

    def test_the_loop_is_served_while_the_scraping_cycle_waits_for_the_scraper(self):
        served, observed = threading.Event(), {}

        def blocking_scrape(*args, **kwargs):
            observed["thread"] = threading.current_thread().name
            observed["served"] = served.wait(2)      # solo se il loop viene servito durante l'attesa
            return None                              # nessun volo

        async def scenario():
            async def other_coroutine():             # rappresenta il bot Telegram
                await asyncio.sleep(0)
                served.set()
            other = asyncio.create_task(other_coroutine())
            await _run_cycle([_search(1)], blocking_scrape)
            await other

        asyncio.run(scenario())

        assert observed["served"] is True
        assert observed["thread"].startswith("scraper")

    def test_the_loop_is_served_while_cerca_waits_for_search_options(self):
        served, observed = threading.Event(), {}

        def blocking_search_options(*args, **kwargs):
            observed["thread"] = threading.current_thread().name
            observed["served"] = served.wait(2)
            return []

        async def scenario():
            async def other_coroutine():
                await asyncio.sleep(0)
                served.set()
            other = asyncio.create_task(other_coroutine())
            texts = await _cerca(blocking_search_options)
            await other
            return texts

        texts = asyncio.run(scenario())

        assert observed["served"] is True
        assert observed["thread"].startswith("scraper")
        assert texts[1].startswith("Nessun volo senza scali trovato")    # comportamento invariato

    def test_the_whole_pass_over_many_searches_never_starves_the_loop(self):
        """Ogni ricerca cede il controllo: il loop viene servito a ogni singola chiamata."""
        served_count = {"loop_turns": 0}
        turns_seen_by_scraper = []

        def scrape(*args, **kwargs):
            turns_seen_by_scraper.append(served_count["loop_turns"])
            return None

        async def scenario():
            async def heartbeat():
                while True:
                    served_count["loop_turns"] += 1
                    await asyncio.sleep(0)
            task = asyncio.create_task(heartbeat())
            await _run_cycle([_search(i) for i in range(1, 6)], scrape)
            task.cancel()

        asyncio.run(scenario())

        # Il battito avanza fra una chiamata e la successiva: con lo scraping nel loop
        # resterebbe fermo per l'intero passaggio.
        assert turns_seen_by_scraper == sorted(turns_seen_by_scraper)
        assert len(set(turns_seen_by_scraper)) >= 4


# ------------------------------------ 3. ciclo e /cerca insieme: una chiamata alla volta ----

class TestSerialization:

    def test_cycle_and_cerca_never_have_two_active_calls(self, _fresh_scrape_runner, monkeypatch):
        submitted = _count_submissions(_fresh_scrape_runner, monkeypatch)
        lock, gate = threading.Lock(), threading.Event()
        activity = {"now": 0, "max": 0, "calls": []}

        def tracked(label):
            with lock:
                activity["now"] += 1
                activity["max"] = max(activity["max"], activity["now"])
                activity["calls"].append(label)
            gate.wait(5)                             # tiene occupato il worker finche' le richieste sono in coda
            with lock:
                activity["now"] -= 1

        def scrape(*args, **kwargs):
            tracked("ciclo")
            return None

        def search_options(*args, **kwargs):
            tracked("cerca")
            return []

        async def scenario():
            cycle = asyncio.create_task(_run_cycle([_search(1), _search(2)], scrape))
            user = asyncio.create_task(_cerca(search_options))
            await _until_submitted(submitted, 2)     # 1ª chiamata del ciclo + /cerca, ENTRAMBE in coda
            gate.set()
            await asyncio.gather(cycle, user)

        asyncio.run(scenario())

        assert activity["max"] == 1                  # mai due chiamate attive insieme
        assert sorted(activity["calls"]) == ["cerca", "ciclo", "ciclo"]


# ----------------------------------- 4. nel thread niente sessioni ne' oggetti ORM ----

_SIMPLE_TYPES = (str, int, float, bool, datetime, type(None))


class _ThreadSpy:
    """Registra in quale thread viene letto ogni attributo (ORM-like) della ricerca."""

    def __init__(self, inner, log):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_log", log)

    def __getattr__(self, name):
        self._log.append(threading.current_thread().name)
        return getattr(self._inner, name)


class TestNoDatabaseInTheScraperThread:

    def test_only_simple_values_reach_the_scraper(self):
        captured = {}

        def scrape(*args, **kwargs):
            captured["args"], captured["kwargs"] = args, kwargs
            return None

        orm_search = MonitoredSearch(
            id=7, origin="PSA", destination="CAG",
            departure_date=datetime.now() + timedelta(days=30), return_date=None,
            monitor_type=MonitorType.MANUAL, telegram_topic_id=1,
            earliest_departure_hour=17, latest_departure_hour=None,
            earliest_return_hour=None, latest_return_hour=None)

        async def scenario():
            with patch.object(scheduler, "scrape_google", side_effect=scrape):
                await scheduler._scrape_search(MagicMock(), orm_search)

        asyncio.run(scenario())

        values = list(captured["args"]) + list(captured["kwargs"].values())
        assert values, "lo scraper non ha ricevuto argomenti"
        for value in values:
            assert isinstance(value, _SIMPLE_TYPES), f"valore non semplice nel thread: {value!r}"
            assert not isinstance(value, MonitoredSearch)

    def test_search_attributes_and_session_are_only_used_on_the_loop_thread(self):
        attribute_threads, session_threads = [], []
        session = MagicMock()
        session.commit.side_effect = lambda: session_threads.append(threading.current_thread().name)
        session.add.side_effect = lambda *a: session_threads.append(threading.current_thread().name)
        searches = [_ThreadSpy(_search(i), attribute_threads) for i in (1, 2)]
        scraper_threads = []

        def scrape(*args, **kwargs):
            scraper_threads.append(threading.current_thread().name)
            return _price(50.0)

        async def scenario():
            loop_thread = threading.current_thread().name
            await _run_cycle(searches, scrape, session=session)
            return loop_thread

        loop_thread = asyncio.run(scenario())

        assert attribute_threads and session_threads            # il controllo non e' vuoto
        assert set(attribute_threads) == {loop_thread}          # attributi ORM: solo nel loop
        assert set(session_threads) == {loop_thread}            # sessione: solo nel loop
        assert scraper_threads and all(t.startswith("scraper") for t in scraper_threads)

    def test_no_sql_statement_is_issued_from_the_scraper_thread(self, tmp_path):
        """Con ORM, pool e alerts.py REALI: nessuna istruzione SQL parte dal thread dello scraper."""
        engine = create_engine(f"sqlite:///{tmp_path / 'threads.db'}", pool_pre_ping=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        with Session() as setup:
            for i in (1, 2, 3):
                setup.add(MonitoredSearch(
                    origin="PSA", destination="CAG",
                    departure_date=datetime.now() + timedelta(days=30 + i), return_date=None,
                    monitor_type=MonitorType.MANUAL, telegram_topic_id=7, active=True,
                    flight_key=f"PSACAG{i:04d}"))
            setup.commit()
            by_departure = {s.departure_date: s.id for s in setup.query(MonitoredSearch)}
        statement_threads, scraper_threads = [], []
        event.listen(engine, "before_cursor_execute",
                     lambda *a: statement_threads.append(threading.current_thread().name))

        def scrape(origin, destination, departure_date, return_date=None, **kwargs):
            scraper_threads.append(threading.current_thread().name)
            return _price(40.0 + by_departure[departure_date])      # nessun accesso al DB qui

        async def scenario():
            statement_threads.clear()
            with patch.object(scheduler, "SessionLocal", Session), \
                 patch.object(scheduler, "Bot", return_value=MagicMock(send_message=AsyncMock())), \
                 patch.object(scheduler, "_sleep", new=AsyncMock()), \
                 patch.object(scheduler, "scrape_google", side_effect=scrape):
                await scheduler.run_scraping_cycle()
            return threading.current_thread().name

        loop_thread = asyncio.run(scenario())

        assert len(statement_threads) > 5                        # il cycle ha davvero usato il DB
        assert set(statement_threads) == {loop_thread}           # ...sempre dal thread del loop
        assert len(scraper_threads) == 3
        assert all(t.startswith("scraper") for t in scraper_threads)
        engine.dispose()


# -------------------- 5. le eccezioni attraversano il thread e la classificazione non cambia ----

class TestExceptionsAndRetryClassificationUnchanged:

    @pytest.mark.parametrize("error_kwargs,expected_attempts", [
        ({"timed_out": True}, 2),              # timeout        -> ritentato
        ({"status_code": 503}, 2),             # HTTP 5xx       -> ritentato
        ({"status_code": 429}, 2),             # HTTP 429       -> ritentato
        ({"status_code": 403}, 1),             # HTTP 403       -> NON ritentato
        ({}, 1),                               # errore generico -> NON ritentato
    ], ids=["timeout", "http503", "http429", "http403", "generic"])
    def test_attributes_set_in_the_thread_drive_the_retry_decision(self, error_kwargs, expected_attempts):
        calls = []

        def scrape(*args, **kwargs):
            calls.append(threading.current_thread().name)
            if len(calls) == 1:
                raise ScraperError("errore", **error_kwargs)
            return None

        result = asyncio.run(_run_cycle([_search(1)], scrape))

        assert len(calls) == expected_attempts
        assert result.sleep.await_count == expected_attempts - 1

    def test_cerca_keeps_the_original_error_object_and_cause_for_the_logs(self, caplog):
        cause = TimeoutError("operation timed out")
        error = ScraperError("richiesta fallita", timed_out=True)
        error.__cause__ = cause

        def search_options(*args, **kwargs):
            raise error

        with caplog.at_level(logging.INFO, logger="flight_bot.bot"):
            texts = asyncio.run(_cerca(search_options))

        assert texts[-1] == GENERIC_ERROR                        # all'utente: messaggio generico
        record = [r for r in caplog.records if r.name == "flight_bot.bot"][0]
        assert record.exc_info[1] is error                       # nei log: l'oggetto originale
        assert record.exc_info[1].__cause__ is cause
        assert record.exc_info[1].timed_out is True

    def test_unexpected_errors_in_the_thread_keep_their_existing_handling(self):
        def search_options(*args, **kwargs):
            raise ValueError("boom")

        texts = asyncio.run(_cerca(search_options))

        assert texts[-1] == "Errore durante la ricerca: boom"


# ------------------------------------------- 6. annullamento del chiamante (ciclo) ----

class TestCancellationOfTheCycle:

    def test_cancelling_the_cycle_during_a_scrape_closes_the_session_and_keeps_the_runner_usable(
            self, _fresh_scrape_runner):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        session = MagicMock()

        def slow_scrape(*args, **kwargs):
            started.set()
            release.wait(5)
            finished.set()
            return None

        async def scenario():
            task = asyncio.create_task(_run_cycle([_search(1), _search(2)], slow_scrape, session=session))
            await _wait(started)
            task.cancel()                            # come fa APScheduler allo shutdown
            with pytest.raises(asyncio.CancelledError):
                await task
            session.close.assert_called_once()       # il `finally` del ciclo ha chiuso la sessione
            release.set()
            await _wait(finished)                    # la chiamata in corso termina da sola
            return await _fresh_scrape_runner.run(lambda: "ancora utilizzabile")

        assert asyncio.run(scenario()) == "ancora utilizzabile"


# ------------------------------------------------- 7. uso dopo lo shutdown del runner ----

class TestUseAfterShutdown:

    def test_the_cycle_survives_a_closed_runner_without_counting_failures(self, _fresh_scrape_runner, caplog):
        _fresh_scrape_runner.shutdown()
        calls = []

        def scrape(*args, **kwargs):
            calls.append(1)

        with caplog.at_level(logging.INFO, logger="flight_bot.scheduler"):
            result = asyncio.run(_run_cycle([_search(1), _search(2)], scrape))

        assert calls == []                                       # nessuna chiamata allo scraper
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 2
        assert all(isinstance(r.exc_info[1], ScrapeRunnerClosed) for r in errors)
        assert scheduler._consecutive_failures == {}             # non e' un fallimento di ricerca
        result.session.close.assert_called_once()

    def test_cerca_tells_the_user_the_service_is_unavailable_when_the_runner_is_closed(
            self, _fresh_scrape_runner):
        """Contratto AGGIORNATO: prima l'utente vedeva "Errore durante la ricerca: <messaggio interno>"."""
        _fresh_scrape_runner.shutdown()

        texts = asyncio.run(_cerca(lambda *a, **k: []))

        assert texts == ["Cerco voli senza scali, un momento...", UNAVAILABLE]
        assert texts[-1] != GENERIC_ERROR                         # distinto dall'errore dello scraper

    def test_cerca_shows_no_internal_details_when_the_runner_is_closed(self, _fresh_scrape_runner):
        _fresh_scrape_runner.shutdown()
        reply = AsyncMock()
        update = SimpleNamespace(message=SimpleNamespace(reply_text=reply))

        async def scenario():
            with patch.object(bot, "search_options", side_effect=lambda *a, **k: []):
                await bot.cerca(update, SimpleNamespace(args=["psa", "cag", "25-12-2026"]))

        asyncio.run(scenario())

        shown = " ".join(call.args[0] for call in reply.call_args_list).lower()
        for internal in ("scraperunnerclosed", "runner", "chiuso", "arresta", "eccezione",
                         "exception", "traceback", "errore durante la ricerca"):
            assert internal not in shown, internal
        assert all("reply_markup" not in call.kwargs for call in reply.call_args_list)  # nessun pulsante

    def test_a_closed_runner_is_logged_as_a_warning_without_a_traceback(self, _fresh_scrape_runner, caplog):
        _fresh_scrape_runner.shutdown()

        with caplog.at_level(logging.INFO, logger="flight_bot.bot"):
            asyncio.run(_cerca(lambda *a, **k: []))

        records = [r for r in caplog.records if r.name == "flight_bot.bot"]
        assert [r.levelno for r in records] == [logging.WARNING]  # niente ERROR: non e' un bug
        assert records[0].exc_info is None
        assert "PSA" in records[0].getMessage().upper()           # ma la ricerca e' identificabile

    @pytest.mark.parametrize("error", [
        ScraperError("richiesta fallita", timed_out=True),
        ScraperError("risposta HTTP", status_code=503),
        ScraperError("errore generico"),
    ], ids=["timeout", "http503", "generic"])
    def test_scraper_errors_still_get_the_scraper_message_when_the_runner_is_open(self, error):
        def search_options(*args, **kwargs):
            raise error

        texts = asyncio.run(_cerca(search_options))

        assert texts[-1] == GENERIC_ERROR
        assert texts[-1] != UNAVAILABLE

    @pytest.mark.parametrize("error,expected", [
        (ValueError("boom"), "Errore durante la ricerca: boom"),
        (RuntimeError("altro problema"), "Errore durante la ricerca: altro problema"),
        (KeyError("chiave"), "Errore durante la ricerca: 'chiave'"),
        (type("SimilARunnerError", (RuntimeError,), {})("quasi uguale"),
         "Errore durante la ricerca: quasi uguale"),
    ], ids=["ValueError", "RuntimeError", "KeyError", "RuntimeError_subclass"])
    def test_unexpected_errors_keep_their_existing_handling(self, error, expected):
        """Il ramo nuovo e' ESPLICITO: non deve catturare altri errori, nemmeno altri RuntimeError."""
        def search_options(*args, **kwargs):
            raise error

        texts = asyncio.run(_cerca(search_options))

        assert texts[-1] == expected


# --------------------------------------------- 8. ciclo di vita dell'applicazione (main) ----

def _run_main(order, failing_step=None):
    class StopHere:
        async def wait(self):
            raise asyncio.CancelledError()

    def step(name, fail=False):
        def action(*args, **kwargs):
            order.append(name)
            if fail:
                raise RuntimeError(f"{name} fallito")
        return action

    application = MagicMock()
    application.initialize = AsyncMock()
    application.start = AsyncMock()
    application.updater.start_polling = AsyncMock()
    application.updater.stop = AsyncMock(side_effect=step("updater.stop", failing_step == "updater.stop"))
    application.stop = AsyncMock(side_effect=step("application.stop", failing_step == "application.stop"))
    application.shutdown = AsyncMock(side_effect=step("application.shutdown"))
    sched = MagicMock()
    sched.shutdown.side_effect = step("scheduler.shutdown", failing_step == "scheduler.shutdown")

    with patch.object(main, "init_db"), patch.object(main, "discover_new_weekends"), \
         patch.object(main, "deactivate_expired_searches"), \
         patch.object(main, "start_scheduler", return_value=sched), \
         patch.object(main, "build_application", return_value=application), \
         patch.object(main, "shutdown_scrape_runner", side_effect=step("runner.shutdown")), \
         patch.object(main.asyncio, "Event", StopHere):
        return asyncio.run(main.main())


class TestApplicationLifecycle:

    def test_the_runner_is_closed_last_after_scheduler_and_telegram_are_stopped(self):
        order = []

        with pytest.raises(asyncio.CancelledError):
            _run_main(order)

        assert order == ["scheduler.shutdown", "updater.stop", "application.stop",
                         "application.shutdown", "runner.shutdown"]

    @pytest.mark.parametrize("failing_step", ["scheduler.shutdown", "updater.stop", "application.stop"])
    def test_the_runner_is_closed_even_if_a_previous_shutdown_step_fails(self, failing_step):
        order = []

        with pytest.raises(RuntimeError, match="fallito"):
            _run_main(order, failing_step=failing_step)

        assert order[-1] == "runner.shutdown"
        assert failing_step in order
