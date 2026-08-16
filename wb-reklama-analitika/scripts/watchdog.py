"""Сторож. Запускается по расписанию, молчит пока всё в норме, сигналит при беде.

Ничего не меняет в кабинете — только смотрит и пишет. Заодно копит историю в
data/istoriya.jsonl, из которой потом считаются «мёртвые часы».

    python3 scripts/watchdog.py           # одна проверка
    python3 scripts/watchdog.py --chasy   # разбор по часам за накопленный период

В cron (Mac/Linux), проверка каждые 30 минут с 8 до 23. Пути абсолютные — у cron
своё окружение, и на тильде он спотыкается:

    */30 8-23 * * * cd /Users/ИМЯ/.claude/skills/wb-reklama-analitika && /usr/bin/python3 scripts/watchdog.py >> /Users/ИМЯ/.claude/skills/wb-reklama-analitika/data/watchdog.log 2>&1

Пять полей: минута, час, день месяца, месяц, день недели. Потеряешь звёздочку —
расписание съедет влево и станет совсем другим.

Токен cron берёт только из reference/token.txt: файл ~/.zshrc он не читает, и
переменной окружения WB_API_TOKEN у него нет.
"""

import argparse
import collections
import datetime as dt
import json
import sys

import nastroyki
import telega
import wb_api

# Сколько проверок подряд кампания может стоять без единого показа,
# прежде чем это считается остановкой, а не паузой между показами.
PROVEROK_TISHINY = 3


def _dengi(summa):
    return f"{float(summa or 0):,.0f} ₽".replace(",", " ")


def sostoyanie_put(papka):
    return papka / "storozh_sostoyanie.json"


def prochitat_sostoyanie(papka):
    put = sostoyanie_put(papka)
    if not put.exists():
        return {"den": "", "otpravleno": [], "tishina": {}}
    try:
        return json.loads(put.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"den": "", "otpravleno": [], "tishina": {}}


def zapisat_sostoyanie(papka, sostoyanie):
    sostoyanie_put(papka).write_text(
        json.dumps(sostoyanie, ensure_ascii=False, indent=2), encoding="utf-8")


def proverka(papka, limity, tikho=False):
    """Одна проверка. Возвращает список новых тревог."""
    token, token_stats = nastroyki.tokeny()
    klient = wb_api.WBClient(token, token_stats, tikho=True)

    segodnya = dt.date.today().isoformat()
    seychas = dt.datetime.now().isoformat(timespec="seconds")

    ids = klient.id_kampaniy(tolko_aktivnye=True)
    kampanii = klient.detali_kampaniy(ids) if ids else []
    statistika = klient.statistika(ids, segodnya, segodnya) if ids else []

    imena = {int(k["advertId"]): k.get("name") or f"кампания {k['advertId']}"
             for k in kampanii if k.get("advertId")}
    statusy = {int(k["advertId"]): k.get("status") for k in kampanii if k.get("advertId")}

    sostoyanie = prochitat_sostoyanie(papka)
    if sostoyanie.get("den") != segodnya:
        sostoyanie = {"den": segodnya, "otpravleno": [], "tishina": {}}

    otpravleno = set(sostoyanie.get("otpravleno", []))
    tishina = sostoyanie.get("tishina", {})
    trevogi = []
    stroki_istorii = []

    for zapis in statistika:
        advert_id = zapis.get("advertId") or zapis.get("id")
        if not advert_id:
            continue
        advert_id = int(advert_id)
        imya = imena.get(advert_id, f"кампания {advert_id}")
        rashod = float(zapis.get("sum") or 0)
        zakazy = float(zapis.get("orders") or 0)
        kliki = float(zapis.get("clicks") or 0)
        pokazy = float(zapis.get("views") or 0)

        stroki_istorii.append({
            "kogda": seychas,
            "advertId": advert_id,
            "nazvanie": imya,
            "rashod_za_den": rashod,
            "zakazy_za_den": zakazy,
            "kliki_za_den": kliki,
            "pokazy_za_den": pokazy,
        })

        klyuch_sliv = f"sliv:{advert_id}"
        if rashod >= limity["ALERT_SPEND"] and zakazy == 0 and klyuch_sliv not in otpravleno:
            trevogi.append(
                f"СЛИВ · {imya} (id {advert_id})\n"
                f"Сегодня потрачено {_dengi(rashod)}, заказов — ноль.\n"
                f"Порог тревоги: {_dengi(limity['ALERT_SPEND'])}.\n"
                f"Я ничего не меняю сама. Скажи «аналитика рекламы ВБ», "
                f"чтобы разобрать и снизить ставку."
            )
            otpravleno.add(klyuch_sliv)

        klyuch_pauza = f"pauza:{advert_id}"
        if statusy.get(advert_id) == 11 and klyuch_pauza not in otpravleno:
            trevogi.append(
                f"ВСТАЛА · {imya} (id {advert_id})\n"
                f"Кампания на паузе, показов нет. За сегодня успела потратить {_dengi(rashod)}."
            )
            otpravleno.add(klyuch_pauza)

        # Кампания активна, но показов не прибавляется — обычно кончился бюджет.
        proshloe = tishina.get(str(advert_id), {})
        if statusy.get(advert_id) == 9 and pokazy == proshloe.get("pokazy", -1):
            schetchik = proshloe.get("schetchik", 0) + 1
        else:
            schetchik = 0
        tishina[str(advert_id)] = {"pokazy": pokazy, "schetchik": schetchik}

        klyuch_stoit = f"stoit:{advert_id}"
        if schetchik >= PROVEROK_TISHINY and klyuch_stoit not in otpravleno:
            trevogi.append(
                f"НЕ КРУТИТСЯ · {imya} (id {advert_id})\n"
                f"Статус «идут показы», но за последние "
                f"{PROVEROK_TISHINY * 30} минут ни одного нового показа.\n"
                f"Чаще всего это дневной бюджет или закончившийся баланс."
            )
            otpravleno.add(klyuch_stoit)

    balans = klient.balans()
    if balans:
        vsego = float(balans.get("balance") or 0) + float(balans.get("bonus") or 0)
        if vsego < limity["ALERT_SPEND"] and "balans" not in otpravleno:
            trevogi.append(
                f"БАЛАНС · на счету осталось {_dengi(vsego)} — "
                f"это меньше твоего дневного порога {_dengi(limity['ALERT_SPEND'])}. "
                f"Кампании могут встать."
            )
            otpravleno.add("balans")

    if stroki_istorii:
        with open(papka / "istoriya.jsonl", "a", encoding="utf-8") as fayl:
            for stroka in stroki_istorii:
                fayl.write(json.dumps(stroka, ensure_ascii=False) + "\n")

    sostoyanie["otpravleno"] = sorted(otpravleno)
    sostoyanie["tishina"] = tishina
    zapisat_sostoyanie(papka, sostoyanie)

    return trevogi


def mertvye_chasy(papka, limity):
    """Ищет часы, в которые деньги уходят, а заказы не приходят."""
    put = papka / "istoriya.jsonl"
    if not put.exists():
        return ("Истории пока нет. Сторож копит её сам — вернись через день-два "
                "работы по расписанию.")

    zapisi = []
    for stroka in put.read_text(encoding="utf-8").splitlines():
        stroka = stroka.strip()
        if stroka:
            try:
                zapisi.append(json.loads(stroka))
            except json.JSONDecodeError:
                continue

    # В истории лежат накопленные за день суммы, поэтому час к часу считаем разницу.
    po_kampaniyam = collections.defaultdict(list)
    for zapis in zapisi:
        kogda = zapis.get("kogda", "")
        po_kampaniyam[(zapis.get("advertId"), kogda[:10])].append(zapis)

    po_chasam = collections.defaultdict(lambda: {"rashod": 0.0, "zakazy": 0.0})
    for zamery in po_kampaniyam.values():
        zamery.sort(key=lambda z: z.get("kogda", ""))
        for predydushchiy, tekushchiy in zip(zamery, zamery[1:]):
            chas = int(tekushchiy["kogda"][11:13])
            prirost_rashoda = tekushchiy["rashod_za_den"] - predydushchiy["rashod_za_den"]
            prirost_zakazov = tekushchiy["zakazy_za_den"] - predydushchiy["zakazy_za_den"]
            if prirost_rashoda < 0 or prirost_zakazov < 0:
                continue  # начался новый день, счётчики обнулились
            po_chasam[chas]["rashod"] += prirost_rashoda
            po_chasam[chas]["zakazy"] += prirost_zakazov

    if not po_chasam:
        return ("Замеров пока мало — для разбора по часам нужно хотя бы два запуска "
                "сторожа подряд в один день.")

    stroki = ["РАЗБОР ПО ЧАСАМ", "",
              "Час   Расход      Заказы   CPO", ""]
    plokhie = []
    for chas in sorted(po_chasam):
        dannye = po_chasam[chas]
        cpo = dannye["rashod"] / dannye["zakazy"] if dannye["zakazy"] else None
        stroki.append(f"{chas:02d}:00 {_dengi(dannye['rashod']):>10}  "
                      f"{dannye['zakazy']:>6.0f}   "
                      f"{_dengi(cpo) if cpo else '—'}")
        if dannye["rashod"] > 0 and dannye["zakazy"] == 0:
            plokhie.append((chas, dannye["rashod"]))
        elif cpo and cpo > limity["TARGET_CPO"] * 1.5:
            plokhie.append((chas, dannye["rashod"]))

    stroki.append("")
    if plokhie:
        vsego = sum(r for _, r in plokhie)
        chasy = ", ".join(f"{c:02d}:00" for c, _ in plokhie)
        stroki.append(f"Мёртвые часы: {chasy}")
        stroki.append(f"За период они съели {_dengi(vsego)} почти без отдачи.")
        stroki.append("В кабинете WB нет почасового расписания показов, поэтому "
                      "это повод не отключать часы, а снизить ставку и следить, "
                      "не сместится ли расход в рабочее время.")
    else:
        stroki.append("Явно провальных часов не видно — расход распределён ровно.")

    return "\n".join(stroki)


def main():
    razbor = argparse.ArgumentParser(description="Сторож рекламы WB")
    razbor.add_argument("--chasy", action="store_true", help="разбор по часам вместо проверки")
    argumenty = razbor.parse_args()

    papka = nastroyki.podgotovit_papki()
    try:
        limity = nastroyki.limity()
    except nastroyki.OshibkaNastroek as oshibka:
        print(f"{oshibka}", file=sys.stderr)
        return 1

    if argumenty.chasy:
        print(mertvye_chasy(papka, limity))
        return 0

    seychas = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        trevogi = proverka(papka, limity)
    except (nastroyki.OshibkaNastroek, wb_api.WBError) as oshibka:
        print(f"[{seychas}] Проверка не прошла: {oshibka}", file=sys.stderr)
        return 1

    if not trevogi:
        print(f"[{seychas}] всё в норме")
        return 0

    tekst = f"РЕКЛАМА WB · {seychas}\n\n" + "\n\n".join(trevogi)
    print(tekst)

    if telega.nastroen(limity):
        poluchilos, chto_skazat = telega.otpravit(limity, tekst)
        print(f"[{seychas}] {chto_skazat}")
    else:
        print(f"[{seychas}] Telegram не настроен — тревога осталась только в логе. "
              f"Настроить: NASTROYKA.md, шаг 4.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
