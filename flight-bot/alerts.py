from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import Bot

from config import PRICE_DROP_THRESHOLD_PCT, PRICE_RISE_THRESHOLD_PCT, TELEGRAM_CHAT_ID
from models import MonitoredSearch, PriceSnapshot, AlertSent, MonitorType
from calibration import get_optimal_window
from baseline import get_baseline, PriceTier


async def maybe_send_alert(session: Session, bot: Bot, search: MonitoredSearch,
                            new_snapshot: PriceSnapshot) -> None:
    """
    Da chiamare subito dopo aver salvato un nuovo PriceSnapshot.

    Ribassi: sempre segnalati (su qualunque monitoraggio), se il calo rispetto
    all'ultimo prezzo noto per QUESTO volo specifico è almeno PRICE_DROP_THRESHOLD_PCT.

    Rincari: segnalati SOLO se il monitoraggio è manuale, oppure se un volo
    automatico è stato "promosso" con /monitora <chiave> (search.full_monitoring).
    Sui weekend automatici non promossi, i rincari non generano mai un messaggio:
    con ~100 weekend monitorati in parallelo, le normali fluttuazioni di prezzo
    settimana su settimana diventerebbero rumore, non segnale utile.
    """
    previous_snapshot = session.execute(
        select(PriceSnapshot)
        .where(
            PriceSnapshot.search_id == search.id,
            PriceSnapshot.id != new_snapshot.id,
        )
        .order_by(PriceSnapshot.scraped_at.desc())
    ).scalars().first()

    if previous_snapshot is None:
        return  # primo prezzo mai visto per questa ricerca: nessun confronto possibile

    previous_price = previous_snapshot.price_eur
    if previous_price <= 0:
        return

    change_pct = (new_snapshot.price_eur - previous_price) / previous_price * 100

    is_drop = change_pct <= -PRICE_DROP_THRESHOLD_PCT
    is_rise = change_pct >= PRICE_RISE_THRESHOLD_PCT

    full_monitoring = search.monitor_type == MonitorType.MANUAL or search.full_monitoring

    if is_drop:
        await _send_alert(session, bot, search, new_snapshot, previous_price, change_pct, is_drop=True)
    elif is_rise and full_monitoring:
        await _send_alert(session, bot, search, new_snapshot, previous_price, change_pct, is_drop=False)


async def _send_alert(session: Session, bot: Bot, search: MonitoredSearch,
                       snapshot: PriceSnapshot, previous_price: float,
                       change_pct: float, is_drop: bool) -> None:
    message = _build_alert_message(session, search, snapshot, previous_price, change_pct, is_drop)

    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        message_thread_id=search.telegram_topic_id,
        text=message,
        parse_mode="HTML",
    )

    session.add(AlertSent(search_id=search.id, price_snapshot_id=snapshot.id))
    session.commit()


def _build_alert_message(session: Session, search: MonitoredSearch, snapshot: PriceSnapshot,
                          previous_price: float, change_pct: float, is_drop: bool) -> str:
    dep = search.departure_date.strftime("%d/%m/%Y")
    dep_time = f" {snapshot.departure_time}" if snapshot.departure_time else ""
    ret = search.return_date.strftime("%d/%m/%Y") if search.return_date else None
    ret_time = f" {snapshot.return_time}" if snapshot.return_time else ""

    label = "Ribasso rilevato" if is_drop else "Rincaro rilevato"
    key_suffix = f" — {search.flight_key}" if search.flight_key else ""

    lines = [
        f"<b>{label}: {search.origin} → {search.destination}{key_suffix}</b>",
        f"Andata: {dep}{dep_time}" + (f" · Ritorno: {ret}{ret_time}" if ret else ""),
        f"Nuovo prezzo: <b>{snapshot.price_eur:.0f}€</b> "
        f"(era {previous_price:.0f}€, {change_pct:+.0f}%)",
    ]

    lines.append(_baseline_note(session, search, snapshot))

    if search.red_period_name:
        lines.append(_best_moment_note(session, search, snapshot))

    return "\n".join(lines)


def _baseline_note(session: Session, search: MonitoredSearch, snapshot: PriceSnapshot) -> str:
    baseline = get_baseline(
        session, search.origin, search.destination, snapshot.price_eur, search.red_period_name
    )

    if baseline.tier == PriceTier.INSUFFICIENT_DATA:
        return f"📊 Storico ancora insufficiente su questa rotta per un confronto ({baseline.sample_count} rilevazioni)"

    if baseline.is_red_period:
        intro = f"📊 Rispetto ai {baseline.red_period_name} passati su questa rotta"
        min_label = f"minimo mai visto in un {baseline.red_period_name}"
    else:
        intro = "📊 Rispetto agli ultimi 12 mesi in periodo normale su questa rotta"
        min_label = "minimo mai visto"

    stats = (
        f"{intro} ({baseline.sample_count} rilevazioni): media {baseline.avg_price:.0f}€, "
        f"{min_label} {baseline.min_price:.0f}€ → "
    )

    verdicts = {
        PriceTier.RECORD_LOW: "🔥 <b>nuovo minimo storico mai registrato su questa rotta</b>",
        PriceTier.GOOD_PRICE: "<b>tra i migliori prezzi mai rilevati</b>",
        PriceTier.IN_LINE: "in linea con la media, non un affare storico",
        PriceTier.ABOVE_AVERAGE: "sopra la media, valuta se prenotare comunque se ti serve",
    }
    return stats + verdicts[baseline.tier]


def _best_moment_note(session: Session, search: MonitoredSearch, snapshot: PriceSnapshot) -> str:
    """
    Nota facoltativa aggiunta all'alert quando il volo cade in un periodo rosso
    e lo snapshot attuale rientra nella finestra ottimale d'acquisto calcolata
    (calibrata se disponibile, altrimenti default).
    """
    days_min, days_max, is_calibrated = get_optimal_window(
        session, search.red_period_name, search.origin, search.destination
    )

    if days_min <= snapshot.days_before_departure <= days_max:
        basis = "su dati storici di questa rotta" if is_calibrated else "stima iniziale, non ancora calibrata sui tuoi dati"
        return (
            f"📌 Periodo rosso: <b>{search.red_period_name}</b>. Questo prezzo cade nella finestra "
            f"ottimale d'acquisto ({days_min}-{days_max} giorni prima, {basis}): "
            f"potrebbe essere il momento migliore per comprare."
        )
    return f"📌 Periodo rosso: {search.red_period_name}."
