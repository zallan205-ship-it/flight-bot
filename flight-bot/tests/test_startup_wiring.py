"""
Test del WIRING di avvio: ci sono perche' una funzione di scheduler.py (discover_new_weekends)
e' sparita per errore mentre main.py e start_scheduler() continuavano a usarla, e nessun
test se n'e' accorto (la suite non importava main.py ne' costruiva lo scheduler).

Cosa si verifica:
  - ogni nome importato da un modulo INTERNO esiste davvero (guardia generale);
  - main.py si importa e ha cio' che gli serve;
  - start_scheduler() registra tutti i job previsti;
  - la sequenza di avvio popola i weekend prima di far partire il polling;
  - discover_new_weekends / _next_weekend_dates fanno quello che il progetto promette
    (generazione automatica dei weekend PSA-CAG / CAG-PSA, senza duplicati).

Tutto offline: nessun polling Telegram, nessuna richiesta di rete.
"""
import ast
import asyncio
import importlib
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
from models import Base, MonitoredSearch, MonitorType, build_flight_key  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parents[1]


# ------------------------------------------------ 1. gli import interni risolvono ----

def _local_module_names():
    names = {p.stem for p in PROJECT_DIR.glob("*.py")}
    names |= {p.name for p in PROJECT_DIR.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    names.discard("tests")
    return names


def _production_files():
    files = [p for p in PROJECT_DIR.glob("*.py") if not p.name.startswith("test_")]
    files += list((PROJECT_DIR / "scrapers").glob("*.py"))
    return sorted(files)


def _internal_imports(path):
    """(file, modulo, nome) per ogni `from <modulo interno> import <nome>`."""
    local = _local_module_names()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] in local:
                for alias in node.names:
                    yield path.name, node.module, alias.name


class TestInternalImportsResolve:

    def test_the_guard_actually_finds_imports(self):
        """Sanity: la guardia vede gli import di main.py, altrimenti non proverebbe nulla."""
        found = {(f, m, n) for p in _production_files() for f, m, n in _internal_imports(p)}
        assert ("main.py", "scheduler", "start_scheduler") in found
        assert ("main.py", "scheduler", "deactivate_expired_searches") in found

    @pytest.mark.parametrize("path", _production_files(), ids=lambda p: p.name)
    def test_every_name_imported_from_an_internal_module_exists(self, path):
        for filename, module, name in _internal_imports(path):
            if name == "*":
                continue
            imported = importlib.import_module(module)
            assert hasattr(imported, name), (
                f"{filename} importa '{name}' da '{module}', ma '{module}' non lo definisce")


# ------------------------------------------------------------- 2. main.py si importa ----

class TestMainModule:

    def test_main_imports_and_exposes_what_it_uses(self):
        import main

        for name in ("start_scheduler", "discover_new_weekends", "deactivate_expired_searches",
                     "init_db", "build_application"):
            assert callable(getattr(main, name)), name


# ------------------------------------------------ 3. start_scheduler registra i job ----

def _registered_jobs():
    """Costruisce lo scheduler reale (in un loop, senza lasciarlo girare) e ne elenca i job."""
    async def scenario():
        sched = scheduler.start_scheduler()
        try:
            return {job.func.__name__: type(job.trigger).__name__ for job in sched.get_jobs()}
        finally:
            sched.shutdown(wait=False)

    return asyncio.run(scenario())


class TestStartScheduler:

    def test_registers_every_expected_job(self):
        jobs = _registered_jobs()

        assert set(jobs) == {"discover_new_weekends", "deactivate_expired_searches",
                             "run_scraping_cycle", "weekly_recalibration"}

    def test_scraping_runs_on_an_interval_and_the_maintenance_jobs_are_cron(self):
        jobs = _registered_jobs()

        assert jobs["run_scraping_cycle"] == "IntervalTrigger"
        assert jobs["discover_new_weekends"] == "CronTrigger"
        assert jobs["deactivate_expired_searches"] == "CronTrigger"
        assert jobs["weekly_recalibration"] == "CronTrigger"

    def test_every_registered_job_is_defined_in_the_scheduler_module(self):
        for name in _registered_jobs():
            assert callable(getattr(scheduler, name)), name


# ---------------------------------------------------------- 4. sequenza di avvio ----

class TestStartupSequence:

    def test_weekends_are_generated_before_polling_starts(self):
        import main

        order = []
        application = MagicMock()
        for method in ("initialize", "start", "stop", "shutdown"):
            setattr(application, method, AsyncMock())
        application.updater.start_polling = AsyncMock(side_effect=lambda: order.append("polling"))
        application.updater.stop = AsyncMock()
        sched = MagicMock()

        class StopHere:                                   # sostituisce l'attesa infinita
            async def wait(self):
                raise asyncio.CancelledError()

        with patch.object(main, "init_db", side_effect=lambda: order.append("init_db")), \
             patch.object(main, "discover_new_weekends",
                          side_effect=lambda: order.append("discover_new_weekends")), \
             patch.object(main, "deactivate_expired_searches",
                          side_effect=lambda: order.append("deactivate_expired_searches")), \
             patch.object(main, "start_scheduler",
                          side_effect=lambda: order.append("start_scheduler") or sched), \
             patch.object(main, "build_application", return_value=application), \
             patch.object(main.asyncio, "Event", StopHere):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(main.main())

        assert order == ["init_db", "discover_new_weekends", "deactivate_expired_searches",
                         "start_scheduler", "polling"]
        sched.shutdown.assert_called_once()               # e si chiude in modo ordinato


# ------------------------------------------ 5. discover_new_weekends: comportamento ----

class TestNextWeekendDates:

    def test_returns_consecutive_saturday_sunday_pairs(self):
        pairs = scheduler._next_weekend_dates(6)

        assert len(pairs) == 6
        for departure, ret in pairs:
            assert departure.weekday() == scheduler.WEEKEND_DEPARTURE_WEEKDAY
            assert (departure.hour, departure.minute) == (scheduler.WEEKEND_DEPARTURE_MIN_HOUR, 0)
            assert ret.weekday() == scheduler.WEEKEND_RETURN_WEEKDAY
            assert (ret.hour, ret.minute) == (scheduler.WEEKEND_RETURN_MIN_HOUR, 0)
            assert (ret.date() - departure.date()) == timedelta(days=1)
        departures = [d for d, _ in pairs]
        assert all(b - a == timedelta(weeks=1) for a, b in zip(departures, departures[1:]))

    def test_starts_from_the_current_week_not_from_the_past(self):
        first_departure = scheduler._next_weekend_dates(1)[0][0]

        assert first_departure.date() >= datetime.now().date()
        assert first_departure.date() - datetime.now().date() < timedelta(days=7)

    def test_zero_weekends_gives_an_empty_list(self):
        assert scheduler._next_weekend_dates(0) == []


@pytest.fixture
def weekend_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'weekends.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with patch.object(scheduler, "SessionLocal", Session):
        yield Session
    engine.dispose()


def _auto_weekends(Session):
    with Session() as s:
        return s.query(MonitoredSearch).filter_by(monitor_type=MonitorType.AUTO_WEEKEND).all()


class TestDiscoverNewWeekends:

    def expected_count(self):
        return scheduler.WEEKENDS_HORIZON_TO_TRACK * len(scheduler.AUTO_ROUTES)

    def test_creates_every_weekend_for_every_automatic_route(self, weekend_db):
        scheduler.discover_new_weekends()

        searches = _auto_weekends(weekend_db)
        assert len(searches) == self.expected_count()
        routes = {(r["origin"], r["destination"]): r["topic_id"] for r in scheduler.AUTO_ROUTES}
        for s in searches:
            assert routes[(s.origin, s.destination)] == s.telegram_topic_id
            assert s.return_date.date() - s.departure_date.date() == timedelta(days=1)
            assert s.active is True
            assert s.flight_key == build_flight_key(s)           # chiave leggibile assegnata
        assert len({s.flight_key for s in searches}) == len(searches)   # tutte distinte

    def test_a_second_run_creates_no_duplicates(self, weekend_db):
        scheduler.discover_new_weekends()
        scheduler.discover_new_weekends()

        assert len(_auto_weekends(weekend_db)) == self.expected_count()

    def test_it_only_recreates_the_missing_weekends(self, weekend_db):
        scheduler.discover_new_weekends()
        with weekend_db() as s:
            for victim in s.query(MonitoredSearch).order_by(MonitoredSearch.id).limit(3).all():
                s.delete(victim)
            s.commit()
        assert len(_auto_weekends(weekend_db)) == self.expected_count() - 3

        scheduler.discover_new_weekends()

        assert len(_auto_weekends(weekend_db)) == self.expected_count()

    def test_manual_searches_are_left_alone(self, weekend_db):
        with weekend_db() as s:
            s.add(MonitoredSearch(
                origin="PSA", destination="CAG", departure_date=datetime.now() + timedelta(days=40),
                return_date=None, monitor_type=MonitorType.MANUAL, telegram_topic_id=13))
            s.commit()

        scheduler.discover_new_weekends()

        with weekend_db() as s:
            manual = s.query(MonitoredSearch).filter_by(monitor_type=MonitorType.MANUAL).all()
        assert len(manual) == 1
        assert len(_auto_weekends(weekend_db)) == self.expected_count()

    def test_red_period_is_recorded_when_a_weekend_falls_inside_one(self, weekend_db):
        scheduler.discover_new_weekends()

        for s in _auto_weekends(weekend_db):
            period = scheduler._find_red_period(s.departure_date)
            assert s.red_period_name == (period.name if period else None)

    def test_session_is_closed_even_if_something_fails(self):
        session = MagicMock()
        session.query.side_effect = RuntimeError("database giu'")

        with patch.object(scheduler, "SessionLocal", return_value=session):
            with pytest.raises(RuntimeError):
                scheduler.discover_new_weekends()

        session.close.assert_called_once()
