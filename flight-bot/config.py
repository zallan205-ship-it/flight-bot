"""
Configurazione centrale del bot.

Le variabili sensibili (token, connection string) vanno in un file .env
(vedi .env.example) e sono lette tramite python-dotenv. Tutto il resto
(rotte, periodi rossi, soglie) è qui perché sono parametri di dominio
che modifichiamo spesso durante lo sviluppo.
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

# --- Credenziali / infrastruttura -------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])  # gruppo con i topic abilitati

# ID dei tre topic (thread) Telegram. Si ottengono aprendo il topic nell'app
# Telegram desktop/web: sono nell'URL (.../c/<chat_id>/<message_thread_id>).
TOPIC_PSA_CAG = int(os.environ["TOPIC_PSA_CAG"])
TOPIC_CAG_PSA = int(os.environ["TOPIC_CAG_PSA"])
TOPIC_MANUAL = int(os.environ["TOPIC_MANUAL"])

DATABASE_URL = os.environ["DATABASE_URL"]  # es. postgresql://user:pass@host:5432/flightbot

# --- Rotte monitorate in automatico ------------------------------------------------

AUTO_ROUTES = [
    {"origin": "PSA", "destination": "CAG", "topic_id": TOPIC_PSA_CAG},
    {"origin": "CAG", "destination": "PSA", "topic_id": TOPIC_CAG_PSA},
]

# Weekend tipo: partenza sabato sera, ritorno domenica sera.
WEEKEND_DEPARTURE_WEEKDAY = 5   # 0=lunedì ... 5=sabato
WEEKEND_DEPARTURE_MIN_HOUR = 17  # "sera" = dalle 17:00 in poi
WEEKEND_RETURN_WEEKDAY = 6      # domenica
WEEKEND_RETURN_MIN_HOUR = 17

# Quanti weekend futuri tenere sotto monitoraggio contemporaneamente.
# Si allarga automaticamente quando le compagnie rilasciano nuove date
# (vedi scheduler.discover_newly_released_weekends).
WEEKENDS_HORIZON_TO_TRACK = 52

# --- Periodi rossi (alta domanda) e finestra d'acquisto ottimale ------------------
#
# I range "days_before_min/max" sono valori di DEFAULT usati finché non c'è
# storico sufficiente. Una volta che calibration.py ha abbastanza campioni
# per una rotta+periodo, questi vengono sovrascritti nella tabella
# `red_period_calibration` del DB e i default qui sotto diventano solo il
# fallback iniziale.
#
# Fonti dei default iniziali: media di più guide di viaggio generiche
# (non un dato ufficiale delle compagnie - per questo servirà la calibrazione).

@dataclass
class RedPeriod:
    name: str
    month_start: int
    day_start: int
    month_end: int
    day_end: int
    days_before_min: int  # finestra ottimale di acquisto: minimo giorni-prima-partenza
    days_before_max: int  # finestra ottimale di acquisto: massimo giorni-prima-partenza
    min_samples_for_calibration: int = 8  # sotto questa soglia si usa il default

RED_PERIODS: list[RedPeriod] = [
    RedPeriod("Natale/Capodanno", 12, 15, 1, 7, days_before_min=60, days_before_max=100),
    RedPeriod("Ferragosto", 8, 1, 8, 20, days_before_min=90, days_before_max=130),
    RedPeriod("Pasqua", 3, 20, 4, 15, days_before_min=60, days_before_max=90),
    # Pasqua ha data mobile: da affinare con calendario liturgico se serve precisione.
]

# --- Alert -------------------------------------------------------------------------

# Alert solo se il prezzo scende di almeno questa percentuale rispetto
# all'ultimo prezzo noto per la stessa combinazione rotta+data+orario.
PRICE_DROP_THRESHOLD_PCT = 5.0

# Soglia per segnalare un RINCARO (solo su monitoraggi manuali o voli "promossi").
PRICE_RISE_THRESHOLD_PCT = 5.0

# Intervallo di polling dello scraping (minuti). Differenziato per dare priorità
# alle rotte automatiche rispetto a quelle manuali se il carico diventa un problema.
SCRAPE_INTERVAL_MINUTES_AUTO = 180
SCRAPE_INTERVAL_MINUTES_MANUAL = 180

# --- Baseline storiche (per giudicare se un prezzo è conveniente) --------------------

# Baseline "periodo normale": guarda ai prezzi della stessa rotta rilevati negli
# ultimi N mesi, ESCLUDENDO i voli che cadono in un periodo rosso (altrimenti la
# media verrebbe gonfiata dall'alta stagione).
NORMAL_BASELINE_MONTHS = 12

# Baseline "periodo rosso": guarda solo ai prezzi di voli che cadono nello STESSO
# periodo rosso (es. solo altri Natali), nelle ultime N edizioni. Non ha senso
# limitarla a 12 mesi: un periodo rosso capita una volta l'anno, quindi servono
# più edizioni per avere un campione utile.
RED_PERIOD_BASELINE_YEARS = 3

# Sotto questa soglia di campioni, il bot dichiara lo storico insufficiente
# invece di forzare un giudizio su troppo pochi dati.
MIN_SAMPLES_FOR_BASELINE = 5

# Un prezzo entro questa percentuale sopra il minimo storico viene giudicato
# "buon prezzo" (non solo il minimo esatto).
GOOD_PRICE_MARGIN_PCT = 10.0

# --- Ricerca interattiva /cerca -----------------------------------------------------

# Quanti voli (senza scali) mostrare come opzioni con bottone dopo /cerca.
SEARCH_MAX_OPTIONS = 6

# Ampiezza della fascia oraria (in ore, ± attorno all'orario scelto) quando si
# promuove un'opzione di /cerca a monitoraggio: invece di tracciare un volo
# esatto (fragile: può sparire dai risultati se cambia orario/aereo), si
# traccia il prezzo più basso in questa fascia.
SEARCH_TIME_WINDOW_HOURS = 1
