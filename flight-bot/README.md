# Flight Bot

Bot Telegram che monitora i prezzi dei voli Pisa-Cagliari/Cagliari-Pisa nei
weekend (partenza sabato sera, ritorno domenica sera) più eventuali
monitoraggi manuali su date specifiche, e avvisa quando il prezzo scende.

Fonte prezzi: solo Google Flights, tramite la libreria `fast-flights`
(https://github.com/AWeirdDev/fast-flights). eDreams è stato scartato di
proposito: niente scraper dedicato integrato nel bot, dato che i prezzi
Prime si scostano poco da Google Flights — il controllo va fatto a mano
su eDreams quando arriva un alert.

## Struttura

```
config.py        # rotte, topic Telegram, periodi rossi di default, soglie
models.py         # schema DB (SQLAlchemy)
db.py              # connessione/sessione DB
scrapers/
  google_flights.py  # basato su fast-flights, verificato (vedi sotto)
calibration.py    # auto-calibrazione finestra ottimale d'acquisto periodi rossi
alerts.py           # valutazione ribassi + invio messaggi Telegram
scheduler.py       # job periodici (generazione weekend, scraping, calibrazione)
bot.py               # comandi Telegram per i monitoraggi manuali
main.py             # entry point
```

## Setup

1. `pip install -r requirements.txt`
2. Copia `.env.example` in `.env` e compila:
   - `TELEGRAM_BOT_TOKEN`: token del bot da BotFather
   - `TELEGRAM_CHAT_ID`: ID del gruppo Telegram con i topic abilitati
   - `TOPIC_PSA_CAG`, `TOPIC_CAG_PSA`, `TOPIC_MANUAL`: ID dei tre topic
     (si trovano nell'URL quando apri il topic da Telegram desktop/web)
   - `DATABASE_URL`: connection string Postgres del provider cloud scelto
     (Supabase, Neon e Railway hanno tutti un tier gratuito sufficiente
     per iniziare — nessuno è stato ancora scelto, da decidere)
3. `python main.py`

## Cosa è già deciso e implementato

- Schema DB con storico prezzi, per calcolare medie e calibrare i periodi rossi
- Generazione automatica dei weekend PSA-CAG/CAG-PSA in un orizzonte scorrevole
  (default 52 settimane), con creazione della riga di monitoraggio anche
  prima che il volo sia "rilasciato": il primo scrape utile lo valorizza
  appena la compagnia lo pubblica
- Comando `/monitora` per aggiungere monitoraggi manuali su date specifiche
- Alert solo sui ribassi (soglia configurabile, default 5%), instradati
  automaticamente sul topic giusto in base al tipo di monitoraggio
- Nota "momento migliore per comprare" negli alert quando il volo cade in un
  periodo rosso e il prezzo rilevato è dentro la finestra ottimale
- Auto-calibrazione: finché non c'è abbastanza storico (default: 8 campioni
  per rotta+periodo) si usano i range di default in `config.py`; dopo,
  `calibration.py` calcola la finestra reale osservata e la usa al posto del default

### Sullo scraper Google Flights

`scrapers/google_flights.py` usa la libreria `fast-flights`, un progetto
open-source di terze parti che ho verificato leggendo il suo codice sorgente
(non fa scraping del DOM: replica la richiesta interna di Google Flights e
legge il JSON che la pagina incorpora in un tag `<script>`). Ho anche
verificato che la costruzione della query (codifica rotta/date nel formato
protobuf che Google si aspetta) funziona correttamente eseguendola.

Quello che NON ho potuto verificare da questo ambiente: la chiamata di rete
vera e propria verso google.com, perché il sandbox in cui ho scritto questo
codice non ha accesso a quel dominio. Il primo test reale va fatto da te,
lanciando `python main.py` o uno script minimo che chiama
`scrape_google(...)` su una rotta/data note.

## Cosa manca prima di poter andare in produzione

1. **Primo test reale dello scraper** (vedi sopra) — priorità più alta.
2. **Scelta del provider cloud** per DB e hosting del processo (il bot deve
   girare 24/7 per lo scheduler — serve un servizio tipo Railway/Render/Fly.io,
   non solo il DB).
3. **Gestione errori/robustezza**: al momento uno scraping fallito viene
   solo loggato; da valutare se serve un alert Telegram (es. su un topic di
   servizio a parte) quando lo scraper fallisce ripetutamente, per accorgersi
   se Google ha cambiato la struttura dei dati interni (raro ma possibile:
   in tal caso va aggiornata la libreria fast-flights, non il nostro codice).
4. **Pulizia dei monitoraggi scaduti**: le ricerche con `departure_date` nel
   passato restano nel DB come `active=True` — serve un job che le disattivi.
5. Nessun test automatico incluso per ora.


## Aggiornamenti — monitoraggio a 360°, chiavi volo, baseline storiche

- **Chiave univoca per volo**: ogni monitoraggio (auto o manuale) ha una chiave tipo
  `PSACAG0491` (origine+destinazione+ID), mostrata in ogni alert.
- **Rincari**: segnalati (oltre ai ribassi) solo per i monitoraggi manuali e per i
  voli automatici "promossi". Sui ~104 weekend automatici normali, solo i ribassi.
- **Promozione**: `/monitora PSACAG0491` trasforma un weekend automatico in
  monitoraggio completo — i suoi alert si spostano nel topic dei monitoraggi
  manuali e iniziano a includere anche i rincari.
- **Baseline storiche**: ogni alert include un confronto con lo storico della
  rotta (`baseline.py`), separato per periodo normale (ultimi 12 mesi, esclusi
  periodi rossi) e periodo rosso (ultime 3 edizioni dello stesso periodo, es.
  solo altri Natali) — per non far sembrare sempre "caro" un prezzo di alta
  stagione confrontandolo con un minimo raggiungibile solo in bassa stagione.
- **Pulizia automatica**: i monitoraggi con `departure_date` passata vengono
  disattivati ogni notte (restano nel DB come storico per le baseline, solo
  `active` passa a `False`).

Logica testata end-to-end con un DB SQLite in memoria (baseline, generazione
chiavi, formattazione messaggi) — non solo verificata sintatticamente.


## Aggiornamento — orario del volo negli alert, filtro orario solo sui weekend automatici

- Ogni alert mostra ora l'orario reale del volo (es. "Andata: 14/11/2026 19:35"),
  non solo la data.
- Il filtro "partenza serale" (sabato/domenica dalle 17:00) si applica SOLO ai
  weekend automatici, tramite `earliest_departure_hour` — un parametro nativo
  di `fast-flights` che passa il filtro direttamente a Google (non un filtro
  fatto in locale sui risultati, verificato leggendo il codice sorgente della
  libreria).
- I monitoraggi manuali (`/monitora ORIGINE DESTINAZIONE DATA`) NON applicano
  nessun filtro orario: coprono tutti i voli disponibili in quella data.
- **Punto non verificabile da qui**: per le ricerche andata+ritorno, l'orario
  di ritorno mostrato assume che l'ultima tappa nel payload di Google sia
  quella di ritorno (vedi commento in `scrapers/google_flights.py`). Da
  confermare al primo test reale — se l'orario di ritorno risultasse errato,
  è quel punto del codice da correggere.


## Aggiornamento — risolto il muro di consenso cookie UE su Google Flights

Il cookie statico "CONSENT=YES+" (tecnica diffusa ma vecchia) NON bypassa più
il muro di consenso per i visitatori UE — verificato con un test reale dal
Raspberry Pi (IP italiano), tornava ancora la pagina di consenso invece dei
risultati. La soluzione verificata funzionante: individuare il modulo
"Accetta" nella pagina di consenso e inviarlo per davvero via POST (stesso
meccanismo di un click reale nel browser). Fatto questo, il client riusa il
cookie di consenso ottenuto per tutte le richieste successive nello stesso
processo — il passaggio extra si paga solo alla primissima richiesta di ogni
avvio del bot. Testato con successo: primo volo reale recuperato a 26€
(PSA→CAG). Vedi i commenti in `scrapers/google_flights.py` per i dettagli.

File di debug (`debug_scraper.py`, `debug_scraper2.py`) lasciati nel progetto:
tornano utili se Google dovesse mai cambiare di nuovo la pagina di consenso.


## Aggiornamento — solo voli senza scali (weekend automatici + /cerca), nuovo comando /cerca

- I weekend automatici ora considerano SOLO voli senza scali (`direct_only=True`
  nello scheduler): se il volo più economico del giorno ha uno scalo, viene
  ignorato e si guarda il più economico tra quelli diretti.
- Nuovo comando `/cerca ORIGINE DESTINAZIONE GG-MM-AAAA [ritorno]`: mostra fino
  a 6 opzioni di volo senza scali con prezzo attuale, tramite bottoni inline
  Telegram. Scegliendone uno, nasce un monitoraggio manuale a tutti gli
  effetti (chiave, monitoraggio completo con rincari, topic monitoraggi
  manuali) che traccia il prezzo più basso in una fascia oraria di ±1 ora
  attorno all'orario scelto (non un volo esatto: più robusto, dato che Google
  Flights non permette di tracciare un volo specifico per compagnia+orario).
  La fascia è configurabile in `config.py` (`SEARCH_TIME_WINDOW_HOURS`).
- Autocomplete dei comandi "/" registrato su Telegram (`set_my_commands`),
  con /monitora, /cerca, /elenco elencati con descrizione.
- Logica di filtro "solo diretti" e di conteggio delle tappe testata con dati
  finti (non contro Google reale): un'opzione con scalo più economica di
  quelle dirette viene correttamente esclusa.
