"""Отправка отчётов и алертов в Telegram. Нужна только если REPORT_TO = telegram/both."""

import json
import urllib.error
import urllib.request

PREDEL_SOOBSHCHENIYA = 4000


def nastroen(limity):
    return bool(limity.get("TG_BOT_TOKEN")) and bool(limity.get("TG_CHAT_ID"))


def otpravit(limity, tekst):
    """Шлёт текст в Telegram. Возвращает (получилось, что сказать человеку)."""
    if not nastroen(limity):
        return False, ("Telegram не настроен: в limits.json пустые TG_BOT_TOKEN или "
                       "TG_CHAT_ID. Как заполнить — NASTROYKA.md, шаг 4.")

    url = f"https://api.telegram.org/bot{limity['TG_BOT_TOKEN']}/sendMessage"
    kuski = [tekst[i:i + PREDEL_SOOBSHCHENIYA]
             for i in range(0, len(tekst), PREDEL_SOOBSHCHENIYA)] or [tekst]

    for kusok in kuski:
        telo = json.dumps({
            "chat_id": str(limity["TG_CHAT_ID"]),
            "text": kusok,
            "disable_web_page_preview": True,
        }, ensure_ascii=False).encode("utf-8")
        zapros = urllib.request.Request(url, data=telo, method="POST")
        zapros.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(zapros, timeout=30) as otvet:
                otvet.read()
        except urllib.error.HTTPError as oshibka:
            podrobnosti = oshibka.read().decode("utf-8", "replace")[:200]
            if oshibka.code == 400 and "chat not found" in podrobnosti:
                return False, ("Telegram: чат не найден. Напиши своему боту любое "
                               "сообщение — до этого он не может писать первым.")
            if oshibka.code == 401:
                return False, "Telegram: TG_BOT_TOKEN неверный, проверь его у @BotFather."
            return False, f"Telegram ответил {oshibka.code}: {podrobnosti}"
        except urllib.error.URLError as oshibka:
            return False, f"Telegram недоступен: {oshibka.reason}"

    return True, "Отчёт отправлен в Telegram."
