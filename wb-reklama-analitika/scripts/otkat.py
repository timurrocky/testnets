"""«Верни как было» — откат последних изменений ставок.

Берёт последний пакет применённых изменений из data/changes.jsonl и ставит
обратно старые значения. Сам откат тоже записывается в журнал — так что откатить
можно и откат.

    python3 scripts/otkat.py              # показать, что будет возвращено
    python3 scripts/otkat.py --primenit   # вернуть
"""

import argparse
import datetime as dt
import json
import sys

import nastroyki
import wb_api


def prochitat_zhurnal(papka):
    put = papka / "changes.jsonl"
    if not put.exists():
        return []
    zapisi = []
    for nomer, stroka in enumerate(put.read_text(encoding="utf-8").splitlines(), 1):
        stroka = stroka.strip()
        if not stroka:
            continue
        try:
            zapisi.append(json.loads(stroka))
        except json.JSONDecodeError:
            print(f"  Строка {nomer} журнала повреждена, пропускаю.", file=sys.stderr)
    return zapisi


def posledniy_paket(zapisi):
    """Последняя группа реально применённых изменений (по метке запуска)."""
    primenennye = [z for z in zapisi if z.get("primeneno")]
    if not primenennye:
        return []
    posledniy = max(z.get("kogda", "") for z in primenennye)
    # Изменения одного запуска ставятся подряд, но метка времени у каждого своя,
    # поэтому группируем по дате-времени до минуты.
    minuta = posledniy[:16]
    return [z for z in primenennye if z.get("kogda", "")[:16] == minuta]


def main():
    razbor = argparse.ArgumentParser(description="Откат последних изменений ставок")
    razbor.add_argument("--primenit", action="store_true", help="действительно вернуть ставки")
    argumenty = razbor.parse_args()

    papka = nastroyki.podgotovit_papki()
    zapisi = prochitat_zhurnal(papka)
    paket = posledniy_paket(zapisi)

    if not paket:
        print("В журнале нет применённых изменений — откатывать нечего.")
        return 0

    print(f"Последний пакет изменений от {paket[0].get('kogda')}: {len(paket)} шт.\n")
    for zapis in paket:
        print(f"  {zapis['nazvanie']} (id {zapis['advertId']}), «{zapis['gde']}»: "
              f"{zapis['stalo']:.0f} → {zapis['bylo']:.0f} ₽ (возврат)")

    if not argumenty.primenit:
        print("\nЧтобы вернуть: python3 scripts/otkat.py --primenit")
        return 0

    try:
        token, token_stats = nastroyki.tokeny()
    except nastroyki.OshibkaNastroek as oshibka:
        print(f"\n{oshibka}\n", file=sys.stderr)
        return 1

    klient = wb_api.WBClient(token, token_stats)
    vernuto, oshibok = 0, 0
    metka = dt.datetime.now().isoformat(timespec="seconds")

    for zapis in paket:
        staraya = zapis.get("bylo")
        if staraya is None:
            print(f"  Пропускаю id {zapis['advertId']}: не записано старое значение.")
            continue
        try:
            klient.postavit_stavku(zapis["advertId"], zapis["tip"], round(float(staraya)),
                                   zapis["param"], zapis.get("instrument"))
        except wb_api.WBError as oshibka:
            print(f"  ОШИБКА · id {zapis['advertId']}: {oshibka}")
            oshibok += 1
            continue

        with open(papka / "changes.jsonl", "a", encoding="utf-8") as fayl:
            fayl.write(json.dumps({
                "kogda": metka,
                "advertId": zapis["advertId"],
                "nazvanie": zapis["nazvanie"],
                "gde": zapis["gde"],
                "bylo": zapis["stalo"],
                "stalo": staraya,
                "prichina": "откат по команде «верни как было»",
                "tip": zapis["tip"],
                "param": zapis["param"],
                "instrument": zapis.get("instrument"),
                "primeneno": True,
                "eto_otkat": True,
            }, ensure_ascii=False) + "\n")
        vernuto += 1

    print(f"\nВозвращено ставок: {vernuto}"
          + (f", ошибок: {oshibok}" if oshibok else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
