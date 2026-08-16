"""Разбор среза: что происходит с каждой кампанией и что с ней делать.

Ничего не меняет в кабинете. Печатает отчёт человеку и складывает предложения по
ставкам в data/rekomendacii.json — их потом применяет stavki.py.

    python3 scripts/analiz.py                     # разбор + отчёт
    python3 scripts/analiz.py --itogi-nedeli      # вердикт по неделе
    python3 scripts/analiz.py --chto-esli 123:400 # прогноз без изменений
"""

import argparse
import json
import sys

import nastroyki
import telega

# Насколько CPO может отличаться от целевого, чтобы считаться нормой.
KORIDOR_NORMY = 0.2


def _delenie(chislitel, znamenatel):
    return chislitel / znamenatel if znamenatel else None


def _dengi(summa):
    if summa is None:
        return "—"
    return f"{summa:,.0f} ₽".replace(",", " ")


def pribyl_kampanii(kampaniya, tovary, voronka):
    """Средняя прибыль с одного заказа по товарам кампании.

    Если артикулов несколько — взвешиваем по заказам из воронки продаж,
    а когда её нет, берём простое среднее. Возвращает (прибыль, сколько
    артикулов посчитано, сколько всего).
    """
    artikuly = kampaniya.get("artikuly") or []
    izvestnye = [a for a in artikuly if a in tovary]
    if not izvestnye:
        return None, 0, len(artikuly)

    vesa = []
    for artikul in izvestnye:
        zakazy = (voronka.get(artikul) or voronka.get(str(artikul)) or {}).get("zakazy", 0)
        vesa.append(max(float(zakazy or 0), 0.0))

    if sum(vesa) == 0:
        vesa = [1.0] * len(izvestnye)

    itogo = sum(tovary[a]["pribyl_s_zakaza"] * ves for a, ves in zip(izvestnye, vesa))
    return itogo / sum(vesa), len(izvestnye), len(artikuly)


def verdikt(kampaniya, limity, pribyl_s_zakaza):
    """Что происходит с кампанией. Возвращает (метка, объяснение словами)."""
    stat = kampaniya["statistika"]
    rashod = float(stat.get("rashod") or 0)
    zakazy = float(stat.get("zakazy") or 0)
    kliki = float(stat.get("kliki") or 0)
    cpo = _delenie(rashod, zakazy)

    if kampaniya.get("status") == 11:
        return "НА ПАУЗЕ", "кампания остановлена — показов нет, деньги не тратятся"

    if rashod == 0 and kliki == 0:
        return "МОЛЧИТ", "за период ни показов, ни расхода — проверь, идут ли показы вообще"

    if kliki < nastroyki.MINIMUM_KLIKOV:
        return "МАЛО ДАННЫХ", (
            f"всего {kliki:.0f} кликов — по такой выборке решение принимать рано, "
            f"жду {nastroyki.MINIMUM_KLIKOV}"
        )

    if zakazy == 0:
        if rashod >= limity["ALERT_SPEND"]:
            return "СЛИВ", (
                f"{_dengi(rashod)} потрачено, заказов ноль — это уже за порогом "
                f"ALERT_SPEND ({_dengi(limity['ALERT_SPEND'])})"
            )
        return "БЕЗ ЗАКАЗОВ", f"{_dengi(rashod)} потрачено, заказов пока нет"

    if pribyl_s_zakaza is not None and cpo is not None and cpo > pribyl_s_zakaza:
        return "УБЫТОК", (
            f"заказ приносит {_dengi(pribyl_s_zakaza)}, а реклама берёт {_dengi(cpo)} — "
            f"каждый заказ уносит {_dengi(cpo - pribyl_s_zakaza)}"
        )

    if cpo is not None and cpo > limity["TARGET_CPO"] * (1 + KORIDOR_NORMY):
        return "ДОРОГО", (
            f"заказ обходится в {_dengi(cpo)} при цели {_dengi(limity['TARGET_CPO'])}"
        )

    if cpo is not None and cpo < limity["TARGET_CPO"] * (1 - KORIDOR_NORMY) and zakazy >= 3:
        return "МАСШТАБИРУЕМ", (
            f"заказ стоит {_dengi(cpo)} при цели {_dengi(limity['TARGET_CPO'])} — "
            "есть запас, чтобы забрать больше показов"
        )

    return "НОРМА", f"заказ обходится в {_dengi(cpo)}, это в целевом коридоре"


def predlozhit_stavku(mesto, metka, cpo, limity):
    """Какую ставку предложить. Возвращает (новая ставка, причина) или (None, причина)."""
    tekushchaya = float(mesto.get("cpm") or 0)
    if tekushchaya <= 0:
        return None, "текущая ставка неизвестна — не трогаю"

    shag = limity["MAX_STEP_PERCENT"] / 100.0
    potolok_shaga = tekushchaya * (1 + shag)
    pol_shaga = tekushchaya * (1 - shag)

    if metka in ("НА ПАУЗЕ", "МАЛО ДАННЫХ", "МОЛЧИТ", "НОРМА"):
        return None, "менять нечего"

    if metka in ("СЛИВ", "БЕЗ ЗАКАЗОВ"):
        zhelaemaya = pol_shaga
        prichina = "снижаю на максимальный шаг вниз — деньги уходят без заказов"
    elif metka in ("ДОРОГО", "УБЫТОК"):
        if not cpo:
            return None, "нет CPO для расчёта"
        zhelaemaya = tekushchaya * (limity["TARGET_CPO"] / cpo)
        prichina = f"привожу CPO к целевым {_dengi(limity['TARGET_CPO'])}"
    elif metka == "МАСШТАБИРУЕМ":
        if not cpo:
            return None, "нет CPO для расчёта"
        zhelaemaya = tekushchaya * min(limity["TARGET_CPO"] / cpo, 1 + shag)
        prichina = "поднимаю, пока заказы дешевле цели"
    else:
        return None, "нет правила для этого случая"

    novaya = max(pol_shaga, min(potolok_shaga, zhelaemaya))
    upyorlas_v_shag = abs(novaya - zhelaemaya) > 1

    novaya = max(limity["MIN_CPM"], min(limity["MAX_CPM"], novaya))
    novaya = round(novaya)

    if novaya == round(tekushchaya):
        return None, "расчёт дал ту же ставку — менять незачем"

    if novaya >= limity["MAX_CPM"] and zhelaemaya > limity["MAX_CPM"]:
        prichina += f"; упёрлась в потолок MAX_CPM {_dengi(limity['MAX_CPM'])}"
    elif novaya <= limity["MIN_CPM"] and zhelaemaya < limity["MIN_CPM"]:
        prichina += f"; упёрлась в пол MIN_CPM {_dengi(limity['MIN_CPM'])}"
    elif upyorlas_v_shag:
        prichina += f"; ограничила шагом {limity['MAX_STEP_PERCENT']:.0f}% за раз"

    return novaya, prichina


def razobrat(srez, limity, tovary):
    """Считает по каждой кампании цифры, вердикт и предложение по ставке."""
    voronka = srez.get("voronka") or {}
    razbor = []

    for kampaniya in srez.get("kampanii", []):
        stat = kampaniya["statistika"]
        rashod = float(stat.get("rashod") or 0)
        zakazy = float(stat.get("zakazy") or 0)
        vyruchka = float(stat.get("vyruchka") or 0)
        kliki = float(stat.get("kliki") or 0)
        pokazy = float(stat.get("pokazy") or 0)

        cpo = _delenie(rashod, zakazy)
        drr = _delenie(rashod, vyruchka)
        pribyl, izvestno, vsego = pribyl_kampanii(kampaniya, tovary, voronka)
        metka, obyasnenie = verdikt(kampaniya, limity, pribyl)

        chistymi = (zakazy * pribyl - rashod) if pribyl is not None else None

        predlozheniya = []
        for mesto in kampaniya.get("stavki", []):
            novaya, prichina = predlozhit_stavku(mesto, metka, cpo, limity)
            predlozheniya.append({
                "gde": mesto.get("gde"),
                "tekushchaya": mesto.get("cpm"),
                "novaya": novaya,
                "prichina": prichina,
                "tip": mesto.get("tip"),
                "param": mesto.get("param"),
                "instrument": mesto.get("instrument"),
            })

        razbor.append({
            "id": kampaniya["id"],
            "nazvanie": kampaniya["nazvanie"],
            "tip_slovami": kampaniya.get("tip_slovami"),
            "status_slovami": kampaniya.get("status_slovami"),
            "metka": metka,
            "obyasnenie": obyasnenie,
            "rashod": rashod,
            "zakazy": zakazy,
            "vyruchka": vyruchka,
            "kliki": kliki,
            "pokazy": pokazy,
            "ctr": _delenie(kliki, pokazy),
            "cpo": cpo,
            "drr": drr * 100 if drr is not None else None,
            "pribyl_s_zakaza": pribyl,
            "chistymi": chistymi,
            "tovarov_izvestno": izvestno,
            "tovarov_vsego": vsego,
            "artikuly": kampaniya.get("artikuly", []),
            "predlozheniya": predlozheniya,
            "po_dnyam": kampaniya.get("po_dnyam", []),
        })

    poryadok = {"СЛИВ": 0, "УБЫТОК": 1, "ДОРОГО": 2, "БЕЗ ЗАКАЗОВ": 3,
                "МАСШТАБИРУЕМ": 4, "НОРМА": 5, "МАЛО ДАННЫХ": 6,
                "МОЛЧИТ": 7, "НА ПАУЗЕ": 8}
    razbor.sort(key=lambda k: (poryadok.get(k["metka"], 9), -k["rashod"]))
    return razbor


def otchet(srez, razbor, limity, tovary):
    """Собирает текст отчёта."""
    period = srez.get("period", {})
    stroki = [
        f"АНАЛИТИКА РЕКЛАМЫ WB · {period.get('ot', '?')} — {period.get('do', '?')}",
        "",
    ]

    rashod = sum(k["rashod"] for k in razbor)
    zakazy = sum(k["zakazy"] for k in razbor)
    vyruchka = sum(k["vyruchka"] for k in razbor)
    cpo = _delenie(rashod, zakazy)
    drr = _delenie(rashod, vyruchka)

    stroki.append("ИТОГО ПО КАБИНЕТУ")
    stroki.append(f"  Расход: {_dengi(rashod)}   Заказов: {zakazy:.0f}   "
                  f"Выручка: {_dengi(vyruchka)}")
    if cpo is not None:
        znak = "✓" if cpo <= limity["TARGET_CPO"] else "✗"
        stroki.append(f"  CPO: {_dengi(cpo)} при цели {_dengi(limity['TARGET_CPO'])} {znak}")
    if drr is not None:
        znak = "✓" if drr * 100 <= limity["TARGET_DRR"] else "✗"
        stroki.append(f"  ДРР: {drr * 100:.1f}% при цели {limity['TARGET_DRR']:.0f}% {znak}")

    s_pribylyu = [k for k in razbor if k["chistymi"] is not None]
    if s_pribylyu:
        chistymi = sum(k["chistymi"] for k in s_pribylyu)
        slovo = "заработали" if chistymi >= 0 else "потеряли"
        stroki.append(f"  Чистыми по кампаниям с известной себестоимостью: "
                      f"{slovo} {_dengi(abs(chistymi))}")
    elif not tovary:
        stroki.append("  Прибыль не считаю: нет reference/tovary.json — "
                      "вижу только расход, не деньги. Как заполнить: NASTROYKA.md, шаг 5.")

    balans = srez.get("balans") or {}
    if balans:
        stroki.append(f"  Баланс кабинета: счёт {_dengi(balans.get('balance'))}, "
                      f"бонусы {_dengi(balans.get('bonus'))}")

    stroki.append("")
    stroki.append("ПО КАМПАНИЯМ")
    if not razbor:
        stroki.append("  Активных кампаний не нашлось.")

    for kampaniya in razbor:
        stroki.append("")
        stroki.append(f"[{kampaniya['metka']}] {kampaniya['nazvanie']} "
                      f"(id {kampaniya['id']}, {kampaniya['tip_slovami']})")
        stroki.append(f"  {kampaniya['obyasnenie']}")

        chasti = [f"расход {_dengi(kampaniya['rashod'])}",
                  f"заказов {kampaniya['zakazy']:.0f}",
                  f"кликов {kampaniya['kliki']:.0f}"]
        if kampaniya["cpo"] is not None:
            chasti.append(f"CPO {_dengi(kampaniya['cpo'])}")
        if kampaniya["drr"] is not None:
            chasti.append(f"ДРР {kampaniya['drr']:.1f}%")
        stroki.append("  " + " · ".join(chasti))

        if kampaniya["pribyl_s_zakaza"] is not None:
            na_zakaz = kampaniya["pribyl_s_zakaza"] - (kampaniya["cpo"] or 0)
            if kampaniya["zakazy"]:
                slovo = "зарабатываешь" if na_zakaz >= 0 else "теряешь"
                stroki.append(f"  Деньгами: с каждого заказа этой кампании ты "
                              f"{slovo} {_dengi(abs(na_zakaz))}")
            if kampaniya["tovarov_izvestno"] < kampaniya["tovarov_vsego"]:
                stroki.append(f"  (себестоимость известна по {kampaniya['tovarov_izvestno']} "
                              f"из {kampaniya['tovarov_vsego']} артикулов)")

        for predlozhenie in kampaniya["predlozheniya"]:
            if predlozhenie["novaya"]:
                strelka = "↑" if predlozhenie["novaya"] > (predlozhenie["tekushchaya"] or 0) else "↓"
                stroki.append(f"  {strelka} ставка «{predlozhenie['gde']}»: "
                              f"{predlozhenie['tekushchaya']:.0f} → {predlozhenie['novaya']:.0f} ₽ "
                              f"— {predlozhenie['prichina']}")

    k_izmeneniyu = [(k, p) for k in razbor for p in k["predlozheniya"] if p["novaya"]]
    stroki.append("")
    if k_izmeneniyu:
        stroki.append(f"ПРЕДЛАГАЮ ИЗМЕНИТЬ СТАВОК: {len(k_izmeneniyu)}")
        stroki.append("  Применить: скажи «аналитика рекламы ВБ» без «только посмотри»")
        stroki.append("  Откатить потом: «верни как было»")
    else:
        stroki.append("МЕНЯТЬ НИЧЕГО НЕ НАДО — все кампании либо в норме, "
                      "либо данных пока мало.")

    if not limity.get("_zadan_polzovatelem"):
        stroki.append("")
        stroki.append("! Файла reference/limits.json нет — считала по значениям "
                      "по умолчанию (CPO 300 ₽, ДРР 15%, потолок ставки 500 ₽). "
                      "Свои цифры задай по NASTROYKA.md, шаг 3.")

    return "\n".join(stroki)


def itogi_nedeli(razbor, limity):
    """Короткий вердикт: что масштабируем, что закрываем."""
    stroki = ["ИТОГИ НЕДЕЛИ", ""]

    rasti = [k for k in razbor if k["metka"] == "МАСШТАБИРУЕМ"]
    zakryt = [k for k in razbor if k["metka"] in ("СЛИВ", "УБЫТОК")]
    chinit = [k for k in razbor if k["metka"] in ("ДОРОГО", "БЕЗ ЗАКАЗОВ")]
    zhdat = [k for k in razbor if k["metka"] in ("МАЛО ДАННЫХ", "МОЛЧИТ")]

    def blok(zagolovok, spisok, chto_delat):
        if not spisok:
            return
        stroki.append(f"{zagolovok} ({len(spisok)})")
        for kampaniya in spisok:
            stroki.append(f"  • {kampaniya['nazvanie']} (id {kampaniya['id']}) — "
                          f"{kampaniya['obyasnenie']}")
        stroki.append(f"  → {chto_delat}")
        stroki.append("")

    blok("МАСШТАБИРУЕМ", rasti,
         "поднимаем ставки шагами и следим за CPO — запас есть")
    blok("ОСТАНАВЛИВАЕМ ДЕНЬГИ", zakryt,
         "я снижаю ставку до пола; решение остановить кампанию — только твоё, "
         "сама я кампании не выключаю")
    blok("ЧИНИМ", chinit,
         "снижаю ставку и смотрю карточку: цена, фото, отзывы, наличие размеров")
    blok("ЖДЁМ ДАННЫХ", zhdat,
         "не трогаем, пока не накопится статистика")

    rashod = sum(k["rashod"] for k in razbor)
    zakazy = sum(k["zakazy"] for k in razbor)
    if zakazy:
        cpo = rashod / zakazy
        sravnenie = "укладываемся" if cpo <= limity["TARGET_CPO"] else "не укладываемся"
        stroki.append(f"За период: {_dengi(rashod)} расхода, {zakazy:.0f} заказов, "
                      f"CPO {_dengi(cpo)} — в цель {sravnenie}.")

    poteri = [k for k in razbor if k["chistymi"] is not None and k["chistymi"] < 0]
    if poteri:
        vsego = sum(k["chistymi"] for k in poteri)
        stroki.append(f"Убыточных кампаний: {len(poteri)}, суммарно "
                      f"минус {_dengi(abs(vsego))} за период.")

    return "\n".join(stroki)


def chto_esli(razbor, zapros, limity):
    """Прикидка результата смены ставки — без единого обращения к кабинету."""
    try:
        syroy_id, syraya_stavka = zapros.split(":")
        advert_id, novaya_stavka = int(syroy_id), float(syraya_stavka)
    except ValueError:
        return "Формат: --chto-esli ID_КАМПАНИИ:НОВАЯ_СТАВКА, например 123456:400"

    kampaniya = next((k for k in razbor if k["id"] == advert_id), None)
    if not kampaniya:
        return f"Кампании {advert_id} нет среди активных. Проверь id в отчёте."

    stroki = [f"ЧТО ЕСЛИ: {kampaniya['nazvanie']} (id {advert_id}), ставка → {novaya_stavka:.0f} ₽", ""]

    if novaya_stavka > limity["MAX_CPM"]:
        stroki.append(f"! {novaya_stavka:.0f} ₽ выше твоего потолка MAX_CPM "
                      f"({limity['MAX_CPM']:.0f} ₽) — поставить такую я откажусь. "
                      "Считаю прогноз просто для понимания.")
        stroki.append("")

    tekushchie = [p for p in kampaniya["predlozheniya"] if p["tekushchaya"]]
    if not tekushchie:
        return "\n".join(stroki + ["У кампании не видно текущей ставки — прогноз не построю."])

    tekushchaya = float(tekushchie[0]["tekushchaya"])
    if tekushchaya <= 0:
        return "\n".join(stroki + ["Текущая ставка нулевая — прогноз не построю."])

    otnoshenie = novaya_stavka / tekushchaya
    stroki.append(f"Сейчас: ставка {tekushchaya:.0f} ₽, расход {_dengi(kampaniya['rashod'])}, "
                  f"заказов {kampaniya['zakazy']:.0f}, "
                  f"CPO {_dengi(kampaniya['cpo'])}")
    stroki.append("")
    stroki.append("Прикидка на тот же период (CTR и конверсия неизменны):")

    for nazvanie, elastichnost in (("показов прибавится немного", 0.5),
                                   ("показов прибавится пропорционально", 1.0)):
        rost_pokazov = otnoshenie ** elastichnost
        novyy_rashod = kampaniya["rashod"] * rost_pokazov * otnoshenie
        novye_zakazy = kampaniya["zakazy"] * rost_pokazov
        stroki.append(f"  {nazvanie}: расход {_dengi(novyy_rashod)}, "
                      f"заказов {novye_zakazy:.1f}")

    novyy_cpo = (kampaniya["cpo"] or 0) * otnoshenie
    stroka = f"  CPO в обоих случаях: {_dengi(novyy_cpo)}"
    if novyy_cpo > limity["TARGET_CPO"]:
        stroka += f" — выше цели {_dengi(limity['TARGET_CPO'])}"
    stroki.append(stroka)
    stroki.append("  (CPO растёт вместе со ставкой и не зависит от того, "
                  "сколько показов удастся забрать — меняется только объём)")

    if kampaniya["pribyl_s_zakaza"] is not None:
        stroki.append("")
        stroki.append(f"  Заказ приносит {_dengi(kampaniya['pribyl_s_zakaza'])} — "
                      f"выше этой суммы CPO работает в минус.")

    stroki.append("")
    stroki.append("Это прикидка по прошлой статистике, а не обещание: аукцион WB "
                  "меняется каждый день, и реальный рост показов зависит от ставок "
                  "конкурентов. Проверять только небольшими шагами.")
    return "\n".join(stroki)


def main():
    razbor_args = argparse.ArgumentParser(description="Разбор рекламы WB")
    razbor_args.add_argument("--itogi-nedeli", action="store_true", help="короткий вердикт по неделе")
    razbor_args.add_argument("--chto-esli", help="прогноз: ID_КАМПАНИИ:НОВАЯ_СТАВКА")
    razbor_args.add_argument("--v-telegram", action="store_true", help="послать отчёт в Telegram")
    argumenty = razbor_args.parse_args()

    papka = nastroyki.podgotovit_papki()
    put = papka / "srez.json"
    if not put.exists():
        print("Нет data/srez.json — сначала запусти scripts/sobrat.py", file=sys.stderr)
        return 1

    try:
        limity = nastroyki.limity()
    except nastroyki.OshibkaNastroek as oshibka:
        print(f"\n{oshibka}\n", file=sys.stderr)
        return 1

    srez = json.loads(put.read_text(encoding="utf-8"))
    tovary = nastroyki.tovary()
    razobrannoe = razobrat(srez, limity, tovary)

    (papka / "rekomendacii.json").write_text(
        json.dumps({"period": srez.get("period"), "kampanii": razobrannoe},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if argumenty.chto_esli:
        tekst = chto_esli(razobrannoe, argumenty.chto_esli, limity)
    elif argumenty.itogi_nedeli:
        tekst = itogi_nedeli(razobrannoe, limity)
    else:
        tekst = otchet(srez, razobrannoe, limity, tovary)

    print(tekst)

    kuda = str(limity.get("REPORT_TO", "chat")).lower()
    if argumenty.v_telegram or kuda in ("telegram", "both"):
        poluchilos, chto_skazat = telega.otpravit(limity, tekst)
        print(f"\n{chto_skazat}", file=sys.stderr)
        if not poluchilos and kuda == "telegram":
            print("Отчёт выше остался в чате — доставку в Telegram нужно починить.",
                  file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
