"""classification_source: ebay_sold — a CLASSIFICAÇÃO usa a VENDA REALIZADA no eBay US
(mediana 90d), não o TCGplayer (operador 2026-11-16: "preço no TCGplayer não é
referência de venda, eBay vende muito barato").

Trava: mediana vence; amostra fina nunca GREEN; SKU sem venda cai no menor BIN
ativo MARCADO e nunca GREEN; sem nada = RED sem_referencia_us (nunca inventa
preço); freshness guard igual; invariantes (sem piso, threshold fração, margem
bruta pura, > 200% = RED margem_anomala); perfil One Piece segue 'tcg'.
"""
import json
import pathlib
import sys
from datetime import datetime, timezone

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sealed_arbitrage_scanner as S  # noqa: E402

SKU = "ssp-booster-box-en"
TITLE = "(ING) Caixa de Booster - Surging Sparks"


def _now_iso(days_ago=0):
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _config(**refs):
    return {
        "deal_criteria": {"min_total_margin_pct": 0.30, "review_above_margin_pct": 2.0,
                          "max_reference_age_days": 1},
        "filters": {"min_brazil_price_brl": 0},
        "currency": {"usd_brl": 5.0},
        "scope": {},
        "references": {"classification_source": "ebay_sold", "sold_file": "sold.json",
                       "ebay_file": "ebay.json", "us_file": "us.json", **refs},
        "sold_reference": {"window_days": 90, "min_sales_for_green": 2},
    }


def _registry():
    return S.build_registry({"skus": [{
        "id": SKU, "name": "Surging Sparks Booster Box (English)", "product_type": "Booster Box",
        "set": "Surging Sparks", "tcgplayer_product_id": 565606,
        "match": {"set_terms": ["surging sparks"], "type_terms": ["caixa de booster"],
                  "exclude_terms": []}}]})


def _write(tmp_path, sold=None, ebay=None, us=None):
    if sold is not None:
        (tmp_path / "sold.json").write_text(json.dumps(sold), encoding="utf-8")
    if ebay is not None:
        (tmp_path / "ebay.json").write_text(json.dumps(ebay), encoding="utf-8")
    if us is not None:
        (tmp_path / "us.json").write_text(json.dumps(us), encoding="utf-8")


def _sold(usd=200.0, n=3, captured=None, **kw):
    e = {"usd": usd, "n": n, "status": "ok", "thin": n < 2, "last_sold": "2026-11-10",
         "url": "https://www.ebay.com/itm/111111111111", "location_scope": "us", "seller_country": "US"}
    e.update(kw)
    return {"captured_at": captured or _now_iso(), "min_sales_for_green": 2, "entries": {SKU: e}}


def _bin(usd=190.0):
    return {"captured_at": _now_iso(), "entries": {SKU: {"usd": usd, "status": "ok",
            "url": "https://www.ebay.com/itm/222222222222"}}}


def _run(tmp_path, price=500.0, cfg=None):
    cfg = cfg or _config()
    ref_data, prices = S.load_classification_reference(cfg, tmp_path)
    row = S.ScanRow(listing_id="L1", title_br=TITLE, source="liga", seller="x",
                    url="https://www.ligapokemon.com.br/x", price_brl=price)
    S.classify(row, _registry(), prices, cfg)
    S.load_and_apply_sold(
        [row], ref_data, cfg, tmp_path)
    return row, ref_data


def test_source_e_labels():
    cfg = _config()
    assert S.classification_reference_source(cfg) == "ebay_sold"
    assert S.reference_rebuild_hint(cfg) == "build_sold_reference.py"
    assert "venda realizada" in S.classification_reference_label(cfg)
    assert S.classification_reference_source({"references": {"classification_source": "tcg"}}) == "tcg"
    assert S.classification_reference_source({"references": {"classification_source": "ebay"}}) == "ebay"


def test_venda_realizada_classifica_e_tcg_vira_informativo(tmp_path):
    _write(tmp_path, sold=_sold(200.0, 3), ebay=_bin(190.0), us={"captured_at": _now_iso(), "prices": {SKU: 300.0}})
    row, _ = _run(tmp_path, price=500.0)
    # margem bruta pura vs VENDA: (200*5 - 500)/500 = 100%
    assert row.us_price_usd == 200.0 and row.total_margin_pct == 1.0
    assert row.deal_confidence == "GREEN" and row.bucket == "real_opportunities"
    assert row.ref_kind == S.REF_KIND_SOLD and row.sold_n == 3
    assert row.sold_url == "https://www.ebay.com/itm/111111111111"
    # TCG só informativo: 300*5 = 1500 -> margem 200% informativa, não classifica
    assert row.tcg_ref_usd == 300.0 and row.tcg_ref_brl == 1500.0 and row.tcg_margin_pct == 2.0


def test_amostra_fina_nunca_green(tmp_path):
    _write(tmp_path, sold=_sold(200.0, 1), us={"captured_at": _now_iso(), "prices": {SKU: 300.0}})
    S_row, ref = _run(tmp_path, price=500.0)
    rows = [S_row]
    S.apply_sold_reference_guards(rows, ref, _config())
    assert S_row.deal_confidence == "YELLOW" and S_row.bucket == "review_required"
    assert "amostra" in S_row.main_risk.lower()
    assert S_row.total_margin_pct == 1.0          # margem oficial intocada


def test_sem_venda_cai_no_bin_marcado_e_nunca_green(tmp_path):
    _write(tmp_path, sold={"captured_at": _now_iso(), "entries": {}}, ebay=_bin(190.0))
    row, ref = _run(tmp_path, price=500.0)
    assert row.us_price_usd == 190.0 and row.ref_kind == S.REF_KIND_BIN_FALLBACK and row.sold_n == 0
    assert row.sold_url == "https://www.ebay.com/itm/222222222222"     # link do anúncio ativo
    S.apply_sold_reference_guards([row], ref, _config())
    assert row.deal_confidence == "YELLOW" and "pedida" in row.main_risk.lower()


def test_fallback_desligado_ou_sem_nada_e_red_sem_referencia(tmp_path):
    _write(tmp_path, sold={"captured_at": _now_iso(), "entries": {}}, ebay=_bin(190.0))
    cfg = _config()
    cfg["sold_reference"]["fallback_to_active_bin"] = False
    row, _ = _run(tmp_path, cfg=cfg)
    assert row.deal_confidence == "RED" and row.reject_reason == "sem_referencia_us"
    assert row.us_price_usd is None                                    # nunca inventa preço
    _write(tmp_path, sold={"captured_at": _now_iso(), "entries": {}}, ebay={"captured_at": _now_iso(), "entries": {}})
    row, _ = _run(tmp_path)
    assert row.reject_reason == "sem_referencia_us" and "venda" in row.main_risk.lower()


def test_arquivos_ausentes_red_honesto(tmp_path):
    row, ref = _run(tmp_path)
    assert row.reject_reason == "sem_referencia_us" and ref["_missing_sold"] is True


def test_freshness_guard_vale_igual(tmp_path):
    _write(tmp_path, sold=_sold(200.0, 3, captured=_now_iso(3)))
    row, ref = _run(tmp_path, price=500.0)
    assert row.deal_confidence == "GREEN"
    stale = S.apply_freshness_downgrade([row], ref, _config())
    assert stale is not None and row.deal_confidence == "YELLOW"


def test_margem_anomala_acima_de_200_vira_red(tmp_path):
    _write(tmp_path, sold=_sold(200.0, 5))
    row, _ = _run(tmp_path, price=100.0)            # (1000-100)/100 = 900%
    assert row.deal_confidence == "RED" and row.reject_reason == "margem_anomala"


def test_sem_piso_de_preco_e_threshold_fracao(tmp_path):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["filters"]["min_brazil_price_brl"] == 0
    assert cfg["deal_criteria"]["min_total_margin_pct"] == 0.30
    assert cfg["deal_criteria"]["review_above_margin_pct"] == 2.0


def test_config_pokemon_usa_ebay_sold_e_onepiece_segue_tcg():
    pk = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    op = yaml.safe_load((ROOT / "config_onepiece.yaml").read_text(encoding="utf-8"))
    assert pk["references"]["classification_source"] == "ebay_sold"
    assert pk["sold_reference"]["window_days"] == 90 and pk["sold_reference"]["min_sales_for_green"] == 2
    assert op["references"]["classification_source"] == "tcg"
    assert "sold_reference" not in op
    assert S.csv_columns(op) == S.CSV_COLUMNS          # perfil OP: CSV idêntico
    assert len(S.csv_columns(pk)) > len(S.CSV_COLUMNS)


def test_modo_tcg_nao_preenche_colunas_de_venda(tmp_path):
    cfg = {"references": {"classification_source": "tcg"}, "currency": {"usd_brl": 5.0}}
    row = S.ScanRow(listing_id="L", title_br="t", source="liga", seller="", url="u", price_brl=10, sku_id=SKU)
    assert S.load_and_apply_sold([row], {}, cfg, tmp_path) is None
    assert row.ref_kind == "" and row.tcg_ref_usd is None


def test_csv_escreve_colunas_de_venda(tmp_path):
    _write(tmp_path, sold=_sold(200.0, 3), us={"captured_at": _now_iso(), "prices": {SKU: 300.0}})
    row, _ = _run(tmp_path)
    p = tmp_path / "o.csv"
    S.write_csv([row], p, S.csv_columns(_config()))
    head = p.read_text(encoding="utf-8").splitlines()[0]
    for col in ("Venda eBay URL", "Nº vendas", "Ref. TCG (US$)", "Margem vs TCG %", "Tipo de referência"):
        assert col in head
