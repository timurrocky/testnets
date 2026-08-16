"""Клиент к API Wildberries: реклама + аналитика.

ВСЕ адреса методов собраны в ENDPOINTS — если WB обновил API и что-то отвалилось
с 400/404, чинить нужно здесь, в одном месте. Смотри reference/api-spravochnik.md.
"""

import json
import sys
import time
import urllib.error
import urllib.request

BASE_ADVERT = "https://advert-api.wildberries.ru"
BASE_STATS = "https://seller-analytics-api.wildberries.ru"

# Правь тут, если WB сменил версию метода.
ENDPOINTS = {
    "spisok_kampaniy": ("GET", BASE_ADVERT + "/adv/v1/promotion/count"),
    "detali_kampaniy": ("POST", BASE_ADVERT + "/adv/v1/promotion/adverts"),
    "statistika": ("POST", BASE_ADVERT + "/adv/v2/fullstats"),
    "postavit_stavku": ("POST", BASE_ADVERT + "/adv/v0/cpm"),
    "balans": ("GET", BASE_ADVERT + "/adv/v1/balance"),
    "voronka": ("POST", BASE_STATS + "/api/v2/nm-report/detail"),
}

# Сколько ждать между запросами. У fullstats лимит 1 запрос в минуту,
# поэтому статистику по всем кампаниям берём ОДНИМ запросом (метод принимает массив).
PAUZA_MEZHDU_ZAPROSAMI = 0.25
PAUZA_POSLE_429 = 60

TIPY_KAMPANIY = {
    4: "каталог",
    5: "карточка товара",
    6: "поиск",
    7: "главная страница",
    8: "автоматическая",
    9: "аукцион (АРК)",
}

STATUSY = {
    -1: "удаляется",
    4: "готова к запуску",
    7: "завершена",
    8: "отказался",
    9: "идут показы",
    11: "на паузе",
}

AKTIVNYE_STATUSY = (9, 11)


class WBError(Exception):
    """Ошибка обращения к API с понятным человеку объяснением."""

    def __init__(self, soobshchenie, kod=None, telo=None):
        super().__init__(soobshchenie)
        self.kod = kod
        self.telo = telo


def _obyasnit_oshibku(kod, telo, imya_metoda):
    if kod == 401:
        return WBError(
            "401 — токен не подошёл. Проверь: он создан под владельцем аккаунта, "
            "в нём отмечены категории «Продвижение» и «Аналитика», и он не истёк. "
            "Лечится пересозданием токена в кабинете WB.",
            kod, telo,
        )
    if kod == 403:
        return WBError(
            "403 — токен есть, но у него нет прав на этот метод. "
            "Скорее всего не отмечена категория «Продвижение» или «Аналитика».",
            kod, telo,
        )
    if kod in (400, 404):
        return WBError(
            f"{kod} на методе «{imya_metoda}» — обычно значит, что WB обновил API. "
            "Скажи агенту: «почини вызов по документации dev.wildberries.ru» — "
            "адреса методов лежат в scripts/wb_api.py, в словаре ENDPOINTS.",
            kod, telo,
        )
    return WBError(f"{kod} от WB на методе «{imya_metoda}»: {telo[:300]}", kod, telo)


class WBClient:
    def __init__(self, token, token_stats=None, tikho=False):
        self.token = token
        self.token_stats = token_stats or token
        self.tikho = tikho
        self._posledniy_zapros = 0.0

    def _log(self, tekst):
        if not self.tikho:
            print(tekst, file=sys.stderr)

    def _pauza(self):
        proshlo = time.time() - self._posledniy_zapros
        if proshlo < PAUZA_MEZHDU_ZAPROSAMI:
            time.sleep(PAUZA_MEZHDU_ZAPROSAMI - proshlo)

    def zapros(self, imya_metoda, telo=None, popytok=3):
        """Один вызов API. Сам ждёт при 429 и повторяет."""
        if imya_metoda not in ENDPOINTS:
            raise WBError(f"Неизвестный метод «{imya_metoda}» — проверь ENDPOINTS.")
        metod, url = ENDPOINTS[imya_metoda]
        token = self.token_stats if imya_metoda == "voronka" else self.token

        dannye = None
        if telo is not None:
            dannye = json.dumps(telo, ensure_ascii=False).encode("utf-8")

        for popytka in range(1, popytok + 1):
            self._pauza()
            zapros = urllib.request.Request(url, data=dannye, method=metod)
            zapros.add_header("Authorization", token)
            zapros.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(zapros, timeout=120) as otvet:
                    self._posledniy_zapros = time.time()
                    syroy = otvet.read().decode("utf-8").strip()
                    if not syroy:
                        return None
                    return json.loads(syroy)
            except urllib.error.HTTPError as oshibka:
                self._posledniy_zapros = time.time()
                telo_otveta = oshibka.read().decode("utf-8", "replace")
                if oshibka.code == 429 and popytka < popytok:
                    self._log(f"  429 — лимит запросов, жду {PAUZA_POSLE_429} сек...")
                    time.sleep(PAUZA_POSLE_429)
                    continue
                if oshibka.code >= 500 and popytka < popytok:
                    self._log(f"  {oshibka.code} на стороне WB, повтор через 5 сек...")
                    time.sleep(5)
                    continue
                raise _obyasnit_oshibku(oshibka.code, telo_otveta, imya_metoda) from None
            except urllib.error.URLError as oshibka:
                if popytka < popytok:
                    self._log(f"  Сеть недоступна ({oshibka.reason}), повтор через 5 сек...")
                    time.sleep(5)
                    continue
                raise WBError(
                    f"Не смогла достучаться до {url}: {oshibka.reason}. "
                    "Если работаешь в облачной задаче Cowork — переключи её в режим "
                    "«На вашем компьютере», из облака серверы WB закрыты."
                ) from None
        raise WBError(f"Метод «{imya_metoda}» не ответил за {popytok} попыток.")

    # ---------- кампании ----------

    def id_kampaniy(self, tolko_aktivnye=True):
        """Список ID кампаний. По умолчанию — только те, что идут или на паузе."""
        otvet = self.zapros("spisok_kampaniy") or {}
        naydeno = []
        for gruppa in otvet.get("adverts") or []:
            status = gruppa.get("status")
            if tolko_aktivnye and status not in AKTIVNYE_STATUSY:
                continue
            for kampaniya in gruppa.get("advert_list") or []:
                if kampaniya.get("advertId"):
                    naydeno.append(kampaniya["advertId"])
        return sorted(set(naydeno))

    def detali_kampaniy(self, ids):
        """Подробности кампаний. Метод принимает не больше 50 ID за раз."""
        sobrano = []
        for nachalo in range(0, len(ids), 50):
            kusok = ids[nachalo:nachalo + 50]
            otvet = self.zapros("detali_kampaniy", telo=kusok)
            if isinstance(otvet, list):
                sobrano.extend(otvet)
        return sobrano

    def statistika(self, ids, data_ot, data_do):
        """Статистика за период. Все кампании — одним запросом (лимит 1 раз в минуту)."""
        if not ids:
            return []
        telo = [
            {"id": advert_id, "interval": {"begin": data_ot, "end": data_do}}
            for advert_id in ids
        ]
        otvet = self.zapros("statistika", telo=telo)
        return otvet if isinstance(otvet, list) else []

    def balans(self):
        try:
            return self.zapros("balans") or {}
        except WBError as oshibka:
            self._log(f"  Баланс не получен: {oshibka}")
            return {}

    def postavit_stavku(self, advert_id, tip, cpm, param, instrument=None):
        """Ставит новую ставку. Проверок лимитов тут НЕТ — они в scripts/stavki.py."""
        telo = {"advertId": int(advert_id), "type": int(tip), "cpm": int(cpm), "param": int(param)}
        if instrument is not None:
            telo["instrument"] = int(instrument)
        self.zapros("postavit_stavku", telo=telo)
        return True

    # ---------- аналитика продаж ----------

    def voronka(self, nm_ids, data_ot, data_do):
        """Воронка продаж по артикулам: показы карточки, корзины, заказы, выкупы."""
        if not nm_ids:
            return {}
        telo = {
            "nmIDs": [int(x) for x in nm_ids][:20],
            "period": {"begin": data_ot, "end": data_do},
            "page": 1,
        }
        try:
            otvet = self.zapros("voronka", telo=telo) or {}
        except WBError as oshibka:
            self._log(f"  Воронка продаж недоступна: {oshibka}")
            return {}
        po_artikulam = {}
        kartochki = ((otvet.get("data") or {}).get("cards")) or []
        for kartochka in kartochki:
            nm_id = kartochka.get("nmID")
            period = (kartochka.get("statistics") or {}).get("selectedPeriod") or {}
            if nm_id:
                po_artikulam[int(nm_id)] = {
                    "prosmotry_kartochki": period.get("openCardCount", 0),
                    "v_korzinu": period.get("addToCartCount", 0),
                    "zakazy": period.get("ordersCount", 0),
                    "vyruchka": period.get("ordersSumRub", 0),
                    "vykupy": period.get("buyoutsCount", 0),
                }
        return po_artikulam


def stavka_kampanii(kampaniya):
    """Достаёт текущую ставку и параметры для её смены из ответа WB.

    Возвращает список мест, где у кампании есть ставка. У автокампании оно одно,
    у АРК (тип 9) их два: поиск и каталог. Параметры берём из ответа, а не
    зашиваем в код — так переживём смену формата.
    """
    tip = kampaniya.get("type")
    mesta = []

    auto = kampaniya.get("autoParams") or {}
    if auto:
        subject = (auto.get("subject") or {}).get("id")
        if auto.get("cpm") is not None and subject:
            mesta.append({
                "gde": "автокампания",
                "cpm": auto.get("cpm"),
                "param": subject,
                "instrument": None,
                "nms": [n.get("nm") if isinstance(n, dict) else n for n in (auto.get("nms") or [])],
            })

    for blok in kampaniya.get("unitedParams") or []:
        subject = (blok.get("subject") or {}).get("id")
        nms = [n.get("nm") if isinstance(n, dict) else n for n in (blok.get("nms") or [])]
        if not subject:
            continue
        if blok.get("searchCPM") is not None:
            mesta.append({"gde": "поиск", "cpm": blok["searchCPM"], "param": subject,
                          "instrument": 6, "nms": nms})
        if blok.get("catalogCPM") is not None:
            mesta.append({"gde": "каталог", "cpm": blok["catalogCPM"], "param": subject,
                          "instrument": 4, "nms": nms})

    for blok in kampaniya.get("params") or []:
        subject = blok.get("subjectId") or blok.get("setId") or blok.get("menuId")
        if blok.get("price") is not None and subject:
            mesta.append({
                "gde": "размещение",
                "cpm": blok.get("price"),
                "param": subject,
                "instrument": None,
                "nms": [n.get("nm") if isinstance(n, dict) else n for n in (blok.get("nms") or [])],
            })

    for mesto in mesta:
        mesto["tip"] = tip
        mesto["nms"] = [int(n) for n in mesto["nms"] if n]
    return mesta


def artikuly_kampanii(kampaniya):
    """Все артикулы (nmID), которые крутятся в кампании."""
    naydeno = []
    for mesto in stavka_kampanii(kampaniya):
        naydeno.extend(mesto["nms"])
    return sorted(set(naydeno))
