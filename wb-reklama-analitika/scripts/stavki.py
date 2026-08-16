"""Применение ставок. Единственный скрипт, который что-то меняет в кабинете.

Здесь лимиты проверяются ещё раз, независимо от analiz.py: даже если в
рекомендации попадёт заведомо дикая цифра, скрипт её не пропустит.

Что скрипт НЕ умеет и не должен уметь: останавливать кампании, менять дневные
бюджеты, пополнять баланс. Этих вызовов тут просто нет.

    python3 scripts/stavki.py              # показать, что будет сделано
    python3 scripts/stavki.py --primenit   # применить
"""

import argparse
import datetime as dt
import json
import sys

import nastroyki
import wb_api


class Otkloneno(Exception):
    """Изменение не прошло проверку лимитов."""


def proverit(predlozhenie, limity):
    """Последний рубеж. Кидает Otkloneno, если ставка выходит за рамки."""
    novaya = predlozhenie.get("novaya")
    tekushchaya = predlozhenie.get("tekushchaya")

    if novaya is None:
        raise Otkloneno("новая ставка не задана")
    novaya = float(novaya)
    if tekushchaya is None:
        raise Otkloneno("текущая ставка неизвестна — без неё нельзя проверить шаг")
    tekushchaya = float(tekushchaya)

    if novaya > limity["MAX_CPM"]:
        raise Otkloneno(
            f"{novaya:.0f} ₽ выше потолка MAX_CPM ({limity['MAX_CPM']:.0f} ₽)"
        )
    if novaya < limity["MIN_CPM"]:
        raise Otkloneno(
            f"{novaya:.0f} ₽ ниже пола MIN_CPM ({limity['MIN_CPM']:.0f} ₽)"
        )
    if tekushchaya > 0:
        shag = abs(novaya - tekushchaya) / tekushchaya * 100
        if shag > limity["MAX_STEP_PERCENT"] + 0.5:
            raise Otkloneno(
                f"изменение на {shag:.0f}% больше разрешённых "
                f"{limity['MAX_STEP_PERCENT']:.0f}% за раз"
            )
    if predlozhenie.get("param") is None or predlozhenie.get("tip") is None:
        raise Otkloneno("не хватает параметров кампании для вызова API")

    return int(round(novaya))


def zapisat_v_zhurnal(papka, zapis):
    """Пишем ДО применения — чтобы «верни как было» работало всегда."""
    with open(papka / "changes.jsonl", "a", encoding="utf-8") as fayl:
        fayl.write(json.dumps(zapis, ensure_ascii=False) + "\n")


def main():
    razbor = argparse.ArgumentParser(description="Применить ставки по рекомендациям")
    razbor.add_argument("--primenit", action="store_true",
                        help="действительно изменить ставки (без флага — только показ)")
    razbor.add_argument("--kampaniya", type=int, help="только одна кампания по id")
    argumenty = razbor.parse_args()

    papka = nastroyki.podgotovit_papki()
    put = papka / "rekomendacii.json"
    if not put.exists():
        print("Нет data/rekomendacii.json — сначала запусти scripts/analiz.py", file=sys.stderr)
        return 1

    # Токен нужен только чтобы применить. Посмотреть, что будет сделано,
    # можно и без него.
    try:
        limity = nastroyki.limity()
        token, token_stats = nastroyki.tokeny() if argumenty.primenit else (None, None)
    except nastroyki.OshibkaNastroek as oshibka:
        print(f"\n{oshibka}\n", file=sys.stderr)
        return 1

    dannye = json.loads(put.read_text(encoding="utf-8"))
    k_rabote = []
    for kampaniya in dannye.get("kampanii", []):
        if argumenty.kampaniya and kampaniya["id"] != argumenty.kampaniya:
            continue
        for predlozhenie in kampaniya.get("predlozheniya", []):
            if predlozhenie.get("novaya"):
                k_rabote.append((kampaniya, predlozhenie))

    if not k_rabote:
        print("Менять нечего — рекомендаций по ставкам нет.")
        return 0

    if not argumenty.primenit:
        print(f"Режим показа. Было бы изменено ставок: {len(k_rabote)}\n")
        for kampaniya, predlozhenie in k_rabote:
            try:
                novaya = proverit(predlozhenie, limity)
                print(f"  {kampaniya['nazvanie']} (id {kampaniya['id']}), "
                      f"«{predlozhenie['gde']}»: "
                      f"{predlozhenie['tekushchaya']:.0f} → {novaya} ₽ — "
                      f"{predlozhenie['prichina']}")
            except Otkloneno as prichina:
                print(f"  ОТКЛОНЕНО · {kampaniya['nazvanie']} (id {kampaniya['id']}), "
                      f"«{predlozhenie['gde']}»: {prichina}")
        print("\nЧтобы применить: python3 scripts/stavki.py --primenit")
        return 0

    klient = wb_api.WBClient(token, token_stats)
    sdelano, otkloneno, oshibok = 0, 0, 0
    metka_zapuska = dt.datetime.now().isoformat(timespec="seconds")

    for kampaniya, predlozhenie in k_rabote:
        podpis = (f"{kampaniya['nazvanie']} (id {kampaniya['id']}), "
                  f"«{predlozhenie['gde']}»")
        try:
            novaya = proverit(predlozhenie, limity)
        except Otkloneno as prichina:
            print(f"  ОТКЛОНЕНО · {podpis}: {prichina}")
            otkloneno += 1
            continue

        zapisat_v_zhurnal(papka, {
            "kogda": metka_zapuska,
            "advertId": kampaniya["id"],
            "nazvanie": kampaniya["nazvanie"],
            "gde": predlozhenie["gde"],
            "bylo": predlozhenie["tekushchaya"],
            "stalo": novaya,
            "prichina": predlozhenie["prichina"],
            "tip": predlozhenie["tip"],
            "param": predlozhenie["param"],
            "instrument": predlozhenie["instrument"],
            "primeneno": False,
        })

        try:
            klient.postavit_stavku(
                kampaniya["id"], predlozhenie["tip"], novaya,
                predlozhenie["param"], predlozhenie["instrument"],
            )
        except wb_api.WBError as oshibka:
            print(f"  ОШИБКА · {podpis}: {oshibka}")
            oshibok += 1
            continue

        zapisat_v_zhurnal(papka, {
            "kogda": dt.datetime.now().isoformat(timespec="seconds"),
            "advertId": kampaniya["id"],
            "nazvanie": kampaniya["nazvanie"],
            "gde": predlozhenie["gde"],
            "bylo": predlozhenie["tekushchaya"],
            "stalo": novaya,
            "prichina": predlozhenie["prichina"],
            "tip": predlozhenie["tip"],
            "param": predlozhenie["param"],
            "instrument": predlozhenie["instrument"],
            "primeneno": True,
        })
        print(f"  {podpis}: {predlozhenie['tekushchaya']:.0f} → {novaya} ₽")
        sdelano += 1

    print(f"\nИзменено ставок: {sdelano}"
          + (f", отклонено лимитами: {otkloneno}" if otkloneno else "")
          + (f", ошибок API: {oshibok}" if oshibok else ""))
    if sdelano:
        print("Кабинет применяет новые ставки с задержкой до пары минут.")
        print("Откатить: python3 scripts/otkat.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
