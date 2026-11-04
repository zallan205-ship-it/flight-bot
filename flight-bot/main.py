import asyncio
import logging

from db import init_db
from scheduler import start_scheduler, discover_new_weekends, deactivate_expired_searches
from bot import build_application
from scrape_runner import shutdown_scrape_runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


async def main():
    init_db()
    discover_new_weekends()  # popola subito i weekend, non aspettare il cron delle 03:00
    deactivate_expired_searches()

    scheduler = start_scheduler()

    application = build_application()
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    try:
        await asyncio.Event().wait()  # tiene vivo il processo
    finally:
        try:
            scheduler.shutdown()
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
        finally:
            # Per ultimo, e anche se uno degli arresti sopra solleva: ormai non partono piu'
            # ricerche. Le chiamate ancora in coda vengono annullate; quella in corso termina
            # da sola (al massimo il timeout HTTP) e l'interprete la attende all'uscita.
            shutdown_scrape_runner()


if __name__ == "__main__":
    asyncio.run(main())
