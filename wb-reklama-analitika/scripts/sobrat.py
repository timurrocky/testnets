"""Собирает срез рекламного кабинета: кампании, ставки, статистика, воронка продаж.

Ничего не меняет. Результат кладёт в data/srez.json — с ним дальше работают
analiz.py и stavki.py.

    python3 scripts/sobrat.py            # за последние 7 дней
    python3 scripts/sobrat.py --dni 30
"""

import argparse
import datetime as dt
import json
import sys

import nastroyki
import wb_api


def sobrat(dney=7, tikho=False):
    token, token_stats = nastroyki.tokeny()
    klient = wb_api.WBClient(token, token_stats, tikho=tikho)

    segodnya = dt.date.today()
    data_ot = (segodnya - dt.timedelta(days=dney - 1)).isoformat()
    data_do = segodnya.isoformat()

    if not tikho:
        print(f"Смотрю кампании за период {data_ot} — {data_do}...", file=sys.stderr)

    ids = klient.id_kampaniy(tolko_aktivnye=True)
    if not tikho:
        print(f"  Активных кампаний: {len(ids)}", file=sys.stderr)

    kampanii = klient.detali_kampaniy(ids) if ids else []
    statistika = klient.statistika(ids, data_ot, data_do) if ids else []
    balans = klient.balans()

    stat_po_id = {}
    for zapis in statistika:
        advert_id = zapis.get("advertId") or zapis.get("id")
        if advert_id:
            stat_po_id[int(advert_id)] = zapis

    vse_artikuly = []
    for kampaniya in kampanii:
        vse_artikuly.extend(wb_api.artikuly_kampanii(kampaniya))
    voronka = klient.voronka(sorted(set(vse_artikuly)), data_ot, data_do)

    srez = {
        "sobrano": dt.datetime.now().isoformat(timespec="seconds"),
        "period": {"ot": data_ot, "do": data_do, "dney": dney},
        "balans": balans,
        "kampanii": [],
        "voronka": voronka,
    }

    for kampaniya in kampanii:
        advert_id = kampaniya.get("advertId")
        if not advert_id:
            continue
        stat = stat_po_id.get(int(advert_id), {})
        srez["kampanii"].append({
            "id": int(advert_id),
            "nazvanie": kampaniya.get("name") or f"кампания {advert_id}",
            "tip": kampaniya.get("type"),
            "tip_slovami": wb_api.TIPY_KAMPANIY.get(kampaniya.get("type"), "неизвестный тип"),
            "status": kampaniya.get("status"),
            "status_slovami": wb_api.STATUSY.get(kampaniya.get("status"), "неизвестный статус"),
            "dnevnoy_byudzhet": kampaniya.get("dailyBudget"),
            "stavki": wb_api.stavka_kampanii(kampaniya),
            "artikuly": wb_api.artikuly_kampanii(kampaniya),
            "statistika": {
                "pokazy": stat.get("views", 0),
                "kliki": stat.get("clicks", 0),
                "ctr": stat.get("ctr", 0),
                "cpc": stat.get("cpc", 0),
                "rashod": stat.get("sum", 0),
                "v_korzinu": stat.get("atbs", 0),
                "zakazy": stat.get("orders", 0),
                "shtuk": stat.get("shks", 0),
                "vyruchka": stat.get("sum_price", 0),
            },
            "po_dnyam": [
                {
                    "data": den.get("date", "")[:10],
                    "rashod": den.get("sum", 0),
                    "kliki": den.get("clicks", 0),
                    "zakazy": den.get("orders", 0),
                    "vyruchka": den.get("sum_price", 0),
                }
                for den in (stat.get("days") or [])
            ],
        })

    return srez


def main():
    razbor = argparse.ArgumentParser(description="Собрать данные рекламного кабинета WB")
    razbor.add_argument("--dni", type=int, default=7, help="за сколько дней (по умолчанию 7)")
    razbor.add_argument("--tikho", action="store_true", help="без пояснений в процессе")
    argumenty = razbor.parse_args()

    papka = nastroyki.podgotovit_papki()
    try:
        srez = sobrat(dney=argumenty.dni, tikho=argumenty.tikho)
    except (nastroyki.OshibkaNastroek, wb_api.WBError) as oshibka:
        print(f"\n{oshibka}\n", file=sys.stderr)
        return 1

    put = papka / "srez.json"
    put.write_text(json.dumps(srez, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Готово: {len(srez['kampanii'])} кампаний сохранено в {put}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
