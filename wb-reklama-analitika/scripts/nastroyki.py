"""Загрузка токенов, лимитов и себестоимости товаров.

Токен ищется в двух местах: сначала переменная окружения, потом файл в reference/.
Так работают оба варианта из инструкции по настройке.
"""

import json
import os
from pathlib import Path

KOREN = Path(__file__).resolve().parent.parent
PAPKA_REFERENCE = KOREN / "reference"
PAPKA_DATA = KOREN / "data"

LIMITY_PO_UMOLCHANIYU = {
    "MAX_CPM": 500,
    "MAX_STEP_PERCENT": 20,
    "TARGET_CPO": 300,
    "TARGET_DRR": 15,
    "MIN_CPM": 100,
    "ALERT_SPEND": 1500,
    "REPORT_TO": "chat",
    "TG_BOT_TOKEN": "",
    "TG_CHAT_ID": "",
}

# Сколько кликов должно накопиться, прежде чем менять ставку.
# Меньше — и мы будем реагировать на случайность, а не на закономерность.
MINIMUM_KLIKOV = 30


class OshibkaNastroek(Exception):
    pass


def _prochitat_json(put, chto_eto):
    try:
        with open(put, encoding="utf-8") as fayl:
            return json.load(fayl)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as oshibka:
        raise OshibkaNastroek(
            f"Файл {put.name} ({chto_eto}) повреждён: {oshibka}. "
            f"Скажи агенту «пересоздай {put.name}» — он перепишет его заново."
        ) from None


def tokeny():
    """Возвращает (основной токен, токен статистики). Второй может совпадать с первым."""
    osnovnoy = os.environ.get("WB_API_TOKEN", "").strip()
    if not osnovnoy:
        fayl = PAPKA_REFERENCE / "token.txt"
        if fayl.exists():
            osnovnoy = fayl.read_text(encoding="utf-8").strip()

    if not osnovnoy:
        raise OshibkaNastroek(
            "Токен Wildberries не найден. Ожидаю его либо в переменной окружения "
            "WB_API_TOKEN, либо в файле reference/token.txt. "
            "Как получить — смотри NASTROYKA.md, шаги 1-2."
        )

    statistiki = os.environ.get("WB_STATS_TOKEN", "").strip()
    if not statistiki:
        fayl = PAPKA_REFERENCE / "token_stats.txt"
        if fayl.exists():
            statistiki = fayl.read_text(encoding="utf-8").strip()

    return osnovnoy, (statistiki or osnovnoy)


def limity():
    """Рамки безопасности. Чего нет в файле — берётся из значений по умолчанию."""
    svoi = _prochitat_json(PAPKA_REFERENCE / "limits.json", "лимиты") or {}
    itog = dict(LIMITY_PO_UMOLCHANIYU)
    itog.update({k: v for k, v in svoi.items() if v is not None and v != ""})
    itog["_zadan_polzovatelem"] = bool(svoi)

    for klyuch in ("MAX_CPM", "MIN_CPM", "MAX_STEP_PERCENT", "TARGET_CPO",
                   "TARGET_DRR", "ALERT_SPEND"):
        try:
            itog[klyuch] = float(itog[klyuch])
        except (TypeError, ValueError):
            raise OshibkaNastroek(
                f"В limits.json значение {klyuch} = {itog[klyuch]!r} — это не число."
            ) from None

    if itog["MIN_CPM"] > itog["MAX_CPM"]:
        raise OshibkaNastroek(
            f"В limits.json MIN_CPM ({itog['MIN_CPM']:.0f}) больше MAX_CPM "
            f"({itog['MAX_CPM']:.0f}) — коридор ставок получился вывернутым наизнанку."
        )
    if not 0 < itog["MAX_STEP_PERCENT"] <= 100:
        raise OshibkaNastroek(
            f"В limits.json MAX_STEP_PERCENT = {itog['MAX_STEP_PERCENT']:.0f}. "
            "Ожидаю число от 1 до 100 — это проценты."
        )
    return itog


def tovary():
    """Цена и себестоимость по артикулам. Без файла агент считает только CPO."""
    syroe = _prochitat_json(PAPKA_REFERENCE / "tovary.json", "себестоимость товаров") or {}
    razobrano = {}
    for artikul, karta in syroe.items():
        try:
            cena = float(karta.get("cena", 0))
            sebes = float(karta.get("sebestoimost", 0))
            komissiya = float(karta.get("komissiya_i_logistika", 0))
        except (AttributeError, TypeError, ValueError):
            continue
        razobrano[int(artikul)] = {
            "nazvanie": karta.get("nazvanie", f"артикул {artikul}"),
            "cena": cena,
            "sebestoimost": sebes,
            "komissiya_i_logistika": komissiya,
            "pribyl_s_zakaza": cena - sebes - komissiya,
        }
    return razobrano


def podgotovit_papki():
    PAPKA_DATA.mkdir(exist_ok=True)
    PAPKA_REFERENCE.mkdir(exist_ok=True)
    return PAPKA_DATA
