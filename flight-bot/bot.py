"""
Comandi Telegram per la gestione dei monitoraggi.

  /monitora ORIGINE DESTINAZIONE GG-MM-AAAA [GG-MM-AAAA]
      Crea un nuovo monitoraggio manuale. Va sempre nel topic manuale, e ha
      sempre il monitoraggio "completo" attivo (ribassi + rincari) fin da subito.

  /monitora CHIAVE
      (un solo argomento, che corrisponde a una chiave tipo "PSACAG0491"
      già vista in un alert) "Promuove" un volo monitorato in automatico a
      monitoraggio completo: da questo momento segnala anche i rincari, e i
      suoi alert si spostano nel topic dei monitoraggi manuali.

  /elenco
      Elenca i monitoraggi manuali attivi, con la loro chiave.
"""
import re
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from config import TELEGRAM_BOT_TOKEN, TOPIC_MANUAL, SEARCH_MAX_OPTIONS, SEARCH_TIME_WINDOW_HOURS
from db import SessionLocal
from models import MonitoredSearch, MonitorType, build_flight_key
from calibration import _find_red_period
from scrapers.google_flights import search_options

FLIGHT_KEY_PATTERN = re.compile(r"^[A-Z]{6}\d{4}$")  # es. PSACAG0491


async def monitora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    if len(args) == 1 and FLIGHT_KEY_PATTERN.match(args[0].upper()):
        await _promuovi_volo(update, args[0].upper())
        return

    if len(args) < 3:
        await update.message.reply_text(
            "Uso:\n"
            "/monitora ORIGINE DESTINAZIONE GG-MM-AAAA [GG-MM-AAAA ritorno]\n"
            "oppure /monitora CHIAVE (per promuovere un volo già monitorato, es. /monitora PSACAG0491)"
        )
        return

    origin, destination = args[0].upper(), args[1].upper()
    try:
        departure = datetime.strptime(args[2], "%d-%m-%Y")
    except ValueError:
        await update.message.reply_text("Data di partenza non valida, formato GG-MM-AAAA.")
        return

    return_date = None
    if len(args) >= 4:
        try:
            return_date = datetime.strptime(args[3], "%d-%m-%Y")
        except ValueError:
            await update.message.reply_text("Data di ritorno non valida, formato GG-MM-AAAA.")
            return

    session = SessionLocal()
    try:
        red_period = _find_red_period(departure)
        existing = session.query(MonitoredSearch).filter_by(
            origin=origin, destination=destination,
            departure_date=departure, return_date=return_date,
            monitor_type=MonitorType.MANUAL,
        ).first()
        if existing:
            await update.message.reply_text(
                f"Questo volo è già sotto monitoraggio manuale (chiave {existing.flight_key})."
            )
            return

        new_search = MonitoredSearch(
            origin=origin, destination=destination,
            departure_date=departure, return_date=return_date,
            monitor_type=MonitorType.MANUAL,
            telegram_topic_id=TOPIC_MANUAL,
            red_period_name=red_period.name if red_period else None,
            full_monitoring=True,  # i monitoraggi manuali segnalano sempre anche i rincari
        )
        session.add(new_search)
        session.flush()
        new_search.flight_key = build_flight_key(new_search)
        session.commit()
        flight_key = new_search.flight_key
    finally:
        session.close()

    msg = f"Monitoraggio attivato ({flight_key}): {origin} → {destination}, andata {departure:%d/%m/%Y}"
    if return_date:
        msg += f", ritorno {return_date:%d/%m/%Y}"
    msg += "\nMonitora tutti gli orari disponibili in quelle date (nessun filtro orario, a differenza dei weekend automatici)."
    if red_period:
        msg += f"\nRientra nel periodo rosso: {red_period.name}."
    await update.message.reply_text(msg)


async def _promuovi_volo(update: Update, flight_key: str):
    session = SessionLocal()
    try:
        search = session.query(MonitoredSearch).filter_by(flight_key=flight_key).first()
        if search is None:
            await update.message.reply_text(f"Nessun volo trovato con chiave {flight_key}.")
            return

        if search.full_monitoring:
            await update.message.reply_text(
                f"{flight_key} è già in monitoraggio completo (ribassi + rincari, topic monitoraggi manuali)."
            )
            return

        search.full_monitoring = True
        search.telegram_topic_id = TOPIC_MANUAL
        session.commit()
    finally:
        session.close()

    await update.message.reply_text(
        f"{flight_key} promosso a monitoraggio completo: da ora segnala anche i rincari, "
        f"e i suoi alert usciranno in questo topic invece che in quello di rotta."
    )


async def elenco(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = SessionLocal()
    try:
        searches = session.query(MonitoredSearch).filter_by(
            monitor_type=MonitorType.MANUAL, active=True
        ).all()
        promoted = session.query(MonitoredSearch).filter_by(
            monitor_type=MonitorType.AUTO_WEEKEND, full_monitoring=True, active=True
        ).all()
    finally:
        session.close()

    all_searches = searches + promoted
    if not all_searches:
        await update.message.reply_text("Nessun monitoraggio completo attivo al momento.")
        return

    lines = ["Monitoraggi completi attivi (chiave · rotta · date):"]
    for s in all_searches:
        line = f"- {s.flight_key} · {s.origin} → {s.destination}, andata {s.departure_date:%d/%m/%Y}"
        if s.return_date:
            line += f", ritorno {s.return_date:%d/%m/%Y}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines))


async def cerca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Uso: /cerca ORIGINE DESTINAZIONE GG-MM-AAAA [GG-MM-AAAA ritorno]\n"
            "Mostra le opzioni di volo senza scali con il prezzo attuale, da scegliere con un bottone."
        )
        return

    origin, destination = args[0].upper(), args[1].upper()
    try:
        departure = datetime.strptime(args[2], "%d-%m-%Y")
    except ValueError:
        await update.message.reply_text("Data di partenza non valida, formato GG-MM-AAAA.")
        return

    return_date = None
    if len(args) >= 4:
        try:
            return_date = datetime.strptime(args[3], "%d-%m-%Y")
        except ValueError:
            await update.message.reply_text("Data di ritorno non valida, formato GG-MM-AAAA.")
            return

    await update.message.reply_text("Cerco voli senza scali, un momento...")

    try:
        opzioni = search_options(origin, destination, departure, return_date,
                                  max_results=SEARCH_MAX_OPTIONS)
    except Exception as e:
        await update.message.reply_text(f"Errore durante la ricerca: {e}")
        return

    if not opzioni:
        await update.message.reply_text(
            "Nessun volo senza scali trovato per questa data (o il volo non è ancora "
            "disponibile). Puoi comunque usare /monitora per tenerla d'occhio senza "
            "vedere subito i prezzi."
        )
        return

    bottoni = []
    for o in opzioni:
        dep_key = departure.strftime("%Y%m%d")
        if o.return_time:
            etichetta = f"{o.departure_time} → {o.return_time} · {o.price_eur:.0f}€"
            ret_key = return_date.strftime("%Y%m%d")
            callback = f"sel|{origin}|{destination}|{dep_key}|{o.departure_time}|{ret_key}|{o.return_time}"
        else:
            etichetta = f"{o.departure_time} · {o.price_eur:.0f}€"
            callback = f"sel|{origin}|{destination}|{dep_key}|{o.departure_time}|-|-"
        bottoni.append([InlineKeyboardButton(etichetta, callback_data=callback)])

    tratta = f"{origin} → {destination}"
    data_testo = f"{departure:%d/%m/%Y}"
    if return_date:
        data_testo += f" - {return_date:%d/%m/%Y}"

    await update.message.reply_text(
        f"Voli senza scali {tratta}, {data_testo} — scegline uno da monitorare:",
        reply_markup=InlineKeyboardMarkup(bottoni),
    )


async def gestisci_selezione(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # toglie la "clessidra" di caricamento sul bottone

    try:
        _, origin, destination, dep_str, dep_time, ret_str, ret_time = query.data.split("|")
    except ValueError:
        await query.edit_message_text("Errore nel leggere la selezione, riprova con /cerca.")
        return

    departure = datetime.strptime(dep_str, "%Y%m%d")
    return_date = datetime.strptime(ret_str, "%Y%m%d") if ret_str != "-" else None

    dep_hour = int(dep_time.split(":")[0])
    earliest_departure_hour = max(0, dep_hour - SEARCH_TIME_WINDOW_HOURS)
    latest_departure_hour = min(23, dep_hour + SEARCH_TIME_WINDOW_HOURS)

    earliest_return_hour = None
    latest_return_hour = None
    if return_date and ret_time != "-":
        ret_hour = int(ret_time.split(":")[0])
        earliest_return_hour = max(0, ret_hour - SEARCH_TIME_WINDOW_HOURS)
        latest_return_hour = min(23, ret_hour + SEARCH_TIME_WINDOW_HOURS)

    session = SessionLocal()
    try:
        red_period = _find_red_period(departure)
        existing = session.query(MonitoredSearch).filter_by(
            origin=origin, destination=destination,
            departure_date=departure, return_date=return_date,
            monitor_type=MonitorType.MANUAL,
        ).first()
        if existing:
            await query.edit_message_text(
                f"Questo volo era già monitorato (chiave {existing.flight_key})."
            )
            return

        new_search = MonitoredSearch(
            origin=origin, destination=destination,
            departure_date=departure, return_date=return_date,
            monitor_type=MonitorType.MANUAL,
            telegram_topic_id=TOPIC_MANUAL,
            red_period_name=red_period.name if red_period else None,
            full_monitoring=True,
            earliest_departure_hour=earliest_departure_hour,
            latest_departure_hour=latest_departure_hour,
            earliest_return_hour=earliest_return_hour,
            latest_return_hour=latest_return_hour,
        )
        session.add(new_search)
        session.flush()
        new_search.flight_key = build_flight_key(new_search)
        session.commit()
        flight_key = new_search.flight_key
    finally:
        session.close()

    msg = (
        f"Monitoraggio attivato ({flight_key}): {origin} → {destination}, "
        f"andata {departure:%d/%m/%Y} circa {dep_time}"
    )
    if return_date:
        msg += f", ritorno {return_date:%d/%m/%Y} circa {ret_time}"
    msg += (
        f"\nTraccerà il prezzo più basso nella fascia "
        f"{earliest_departure_hour:02d}:00-{latest_departure_hour:02d}:00"
    )
    if earliest_return_hour is not None:
        msg += f" (andata) e {earliest_return_hour:02d}:00-{latest_return_hour:02d}:00 (ritorno)"
    msg += "."

    await query.edit_message_text(msg)


def build_application() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("monitora", monitora))
    application.add_handler(CommandHandler("elenco", elenco))
    application.add_handler(CommandHandler("cerca", cerca))
    application.add_handler(CallbackQueryHandler(gestisci_selezione, pattern=r"^sel\|"))

    # Registra i comandi per il menu "/" di Telegram (autocomplete con
    # descrizione). post_init viene chiamato una volta all'avvio, prima che
    # il bot inizi a ricevere aggiornamenti.
    async def _register_commands(app: Application):
        await app.bot.set_my_commands([
            ("monitora", "Nuovo monitoraggio manuale, o /monitora CHIAVE per promuoverne uno"),
            ("cerca", "Cerca voli senza scali con prezzo attuale e scegline uno da monitorare"),
            ("elenco", "Elenca i monitoraggi manuali/promossi attivi"),
        ])

    application.post_init = _register_commands
    return application
