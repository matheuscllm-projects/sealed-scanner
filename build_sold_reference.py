#!/usr/bin/env python3
"""
build_sold_reference.py — referência de VENDA REALIZADA (a que CLASSIFICA).

Decisão do operador (2026-11-16): "preço no TCGplayer não é referência de venda,
eBay vende muito barato". A margem bruta compara o preço da Liga com o que
compradores do eBay US EFETIVAMENTE pagaram pelo produto em INGLÊS. Este builder:

  1. (--collect, default ligado) abre a busca de VENDIDOS do eBay
     (`LH_Sold=1&LH_Complete=1`) para cada SKU no Chrome real (patchright, mesmo
     perfil persistente da Liga). O eBay EXIGE LOGIN para essa busca: se cair no
     signin, o builder espera o operador logar NA JANELA (até --login-wait s,
     uma vez; o perfil guarda a sessão). Sem login a coleta é pulada com aviso —
     nenhuma credencial passa por aqui. Cada venda parseada (título, preço, data,
     país, link) passa pelo gate de INGLÊS do matcher de vendidos (marcador de
     outro idioma = fora; ausência = inglês) e entra no store
     `data/history/ebay_sold.jsonl` (source_type `ebay_sold_page`, dedupe por
     item_id — recoletar não duplica).
  2. agrega as vendas por SKU (lib/sold_reference.py) e grava
     `data/sold_reference.json` (mediana 90d, n, min/max, última venda, link).

Nunca inventa preço: SKU sem nenhuma venda registrada fica FORA do arquivo; no
pipeline ele cai no menor BIN ativo (data/ebay_reference.json — fallback marcado,
nunca GREEN) ou em RED sem referência. Run degradado (sem Chrome, sem login)
ainda grava a agregação do que existe — e diz o que faltou.

Uso:
    python build_sold_reference.py                 # coleta (Chrome visível) + agrega
    python build_sold_reference.py --no-collect    # só agrega o que já está no store
    python build_sold_reference.py --limit-skus 3  # smoke da coleta
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from lib import sold_reference as SR  # noqa: E402
from lib.sold_importers import (  # noqa: E402
    append_to_store,
    detect_lot,
    match_records_to_skus,
    non_en_marker,
)

DEFAULT_OUTPUT = SR.DEFAULTS["file"]
DEFAULT_STORE = "data/history/ebay_sold.jsonl"
SOURCE_TYPE_PAGE = "ebay_sold_page"
EBAY_SOLD_SEARCH = "https://www.ebay.com/sch/i.html"

# ── Página de vendidos: URL, bloqueio, parse ──────────────────────────────
def sold_search_url(query: str, ipg: int = 60) -> str:
    """Busca de VENDIDOS/encerrados no eBay US, mais recentes primeiro."""
    return (f"{EBAY_SOLD_SEARCH}?_nkw={quote_plus(query)}&LH_Sold=1&LH_Complete=1"
            f"&_ipg={int(ipg)}&_sop=13&rt=nc")


_BLOCK_TITLE_TOKENS = ("entre ou cadastre", "sign in or register", "medida de seguran",
                       "security measure", "pardon our interruption", "verify yourself")


def is_blocked_page(title: str, url: str) -> bool:
    """Signin ou desafio (captcha) = página sem resultados — nunca parsear como
    'zero vendas'."""
    t = (title or "").lower()
    u = (url or "").lower()
    if "signin.ebay" in u or "/signin" in u or "splashui/captcha" in u or "/challenge" in u:
        return True
    return any(tok in t for tok in _BLOCK_TITLE_TOKENS)


# `country_from_location` mora em lib/sold_reference (o store do operador também
# precisa normalizar `Local`); re-exportado aqui p/ o parser e os testes.
country_from_location = SR.country_from_location
DEFAULT_MAX_REFERENCE_AGE_DAYS = SR.DEFAULT_MAX_REFERENCE_AGE_DAYS


_MONTHS_EN = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTHS_PT = {m: i for i, m in enumerate(
    ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"], 1)}
_DATE_EN_RE = re.compile(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b")
_DATE_PT_RE = re.compile(r"\b(\d{1,2})\s+(?:de\s+)?([A-Za-z]{3})[a-z]*\.?\s+(?:de\s+)?(\d{4})\b")


def parse_sold_date(text: str) -> str | None:
    """'Sold  Oct 12, 2026' / 'Vendido em 5 de out. de 2026' -> ISO; None se não há data."""
    t = text or ""
    m = _DATE_EN_RE.search(t)
    if m and m.group(1).lower() in _MONTHS_EN:
        mon = _MONTHS_EN[m.group(1).lower()]
        try:
            return date(int(m.group(3)), mon, int(m.group(2))).isoformat()
        except ValueError:
            return None
    m = _DATE_PT_RE.search(t)
    if m:
        key = m.group(2).lower()
        mon = _MONTHS_PT.get(key) or _MONTHS_EN.get(key)
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(1))).isoformat()
            except ValueError:
                return None
    return None


# Só dólar americano: "US $", "US$", "USD" ou "$" SEM letra/moeda antes
# ("R$ 209", "C $25", "AU $30" = outra moeda -> None, nunca convertido como USD).
_USD_RE = re.compile(
    r"(?:(?<![A-Za-z])US\s?\$|(?<![A-Za-z])USD|(?<![A-Za-z])(?<![A-Za-z]\s)\$)\s*([\d][\d.,]*)")


def parse_usd_text(text: str) -> float | None:
    """'$30.90' / 'US$ 209,00' / '$1,234.56' / 'USD 1.234,56' / '$1.234' (milhar) /
    faixa -> menor valor. Outra moeda ou sem cifra -> None (nunca inventar)."""
    m = _USD_RE.search(text or "")
    if not m:
        return None
    raw = m.group(1).rstrip(".,")
    if "," not in raw and raw.count(".") == 1 and len(raw.split(".")[1]) == 3:
        raw = raw.replace(".", "")          # "1.234" = milhar (decimal tem 2 casas)
    if "," in raw and "." in raw:
        # o separador que vem por ÚLTIMO é o decimal
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        # "209,00" -> decimal BR; "1,234" -> milhar US
        head, _, tail = raw.rpartition(",")
        raw = f"{head}.{tail}" if len(tail) == 2 else raw.replace(",", "")
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v > 0 else None


_ITEM_ID_RE = re.compile(r"/itm/(?:[^/?#]+/)?(\d+)")
_PLACEHOLDER_TITLES = ("shop on ebay", "compre no ebay")


def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el is not None else ""


def parse_sold_search_html(html: str) -> list[dict]:
    """Itens da página de vendidos nos dois layouts do eBay (`li.s-item` clássico,
    `li.s-card` novo). Item sem preço OU sem data de venda fica fora (não é prova
    de venda). Retorna dicts: item_id, title, price_usd, sold_date, link, location,
    bids, raw_caption."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for li in soup.select("li.s-item, li.s-card, div.s-item"):
        classes = " ".join(li.get("class") or [])
        if "placeholder" in classes:
            continue
        a = li.select_one('a.s-item__link[href*="/itm/"], a.su-link[href*="/itm/"], a[href*="/itm/"]')
        href = (a.get("href") or "") if a else ""
        m = _ITEM_ID_RE.search(href)
        if not m:
            continue
        item_id = m.group(1)
        title = _text(li.select_one(".s-item__title, .s-card__title"))
        title = re.sub(r"^(?:new listing|novo anúncio)\s*", "", title, flags=re.I).strip()
        if not title or title.lower() in _PLACEHOLDER_TITLES:
            continue
        price = parse_usd_text(_text(li.select_one(".s-item__price, .s-card__price")))
        caption = _text(li.select_one(".s-item__caption, .s-card__caption"))
        if not caption:
            # fallback: qualquer span com "Sold"/"Vendido"
            for sp in li.select("span"):
                tx = _text(sp)
                if re.search(r"\b(sold|vendido)\b", tx, re.I):
                    caption = tx
                    break
        sold_date = parse_sold_date(caption)
        if price is None or sold_date is None or item_id in seen:
            continue
        seen.add(item_id)
        loc_el = li.select_one(".s-item__location, .s-item__itemLocation")
        loc_txt = _text(loc_el)
        if not loc_txt:
            for sp in li.select("span"):
                tx = _text(sp)
                if re.match(r"^(from|located in|de|localizado em)\s+\S", tx, re.I) and len(tx) < 40:
                    loc_txt = tx
                    break
        bids = None
        bids_txt = _text(li.select_one(".s-item__bids, .s-item__bidCount"))
        mb = re.search(r"(\d+)\s*(?:bids?|lances?)", bids_txt or _text(li), re.I)
        if mb:
            bids = int(mb.group(1))
        out.append({
            "item_id": item_id,
            "title": title,
            "price_usd": price,
            "sold_date": sold_date,
            "link": f"https://www.ebay.com/itm/{item_id}" if href else "",
            "location": country_from_location(loc_txt),
            "bids": bids,
            "raw_caption": caption,
        })
    return out


# ── Coleta no Chrome (patchright, perfil da Liga) ────────────────────────
class SoldPageBrowser:
    """Página eBay no Chrome real (mesmo perfil persistente da Liga: o login no
    eBay feito uma vez fica guardado). `fetch(url)` -> (title, final_url, html)."""

    def __init__(self, headless: bool = False, profile_dir: str | None = None):
        from liga_adapter import _LocalChromeFetcher
        self._f = _LocalChromeFetcher(headless=headless, profile_dir=profile_dir)

    def fetch(self, url: str, timeout_s: int = 60) -> tuple[str, str, str]:
        self._f._ensure()
        page = self._f._page
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001 — networkidle é best-effort
            pass
        return page.title(), page.url, page.content()

    def wait_for_login(self, max_wait_s: int, poll_s: float = 3.0) -> bool:
        """Espera o operador logar na janela (título/URL deixam de ser signin)."""
        page = self._f._page
        deadline = time.time() + max_wait_s
        while time.time() < deadline:
            time.sleep(poll_s)
            try:
                if not is_blocked_page(page.title(), page.url):
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    def close(self):
        self._f.close()


def sku_sold_query(sku) -> str:
    """Mesma query do build_ebay_reference ('pokemon <nome sem (English)>')."""
    from build_ebay_reference import sku_query
    return sku_query(sku, "pokemon")


def collect_sold_pages(skus: list, browser, *, ipg: int = 60, delay_s: float = 1.5,
                       login_wait_s: int = 180, log=print) -> tuple[list[dict], dict]:
    """Abre a busca de vendidos de cada SKU, parseia, gateia por idioma e casa com o
    registry. Retorna (registros prontos p/ o store, stats). Página bloqueada
    (signin/captcha): espera login UMA vez; se não liberar, aborta a coleta
    (stats['blocked']=True) — o que já foi coletado é mantido."""
    stats = {"skus": 0, "pages": 0, "items": 0, "en": 0, "blocked": False,
             "errors": 0, "login_waited": False, "zero_item_pages": 0}
    records: list[dict] = []
    seen: set[str] = set()
    for sku in skus:
        stats["skus"] += 1
        url = sold_search_url(sku_sold_query(sku), ipg=ipg)
        try:
            title, final_url, html = browser.fetch(url)
        except Exception as exc:  # noqa: BLE001 — rede/timeout: segue p/ o próximo
            stats["errors"] += 1
            log(f"    [sold-page] erro em {sku.id}: {type(exc).__name__}: {exc}")
            continue
        if is_blocked_page(title, final_url):
            if stats["login_waited"]:
                stats["blocked"] = True
                log("    [sold-page] eBay ainda bloqueado após o login — coleta interrompida.")
                break
            stats["login_waited"] = True
            log(f"\n  >>> O eBay pede LOGIN para ver vendidos. Faça login NA JANELA do Chrome "
                f"(até {login_wait_s}s). Nenhuma credencial passa pelo scanner. <<<\n")
            if not browser.wait_for_login(login_wait_s):
                stats["blocked"] = True
                log("    [sold-page] sem login — coleta de vendidos PULADA neste run.")
                break
            try:
                title, final_url, html = browser.fetch(url)
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                log(f"    [sold-page] erro em {sku.id}: {type(exc).__name__}: {exc}")
                continue
            if is_blocked_page(title, final_url):
                stats["blocked"] = True
                break
        stats["pages"] += 1
        items = parse_sold_search_html(html)
        stats["items"] += len(items)
        if not items:
            # pode ser "sem vendas" de verdade OU seletor quebrado (eBay mudou o
            # layout): fica visível no log, nunca vira "zero vendas" no arquivo.
            stats["zero_item_pages"] += 1
            log(f"  [sold-page] {sku.id}: 0 vendidos parseados — sem vendas OU layout da "
                "página mudou (conferir seletores se isso valer p/ todos os SKUs)")
        kept = 0
        for it in items:
            if it["item_id"] in seen:
                continue
            if non_en_marker(it["title"]) != "none":
                continue              # a busca também traz japonês/português/etc.
            is_lot, units = detect_lot(it["title"])
            rec = dict(it)
            rec.update({"is_lot": is_lot, "units_in_lot": units, "query_sku": sku.id})
            records.append(rec)
            seen.add(it["item_id"])
            kept += 1
        stats["en"] += kept
        log(f"  [sold-page] {sku.id}: {len(items)} vendidos na página · {kept} EN")
        time.sleep(delay_s)
    return records, stats


# ── Agregação ────────────────────────────────────────────────────────────
def load_skus(registry_path: Path):
    import sealed_arbitrage_scanner as S
    return S.build_registry(S.load_yaml(registry_path, registry_path.name))


def store_path_for(config: dict, root: Path) -> Path:
    rel = SR.sold_ref_cfg(config).get("store_file") or DEFAULT_STORE
    return Path(rel) if Path(rel).is_absolute() else root / rel


def build(config: dict, *, root: Path, now=None, log=print) -> dict:
    """Agrega o store de vendidos -> dict da referência de venda."""
    c = SR.sold_ref_cfg(config)
    store_path = store_path_for(config, root)
    recs = SR.load_store_records(store_path) if store_path.exists() else []
    if not store_path.exists():
        log(f"  [sold-ref] aviso: store de vendidos ausente ({store_path}) — referência vazia.")
    sales = SR.realized_sales_from_store(recs, now, int(c["window_days"]))
    ref = SR.aggregate_sold_reference(sales, now, c)
    if ref.get("skipped_no_url"):
        log(f"  [sold-ref] {len(ref['skipped_no_url'])} SKU(s) com venda mas sem NENHUM link na fonte "
            f"— fora da referência (entrega exige link): {', '.join(ref['skipped_no_url'])}")
    ref["_comment"] = ("Gerado por build_sold_reference.py. Venda REALIZADA (produto em inglês) no eBay US "
                       "por SKU: mediana das vendas na janela. É a referência que CLASSIFICA o perfil "
                       "Pokémon; o menor BIN ativo (ebay_reference.json) é fallback marcado e o "
                       "TCGplayer é informativo. NÃO editar à mão.")
    ref["store_records"] = len(recs)
    ref["store_path"] = str(store_path)
    return ref


def write_reference(path: Path, ref: dict) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ref, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Referência de VENDA realizada (eBay US, inglês).")
    ap.add_argument("--game", default="pokemon", choices=["pokemon", "onepiece"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--registry", default=None)
    ap.add_argument("--output", default=None, help=f"default: config references.sold_file ou {DEFAULT_OUTPUT}")
    ap.add_argument("--collect", action=argparse.BooleanOptionalAction, default=None,
                    help="coletar a página de vendidos do eBay no Chrome visível antes de agregar "
                         "(default: config sold_reference.collect_sold_page, ligado)")
    ap.add_argument("--login-wait", type=int, default=None, help="segundos esperando o login (default config/180)")
    ap.add_argument("--limit-skus", type=int, default=0, help="só os N primeiros SKUs (smoke)")
    ap.add_argument("--ipg", type=int, default=60)
    ap.add_argument("--delay", type=float, default=1.5)
    args = ap.parse_args(argv)

    import sealed_arbitrage_scanner as S
    profile = S.GAME_PROFILES[args.game]
    config_path = Path(args.config) if args.config else SCRIPT_DIR / profile["config"]
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    c = SR.sold_ref_cfg(config)
    refs = config.get("references") or {}
    out_path = Path(args.output) if args.output else SCRIPT_DIR / (refs.get("sold_file") or DEFAULT_OUTPUT)
    registry_path = Path(args.registry) if args.registry else SCRIPT_DIR / profile["registry"]
    collect = c.get("collect_sold_page", True) if args.collect is None else args.collect
    login_wait = args.login_wait if args.login_wait is not None else int(c.get("login_wait_s", 180))

    print("=" * 64)
    print("  REFERÊNCIA DE VENDA REALIZADA (eBay US, inglês) — build")
    print("=" * 64)

    if collect:
        skus = load_skus(registry_path)
        if args.limit_skus:
            skus = skus[: args.limit_skus]
        store_path = store_path_for(config, SCRIPT_DIR)
        browser = None
        try:
            browser = SoldPageBrowser(headless=False)
            records, stats = collect_sold_pages(skus, browser, ipg=args.ipg, delay_s=args.delay,
                                                login_wait_s=login_wait)
            match_records_to_skus(records, skus)
            if records:
                st = append_to_store(records, store_path, SOURCE_TYPE_PAGE, SOURCE_TYPE_PAGE)
                print(f"  [sold-page] store: {st.summary()}")
            print(f"  [sold-page] páginas {stats['pages']}/{stats['skus']} · itens {stats['items']} · "
                  f"EN {stats['en']} · erros {stats['errors']}"
                  + (" · BLOQUEADO (sem login) — coleta parcial/pulada" if stats["blocked"] else ""))
        except Exception as exc:  # noqa: BLE001 — a agregação abaixo ainda roda
            print(f"  [sold-page] aviso: coleta falhou ({type(exc).__name__}: {exc}) — agregando o que existe.")
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
    else:
        print("  coleta da página de vendidos DESLIGADA (--no-collect) — só agregação.")

    ref = build(config, root=SCRIPT_DIR, now=datetime.now(timezone.utc))
    write_reference(out_path, ref)
    n = len(ref["entries"])
    thin = sum(1 for e in ref["entries"].values() if e.get("thin"))
    fora = sum(1 for e in ref["entries"].values() if e.get("location_scope") == "non_us_fallback")
    dropped = sum(int(e.get("n_non_us_dropped") or 0) for e in ref["entries"].values())
    so_fora = len(ref.get("skipped_non_us_only") or [])
    print("-" * 64)
    print(f"  Arquivo : {out_path}")
    print(f"  SKUs com venda realizada: {n} · amostra fina (n<{ref['min_sales_for_green']}): {thin} · "
          f"janela {ref['window_days']}d · store {ref['store_records']} registros")
    if ref.get("require_us", True):
        print(f"  Só venda nos EUA conta: {dropped} venda(s) fora dos EUA descartada(s) "
              f"· {so_fora} SKU(s) SÓ com venda fora dos EUA = sem venda (vão p/ o BIN ativo, fallback)")
    else:
        print(f"  Modo legado (require_us: false): {fora} SKU(s) com referência de fallback fora dos EUA (nunca GREEN)")
    print("  Lembrete: é VENDA REALIZADA (mediana); o menor BIN ativo é informativo/fallback.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    try:
        from lib.console import harden_stdout
        harden_stdout()
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
