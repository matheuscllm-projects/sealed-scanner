"""Referência de VENDA REALIZADA (eBay US, produto em inglês) — lib/sold_reference,
lib/sold_importers e build_sold_reference. Porte adaptado de ptbr-sealed-scanner
(gate de idioma invertido: marcador de OUTRO idioma rejeita; ausência = inglês)."""
from __future__ import annotations

import json
from datetime import date

import pytest

import build_sold_reference as BS
import sealed_arbitrage_scanner as S
from lib import sold_importers as SI
from lib import sold_reference as SR

NOW = date(2026, 11, 16)


def _sku(**kw):
    base = dict(id="ssp-booster-box-en", name="Surging Sparks Booster Box (English)",
                product_type="Booster Box", set_name="Surging Sparks", language="EN",
                set_terms=["surging sparks"], type_terms=["booster box"],
                exclude_terms=["etb", "bundle", "lote"], requires_terms=[])
    base.update(kw)
    return S.Sku(**base)


def _rec(item_id, usd, days_ago=5, **kw):
    d = date.fromordinal(NOW.toordinal() - days_ago).isoformat()
    r = {"item_id": item_id, "sku_id": "ssp-booster-box-en", "match_status": "ok",
         "price_usd": usd, "sold_date": d, "location": "US",
         "link": f"https://www.ebay.com/itm/{item_id}"}
    r.update(kw)
    return r


# ── gate de idioma (EN por ausência de marcador) ───────────────────────────
@pytest.mark.parametrize("title", [
    "Pokemon TCG Surging Sparks Booster Box Japanese",
    "Pokemon Surging Sparks Booster Box (Portuguese)",
    "Pokemon Surging Sparks Booster Box PT-BR",
    "Pokemon Surging Sparks Booster Box Korean Version",
    "Pokemon Surging Sparks Booster Box Chinese",
    "Pokemon Surging Sparks Booster Box Spanish",
    "Pokemon Surging Sparks Booster Box German",
    "Pokemon Surging Sparks Booster Box French",
    "Pokemon Surging Sparks Booster Box Italian",
    "Pokemon Surging Sparks Booster Box Thai",
    "Pokemon Surging Sparks Booster Box Indonesian",
    "Pokemon Surging Sparks Booster Box Japane",      # truncado
    "Pokemon Surging Sparks Booster Box PSA 10",
    "Pokemon Surging Sparks Booster Box Opened",
])
def test_gate_rejeita_outro_idioma_graded_e_aberto(title):
    assert SI.sold_title_passes_gate(title, _sku()) is False


def test_gate_aceita_ingles_sem_marcador_e_exige_set_e_tipo():
    assert SI.sold_title_passes_gate("Pokemon TCG Surging Sparks Booster Box Sealed", _sku())
    assert not SI.sold_title_passes_gate("Pokemon TCG Surging Sparks Elite Trainer Box", _sku())
    assert not SI.sold_title_passes_gate("Pokemon TCG Prismatic Booster Box", _sku())


def test_gate_lote_nao_veta_mas_exclude_de_tipo_sim():
    assert SI.sold_title_passes_gate("Lot of 2 Pokemon Surging Sparks Booster Box", _sku())
    assert not SI.sold_title_passes_gate("Pokemon Surging Sparks Booster Box Bundle", _sku())


def test_detect_lot():
    assert SI.detect_lot("Lot of 3 Pokemon Booster Box") == (True, 3)
    assert SI.detect_lot("2x Pokemon Booster Box") == (True, 2)
    assert SI.detect_lot("Pokemon Booster Box Case") == (True, None)
    assert SI.detect_lot("Pokemon Booster Bundle") == (False, None)


def test_match_records_ok_ambiguous_no_match():
    skus = [_sku(), _sku(id="ssp-booster-box-b", name="B")]
    recs = [{"title": "Surging Sparks Booster Box"}, {"title": "Totally Unrelated"}]
    SI.match_records_to_skus(recs, [skus[0]])
    assert recs[0]["match_status"] == "ok" and recs[1]["match_status"] == "no_match"
    recs = [{"title": "Surging Sparks Booster Box"}]
    SI.match_records_to_skus(recs, skus)
    assert recs[0]["match_status"] == "ambiguous" and recs[0]["sku_id"] is None


def test_append_to_store_dedupe(tmp_path):
    p = tmp_path / "s.jsonl"
    r = [{"item_id": "1", "match_status": "ok", "sku_id": "x"}]
    assert SI.append_to_store(r, p, "ebay_sold_page", "ebay_sold_page").imported == 1
    assert SI.append_to_store(r, p, "ebay_sold_page", "ebay_sold_page").duplicates == 1


# ── agregação: mediana, janela, lote, US-only, link ────────────────────────
def _agg(records, cfg=None):
    sales = SR.realized_sales_from_store(records, NOW, 90)
    return SR.aggregate_sold_reference(sales, NOW, cfg)


def test_mediana_n_e_link_da_venda_mais_recente():
    ref = _agg([_rec("100000001", 100, 30), _rec("100000002", 120, 2), _rec("100000003", 200, 10)])
    e = ref["entries"]["ssp-booster-box-en"]
    assert e["usd"] == 120.0 and e["n"] == 3 and not e["thin"]
    assert e["usd_min"] == 100 and e["usd_max"] == 200
    assert e["url"] == "https://www.ebay.com/itm/100000002"


def test_amostra_fina_e_janela():
    ref = _agg([_rec("100000001", 100, 5), _rec("100000002", 50, 91)])   # 91d fora da janela
    e = ref["entries"]["ssp-booster-box-en"]
    assert e["n"] == 1 and e["thin"] is True
    assert _agg([_rec("100000002", 50, 90)])["entries"]["ssp-booster-box-en"]["n"] == 1  # fronteira inclusiva


def test_lote_so_com_contagem_conhecida():
    ref = _agg([_rec("100000001", 300, is_lot=True, units_in_lot=3),
                _rec("100000002", 400, is_lot=True, units_in_lot=None),
                _rec("100000003", 110)])
    e = ref["entries"]["ssp-booster-box-en"]
    assert e["n"] == 2 and e["from_lot_n"] == 1
    assert sorted(s["usd"] for s in e["sales"]) == [100.0, 110.0]


def test_so_venda_us_conta_e_sem_preco_nao_entra():
    ref = _agg([_rec("100000001", 100, location="BR"), _rec("100000002", 0), _rec("100000003", None)])
    assert ref["entries"] == {} and ref["skipped_non_us_only"] == ["ssp-booster-box-en"]
    ref2 = _agg([_rec("100000001", 100, location="BR"), _rec("100000002", 150)])
    e = ref2["entries"]["ssp-booster-box-en"]
    assert e["usd"] == 150 and e["n_non_us_dropped"] == 1


def test_mesmo_item_em_duas_fontes_conta_uma_vez():
    ref = _agg([_rec("100000001", 100), _rec("v1|100000001|0", 100)])
    assert ref["entries"]["ssp-booster-box-en"]["n"] == 1


def test_venda_sem_link_fica_fora():
    ref = _agg([_rec("100000001", 100, link="")])
    assert ref["entries"] == {} and ref["skipped_no_url"] == ["ssp-booster-box-en"]


def test_sku_sem_venda_e_ausente_nunca_zero():
    assert _agg([])["entries"] == {}


def test_build_le_store_e_agrega(tmp_path):
    store = tmp_path / "data" / "history" / "ebay_sold.jsonl"
    store.parent.mkdir(parents=True)
    store.write_text("\n".join(json.dumps(r) for r in
                               [_rec("100000001", 100), _rec("100000002", 120)]) + "\n", encoding="utf-8")
    ref = BS.build({"sold_reference": {}}, root=tmp_path, now=NOW, log=lambda *_: None)
    assert ref["entries"]["ssp-booster-box-en"]["usd"] == 110.0
    assert ref["store_records"] == 2
    assert SR.DEFAULTS["file"] == "data/sold_reference.json"


# ── parser da página de vendidos (layouts s-item e s-card) ─────────────────
HTML = """
<ul><li class="s-item"><a class="s-item__link" href="https://www.ebay.com/itm/257292810881?hash=abc">
<div class="s-item__title">Pokemon Surging Sparks Booster Box Sealed</div></a>
<span class="s-item__price">$30.90</span><span class="s-item__location">from Brazil</span>
<div class="s-item__caption">Sold  Oct 12, 2026</div></li>
<li class="s-card"><div class="s-card__title"><span>Pokemon Surging Sparks ETB Japanese</span></div>
<span class="s-card__price">$95.00</span><div class="s-card__caption"><span>Sold Sep 30, 2026</span></div>
<a class="su-link" href="https://www.ebay.com/itm/257634909874">x</a></li></ul>
"""


def test_parser_e_coleta_filtram_outro_idioma():
    items = BS.parse_sold_search_html(HTML)
    assert [i["item_id"] for i in items] == ["257292810881", "257634909874"]
    assert items[0]["location"] == "BR" and items[0]["sold_date"] == "2026-10-12"

    class FakeBrowser:
        def fetch(self, url, timeout_s=60):
            return "t", "https://www.ebay.com/sch/i.html", HTML
    recs, stats = BS.collect_sold_pages([_sku()], FakeBrowser(), delay_s=0, log=lambda *_: None)
    assert [r["item_id"] for r in recs] == ["257292810881"]       # japonês fora
    assert stats["en"] == 1 and recs[0]["is_lot"] is False


def test_bloqueio_signin():
    assert BS.is_blocked_page("Sign in or register", "https://signin.ebay.com/x")
    assert not BS.is_blocked_page("pokemon | eBay", "https://www.ebay.com/sch/i.html")
