"""
Test di scrape_runner: l'executor dedicato (UN thread) che esegue le chiamate bloccanti dello
scraper fuori dall'event loop.

Nessun time.sleep e nessun ritardo arbitrario: il coordinamento tra il thread dello scraper e
l'event loop usa handshake con threading.Event; i timeout (5 s) servono solo a non bloccare la
suite se un test fallisce, il percorso corretto non li attende mai.
"""
import asyncio
import threading

import pytest

import scrape_runner
from scrape_runner import ScrapeRunner, ScrapeRunnerClosed
from scrapers.google_flights import ScraperError


@pytest.fixture
def runner():
    instance = ScrapeRunner()
    yield instance
    instance.shutdown(wait=False)


async def _wait(event, timeout=5):
    """Attende un threading.Event SENZA bloccare il loop (e senza polling a tempo)."""
    assert await asyncio.to_thread(event.wait, timeout), "evento non ricevuto"


def _count_submissions(runner, monkeypatch):
    """Conta le chiamate messe in coda sull'executor: sa ESATTAMENTE quando N richieste sono pronte."""
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
        await asyncio.sleep(0)               # cessione del controllo, senza attendere
    raise AssertionError(f"solo {len(submitted)} richieste sottomesse su {count}")


# ------------------------------------------------------------------ esecuzione ----

class TestExecution:

    def test_runs_in_a_dedicated_thread_not_in_the_event_loop_thread(self, runner):
        async def scenario():
            loop_thread = threading.current_thread().name
            first = await runner.run(lambda: threading.current_thread().name)
            second = await runner.run(lambda: threading.current_thread().name)
            return loop_thread, first, second

        loop_thread, first, second = asyncio.run(scenario())

        assert first == second                       # sempre lo stesso, unico thread
        assert first.startswith("scraper")
        assert first != loop_thread

    def test_arguments_and_result_pass_through(self, runner):
        def work(a, b, *, factor):
            return (a + b) * factor

        assert asyncio.run(runner.run(work, 2, 3, factor=10)) == 50

    def test_calls_are_executed_one_at_a_time_in_order(self, runner, monkeypatch):
        submitted = _count_submissions(runner, monkeypatch)
        lock, gate = threading.Lock(), threading.Event()
        activity = {"now": 0, "max": 0, "order": []}

        def work(n):
            with lock:
                activity["now"] += 1
                activity["max"] = max(activity["max"], activity["now"])
                activity["order"].append(n)
            gate.wait(5)                             # tiene occupato il worker finche' tutti sono in coda
            with lock:
                activity["now"] -= 1
            return n

        async def scenario():
            tasks = [asyncio.create_task(runner.run(work, n)) for n in range(4)]
            await _until_submitted(submitted, 4)     # le 4 richieste sono TUTTE in coda
            gate.set()
            return await asyncio.gather(*tasks)

        results = asyncio.run(scenario())

        assert results == [0, 1, 2, 3]
        assert activity["order"] == [0, 1, 2, 3]
        assert activity["max"] == 1                  # mai due chiamate attive insieme

    def test_the_loop_runs_other_coroutines_while_a_call_is_in_progress(self, runner):
        served, observed = threading.Event(), {}

        def blocking():
            observed["served"] = served.wait(2)      # solo se il loop viene servito mentre si attende
            return "fatto"

        async def scenario():
            async def other():
                await asyncio.sleep(0)
                served.set()
            task = asyncio.create_task(other())
            result = await runner.run(blocking)
            await task
            return result

        assert asyncio.run(scenario()) == "fatto"
        assert observed["served"] is True


# ------------------------------------------------------------------- eccezioni ----

class TestExceptionsCrossTheThread:

    def test_scraper_error_keeps_type_attributes_and_cause(self, runner):
        cause = TimeoutError("operation timed out")
        error = ScraperError("richiesta fallita", status_code=503, timed_out=True)
        error.__cause__ = cause

        def failing():
            raise error

        with pytest.raises(ScraperError) as excinfo:
            asyncio.run(runner.run(failing))

        assert excinfo.value is error                # lo stesso oggetto, non una copia
        assert excinfo.value.timed_out is True
        assert excinfo.value.status_code == 503
        assert excinfo.value.__cause__ is cause

    def test_defaults_of_scraper_error_survive_too(self, runner):
        def failing():
            raise ScraperError("errore generico")

        with pytest.raises(ScraperError) as excinfo:
            asyncio.run(runner.run(failing))

        assert excinfo.value.timed_out is False and excinfo.value.status_code is None

    def test_other_exceptions_are_not_converted(self, runner):
        def failing():
            raise ValueError("bug")

        with pytest.raises(ValueError, match="bug"):
            asyncio.run(runner.run(failing))

    def test_an_exception_does_not_poison_the_runner(self, runner):
        def failing():
            raise ValueError("bug")

        async def scenario():
            with pytest.raises(ValueError):
                await runner.run(failing)
            return await runner.run(lambda: "ancora vivo")

        assert asyncio.run(scenario()) == "ancora vivo"


# ----------------------------------------------------------------- annullamento ----

class TestCancellation:

    def test_cancelling_a_running_call_lets_it_finish_and_keeps_the_runner_usable(self, runner):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def slow():
            started.set()
            release.wait(5)
            finished.set()
            return "scartato"

        async def scenario():
            task = asyncio.create_task(runner.run(slow))
            await _wait(started)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not finished.is_set()             # la chiamata e' ancora in corso nel thread
            release.set()
            await _wait(finished)                    # ...e termina da sola
            return await runner.run(lambda: "dopo")

        assert asyncio.run(scenario()) == "dopo"

    def test_cancelling_a_queued_call_means_it_never_runs(self, runner, monkeypatch):
        submitted = _count_submissions(runner, monkeypatch)
        started, release, ran = threading.Event(), threading.Event(), []

        def blocker():
            started.set()
            release.wait(5)

        async def scenario():
            first = asyncio.create_task(runner.run(blocker))
            await _wait(started)
            queued = asyncio.create_task(runner.run(lambda: ran.append("eseguita")))
            await _until_submitted(submitted, 2)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            release.set()
            await first
            return await runner.run(lambda: "dopo")

        assert asyncio.run(scenario()) == "dopo"
        assert ran == []                             # la chiamata annullata in coda non e' mai partita

    def test_a_cancelled_caller_does_not_block_the_next_call(self, runner):
        """Il ciclo di vita resta coerente: annullare piu' volte di seguito non incastra il thread."""
        async def scenario():
            for _ in range(3):
                started, release = threading.Event(), threading.Event()

                def work(started=started, release=release):
                    started.set()
                    release.wait(5)

                task = asyncio.create_task(runner.run(work))
                await _wait(started)
                task.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            return await runner.run(lambda: "ok")

        assert asyncio.run(scenario()) == "ok"


# --------------------------------------------------------------------- shutdown ----

class TestShutdown:

    def test_shutdown_is_idempotent(self, runner):
        runner.shutdown()
        runner.shutdown()
        runner.shutdown(wait=True)                   # nessuna eccezione, nessun blocco

        assert runner.closed is True

    def test_shutdown_before_any_use_is_harmless(self):
        fresh = ScrapeRunner()

        fresh.shutdown()

        assert fresh.closed is True

    def test_the_call_in_progress_completes_and_queued_calls_are_cancelled(self, runner, monkeypatch):
        """Comportamento documentato: la chiamata in corso NON si interrompe, quelle in coda si annullano."""
        submitted = _count_submissions(runner, monkeypatch)
        started, release, finished, ran = (threading.Event(), threading.Event(),
                                           threading.Event(), [])

        def in_progress():
            started.set()
            release.wait(5)
            finished.set()
            return "completata"

        async def scenario():
            current = asyncio.create_task(runner.run(in_progress))
            await _wait(started)
            queued = asyncio.create_task(runner.run(lambda: ran.append("eseguita")))
            await _until_submitted(submitted, 2)

            runner.shutdown()                        # torna subito (wait=False): non attende il thread
            returned_while_running = not finished.is_set()
            release.set()

            result = await current
            with pytest.raises(asyncio.CancelledError):
                await queued
            return result, returned_while_running

        result, returned_while_running = asyncio.run(scenario())

        assert result == "completata"
        assert returned_while_running is True        # shutdown() non ha atteso la chiamata in corso
        assert ran == []

    def test_a_new_call_after_shutdown_fails_explicitly_and_runs_nothing(self, runner):
        ran = []
        runner.shutdown()

        async def scenario():
            await runner.run(lambda: ran.append("eseguita"))

        with pytest.raises(ScrapeRunnerClosed) as excinfo:
            asyncio.run(scenario())

        assert ran == []
        assert "chiuso" in str(excinfo.value)

    def test_the_closed_error_is_not_a_scraper_error(self):
        """Non deve contare come fallimento di una ricerca ne' essere classificato come errore dello scraper."""
        assert issubclass(ScrapeRunnerClosed, RuntimeError)
        assert not issubclass(ScrapeRunnerClosed, ScraperError)

    def test_shutdown_after_use_still_refuses_new_calls(self, runner):
        asyncio.run(runner.run(lambda: None))
        runner.shutdown()

        with pytest.raises(ScrapeRunnerClosed):
            asyncio.run(runner.run(lambda: None))


# --------------------------------------------------------- funzioni del modulo ----

class TestModuleLevelFunctions:

    def test_run_scrape_uses_the_application_runner(self, _fresh_scrape_runner):
        name = asyncio.run(scrape_runner.run_scrape(lambda: threading.current_thread().name))

        assert name.startswith("scraper")

    def test_shutdown_scrape_runner_closes_the_application_runner_idempotently(self, _fresh_scrape_runner):
        scrape_runner.shutdown_scrape_runner()
        scrape_runner.shutdown_scrape_runner()

        assert _fresh_scrape_runner.closed is True
        with pytest.raises(ScrapeRunnerClosed):
            asyncio.run(scrape_runner.run_scrape(lambda: None))

    def test_each_test_gets_a_fresh_runner(self, _fresh_scrape_runner):
        """Garanzia di isolamento (conftest): un test che chiude il runner non inquina gli altri."""
        assert _fresh_scrape_runner.closed is False
        assert scrape_runner._default is _fresh_scrape_runner
