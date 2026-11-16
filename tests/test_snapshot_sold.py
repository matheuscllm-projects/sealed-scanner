"""Entrega (scripts/snapshot.py) no modo `ebay_sold` (perfil Pokémon, 2026-11-16).

Contrato duro do operador: TODA linha em TODO bucket tem link de OFERTA (Liga) E de
REFERÊNCIA — aqui `[oferta] · [eBay vendido] · [TCG]` (sem venda: o link eBay é o do
anúncio ativo de fallback; o TCG vem sempre do registry). A tabela acionável mostra
"Venda eBay (R$)" (mediana 90d, linkada à venda mais recente), "Nº vendas",
"Margem bruta %" vs a VENDA e o TCGplayer como coluna informativa. O modo 'tcg'
(One Piece) mantém o layout de sempre (travado em test_snapshot_links/grouping).
"""
import csv
import json
import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)
import snapshot  # noqa: E402
import sealed_arbitrage_scanner as S  # noqa: E402

SOLD_URL = "https://www.ebay.com/itm/111111111111"
BIN_URL = "https://www.ebay.com/itm/222222222222"


def _row(**kw):
    base = {
        "ID Anúncio": "LIGA-1", "SKU": "ssp-booster-box-en",
        "Produto (canônico)": "Surging Sparks Booster Box (English)", "Tipo": "Booster Box",
        "Coleção": "Surging Sparks", "Vendedor": "loja#1", "Fonte": "liga",
        "URL": "https://www.ligapokemon.com.br/?view=prod/view&pcode=1",
        "Preço BR (R$)": "500.00", "Qtd disponível": "4",
        "Preço US (US$)": "200.0", "Preço US (R$)": "1010.00", "Margem total %": "102.0",
        "Tipo de referência": "venda_realizada", "Nº vendas": "3", "Última venda": "2026-11-10",
        "Venda eBay URL": SOLD_URL, "eBay URL": BIN_URL,
        "Ref. TCG (US$)": "300.0", "Ref. TCG (R$)": "1515.00", "Margem vs TCG %": "203.0",
        "Risco principal": "", "_bucket": "real_opportunities", "_total": 102.0,
    }
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _tcg_ids(monkeypatch):
    monkeypatch.setitem(snapshot.TCG_PRODUCT_IDS, "ssp-booster-box-en", 565606)


def test_links_cell_sold_mode_oferta_ebay_vendido_tcg():
    cell = snapshot.links_cell(_row(), sold_mode=True)
    assert cell == (
        "[oferta](https://www.ligapokemon.com.br/?view=prod/view&pcode=1) · "
        f"[eBay vendido]({SOLD_URL}) · [TCG](https://www.tcgplayer.com/product/565606)"
    )


def test_links_cell_sold_mode_fallback_usa_anuncio_ativo_rotulado_como_tal():
    r = _row(**{"Tipo de referência": "bin_ativo_fallback", "Nº vendas": "0",
                "Venda eBay URL": BIN_URL})
    cell = snapshot.links_cell(r, sold_mode=True)
    assert f"[eBay ativo]({BIN_URL})" in cell and "eBay vendido" not in cell
    assert "[oferta](" in cell and "[TCG](" in cell


def test_links_cell_sold_mode_sem_nenhuma_referencia_ebay_mantem_oferta_e_tcg():
    cell = snapshot.links_cell(_row(**{"Venda eBay URL": "", "eBay URL": ""}), sold_mode=True)
    assert cell.startswith("[oferta](") and "[TCG](" in cell and "eBay" not in cell


def test_links_cell_modo_tcg_inalterado():
    r = {"URL": "https://liga/x", "SKU": "ssp-booster-box-en", "eBay URL": "https://www.ebay.com/itm/42"}
    assert snapshot.links_cell(r) == (
        "[oferta](https://liga/x) · [TCG](https://www.tcgplayer.com/product/565606) · "
        "[eBay](https://www.ebay.com/itm/42)")


def test_group_products_sold_traz_venda_n_e_tcg_informativo():
    g = snapshot.group_products([_row()], sold_mode=True)[0]
    assert g["sold_n"] == "3" and g["ref_kind"] == "venda_realizada"
    assert g["tcg_brl"] == 1010.0                       # Preço US (R$) = a VENDA no modo novo
    assert g["tcg_info_brl"] == 1515.0
    assert g["margem"] == pytest.approx(102.0)           # (1010-500)/500, vs VENDA
    assert g["tcg_info_margem"] == pytest.approx(203.0)  # (1515-500)/500, informativa
    assert g["sold_mode"] is True


def _write_scan(tmp_path, rows, meta):
    results = tmp_path / "results"
    d = results / "unified_20261116_120000"
    d.mkdir(parents=True)
    cols = list(rows[0].keys())
    cols = [c for c in cols if not c.startswith("_")]
    with (d / "unified_deals.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols + ["Confiança do deal"])
        w.writeheader()
        for r in rows:
            out = {k: r[k] for k in cols}
            out["Confiança do deal"] = {"real_opportunities": "GREEN", "review_required": "YELLOW",
                                        "rejected": "RED"}[r["_bucket"]]
            w.writerow(out)
    (d / "run_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return results


def _run(tmp_path, monkeypatch, rows, meta):
    results = _write_scan(tmp_path, rows, meta)
    monkeypatch.setattr(snapshot, "RESULTS", results)
    monkeypatch.setattr(snapshot, "OUT_DIR", tmp_path / "snaps")
    monkeypatch.setattr(sys, "argv", ["snapshot.py"])
    snapshot.main()
    return next((tmp_path / "snaps").glob("scan-*.md")).read_text(encoding="utf-8")


META_SOLD = {"classification_reference": "ebay_sold", "route_label": "r",
             "us_ref_captured_at": "2026-11-16T01:00:00Z"}


def test_entrega_sold_colunas_e_dois_links_em_toda_linha(tmp_path, monkeypatch):
    rows = [
        _row(),
        _row(**{"ID Anúncio": "LIGA-2", "SKU": "ssp-etb-en", "Produto (canônico)": "SSP ETB",
                "_bucket": "review_required", "Tipo de referência": "bin_ativo_fallback",
                "Nº vendas": "0", "Venda eBay URL": BIN_URL, "Risco principal": "Sem venda realizada"}),
        _row(**{"ID Anúncio": "LIGA-3", "SKU": "ssp-bundle-en", "Produto (canônico)": "SSP Bundle",
                "_bucket": "rejected", "Margem total %": "5.0", "_total": 5.0}),
    ]
    monkeypatch.setitem(snapshot.TCG_PRODUCT_IDS, "ssp-etb-en", 111)
    monkeypatch.setitem(snapshot.TCG_PRODUCT_IDS, "ssp-bundle-en", 222)
    text = _run(tmp_path, monkeypatch, rows, META_SOLD)
    for col in ("Venda eBay (R$)", "Nº vendas", "Margem bruta %", "Ref. TCG (R$)", "Margem vs TCG %"):
        assert col in text
    assert "Venda realizada" in text or "venda realizada" in text
    # toda linha de tabela com anúncio (acionável + ranking + escada) tem [oferta] E link de referência
    table_rows = [ln for ln in text.splitlines() if ln.startswith("| ") and "[oferta]" in ln]
    assert len(table_rows) >= 6
    for ln in table_rows:
        assert "[oferta](" in ln
        assert "[TCG](" in ln or "tcgplayer.com" in ln, ln
        assert "[eBay vendido](" in ln or "[eBay ativo](" in ln or "ebay.com/itm" in ln, ln
    # o preço da venda é linkado à venda mais recente
    assert f"[R$ 1010,00]({SOLD_URL})" in text
    # nada de coluna TCG 'classificando' no modo novo
    assert "Ref. eBay (R$)" not in text


def test_entrega_modo_tcg_continua_com_layout_antigo(tmp_path, monkeypatch):
    rows = [{k: v for k, v in _row().items()
             if k not in ("Tipo de referência", "Nº vendas", "Última venda", "Venda eBay URL",
                          "Ref. TCG (US$)", "Ref. TCG (R$)", "Margem vs TCG %")}]
    text = _run(tmp_path, monkeypatch, rows, {"classification_reference": "tcg"})
    assert "Ref. TCG (R$)" in text and "Ref. eBay (R$)" in text
    assert "Venda eBay (R$)" not in text and "Nº vendas" not in text
