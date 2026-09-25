"""
Test rapido e a sé stante: verifica che il bot possa scrivere nei tre topic
Telegram configurati nel file .env. Manda un messaggio di prova a ciascuno.

Uso: python test_telegram.py
"""
import asyncio
from telegram import Bot

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TOPIC_PSA_CAG, TOPIC_CAG_PSA, TOPIC_MANUAL


async def main():
    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    topics = [
        ("Pisa → Cagliari", TOPIC_PSA_CAG),
        ("Cagliari → Pisa", TOPIC_CAG_PSA),
        ("Monitoraggi manuali", TOPIC_MANUAL),
    ]

    for nome, topic_id in topics:
        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                message_thread_id=topic_id,
                text=f"✅ Test di scrittura riuscito su questo topic ({nome}, id={topic_id})",
            )
            print(f"OK — messaggio inviato con successo a '{nome}' (topic_id={topic_id})")
        except Exception as e:
            print(f"ERRORE su '{nome}' (topic_id={topic_id}): {e}")

    print("\nControlla il gruppo Telegram: dovresti vedere un messaggio di conferma "
          "in ciascuno dei tre topic, nel punto giusto.")


if __name__ == "__main__":
    asyncio.run(main())
