"""
Esecuzione dello scraping FUORI dall'event loop, una chiamata alla volta.

Lo scraper e' sincrono: ogni richiesta a Google e ogni lettura del corpo della risposta
bloccano il thread che le esegue. Chiamarle direttamente da una coroutine ferma l'intero
event loop (polling Telegram, altri job, alert) per tutta la durata della chiamata.

Qui le chiamate bloccanti passano per UN solo thread dedicato:
  - il loop resta libero mentre lo scraper lavora;
  - le chiamate sono SERIALIZZATE (un solo worker): ciclo di scraping e /cerca non fanno mai
    richieste contemporanee a Google ne' usano insieme il client `primp` condiviso;
  - nessun retry, nessuna coda aggiuntiva: la policy di retry resta nello scheduler.

Cosa puo' attraversare il thread: SOLO la funzione da eseguire e valori semplici (stringhe,
date, numeri). Mai sessioni SQLAlchemy, oggetti ORM o connessioni DB: la sessione non e'
thread-safe e la lettura degli attributi ORM va fatta PRIMA, sul thread dell'event loop.

Annullamento e arresto (comportamento verificato dai test):
  - se la coroutine che attende viene annullata e la chiamata NON e' ancora partita, non
    partira' mai; se e' gia' in corso, termina da sola nel thread e il risultato viene
    scartato. In entrambi i casi il runner resta utilizzabile;
  - shutdown(): le chiamate ancora in coda vengono annullate, quella in corso NON si puo'
    interrompere e termina (al massimo il timeout HTTP). Con wait=False (default) il
    controllo torna subito, ma l'interprete attende comunque quel thread all'uscita;
  - dopo lo shutdown una nuova richiesta solleva ScrapeRunnerClosed, senza eseguire nulla.
"""
import asyncio
import functools
import threading
from concurrent.futures import ThreadPoolExecutor


class ScrapeRunnerClosed(RuntimeError):
    """Richiesto uno scraping dopo lo shutdown del runner (l'applicazione si sta chiudendo).

    Non e' un ScraperError: non e' un errore dello scraper e non deve contare come
    fallimento di una ricerca.
    """


class ScrapeRunner:
    """Un thread, una chiamata alla volta."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scraper")
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def run(self, func, *args, **kwargs):
        """Esegue `func(*args, **kwargs)` nel thread dello scraper e ne attende l'esito.

        Le eccezioni sollevate da `func` (con tipo, attributi e `__cause__`) si propagano
        invariate al chiamante.
        """
        loop = asyncio.get_running_loop()
        call = functools.partial(func, *args, **kwargs)
        # Controllo e inserimento in coda sono atomici rispetto a shutdown(): non esiste una
        # finestra in cui il flag e' falso ma l'executor e' gia' chiuso.
        with self._lock:
            if self._closed:
                raise ScrapeRunnerClosed(
                    "Il runner dello scraping e' stato chiuso: l'applicazione si sta arrestando")
            future = loop.run_in_executor(self._executor, call)
        return await future

    def shutdown(self, wait: bool = False) -> None:
        """Chiude il runner. Idempotente: puo' essere richiamato piu' volte senza errori."""
        with self._lock:
            self._closed = True
        # ThreadPoolExecutor.shutdown e' a sua volta idempotente.
        self._executor.shutdown(wait=wait, cancel_futures=True)


# Istanza usata dall'applicazione. Le funzioni sotto la risolvono a ogni chiamata, cosi' i
# test possono sostituirla con un runner nuovo senza toccare scheduler e bot.
_default = ScrapeRunner()


async def run_scrape(func, *args, **kwargs):
    """Esegue una chiamata bloccante dello scraper fuori dall'event loop (una alla volta)."""
    return await _default.run(func, *args, **kwargs)


def shutdown_scrape_runner(wait: bool = False) -> None:
    """Chiude il runner dell'applicazione (idempotente)."""
    _default.shutdown(wait=wait)
