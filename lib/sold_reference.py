"""lib/sold_reference.py — referência de VENDA REALIZADA (a que CLASSIFICA).

Decisão do operador (2026-11-16): "preço no TCGplayer não é referência de venda,
eBay vende muito barato". A margem bruta usa o que compradores do eBay US
EFETIVAMENTE pagaram pelo produto em INGLÊS (mediana de 90 dias) — não o
TCGplayer Market (agora só coluna informativa) e não só a pedida (menor BIN
ativo, que vira fallback MARCADO que nunca sustenta GREEN:
`sealed_arbitrage_scanner.apply_sold_reference_guards`).

Fonte: store de vendidos `data/history/ebay_sold.jsonl`, preenchido pela página
de vendidos do eBay coletada no Chrome logado (`build_sold_reference.py
--collect`); só `match_status == "ok"`. Porte de ptbr-sealed-scanner
(lib/sold_reference.py) sem o contador "X sold" de anúncio ativo: o
build_ebay_reference.py daqui não coleta esse contador.

Regras:
  - janela `sold_reference.window_days` (default 90), fronteira inclusiva;
  - venda sem preço (> 0) NUNCA entra; lote só entra normalizado por unidade
    quando `units_in_lot` é conhecido (lote sem contagem fica fora);
  - preço = MEDIANA; `n`, `usd_min`/`usd_max`, `last_sold` e a URL da venda
    mais recente (o link clicável da entrega) acompanham;
  - só venda nos EUA conta (`require_us`, default True — mesmo escopo do menor
    BIN, item nos EUA); SKU só com venda fora dos EUA fica SEM venda;
  - `thin` = n < `min_sales_for_green` (default 2) — GREEN só com amostra >= 2;
  - SKU sem nenhuma venda = ausente do arquivo (nunca 0, nunca preço inventado).

Saída canônica: `data/sold_reference.json` (gitignored). `captured_at` é a hora
do build — o freshness guard é o mesmo das outras referências.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median

from lib.analysis import history_store as store

# Uma regra só de frescor da referência (scanner, builder e orquestrador). O config
# deste repo fixa `deal_criteria.max_reference_age_days: 1`; este é o default quando
# a chave falta (config de teste/alternativo).
DEFAULT_MAX_REFERENCE_AGE_DAYS = 14

# País da venda/anúncio a partir do texto de localização do eBay. Vive aqui (não no
# builder) porque o store do operador guarda `Local` VERBATIM ("Estados Unidos",
# "from Brazil") e a agregação precisa normalizar do mesmo jeito que a página de
# vendidos — senão toda venda americana vira "fora dos EUA" (review 2026-10-19).
_COUNTRY_NAMES = {
    "united states": "US", "estados unidos": "US", "usa": "US", "eua": "US",
    "brazil": "BR", "brasil": "BR",
    "portugal": "PT",
    "germany": "DE", "alemanha": "DE",
    "united kingdom": "UK", "reino unido": "UK", "great britain": "UK",
    "canada": "CA", "canadá": "CA",
    "australia": "AU", "austrália": "AU",
    "japan": "JP", "japão": "JP",
    "france": "FR", "frança": "FR",
    "italy": "IT", "itália": "IT",
    "spain": "ES", "espanha": "ES",
    "netherlands": "NL", "holanda": "NL", "países baixos": "NL",
    "mexico": "MX", "méxico": "MX",
    "argentina": "AR",
    "china": "CN", "hong kong": "HK", "korea": "KR", "coreia do sul": "KR",
}
_LOC_PREFIX_RE = re.compile(r"^(?:from|located in|de|em|localizado em)\s+", re.I)
_ISO2_RE = re.compile(r"^[A-Za-z]{2}$")


def country_from_location(text: str | None) -> str:
    """'from Brazil' -> BR; 'Estados Unidos' -> US; código ISO-2 já normalizado
    ('BR', 'us', '??') passa direto; vazio = sem rótulo = doméstico (eBay US mostra o
    país só quando NÃO é EUA); nome desconhecido -> '??' (nunca chutar US)."""
    t = (text or "").strip()
    if not t:
        return "US"
    if t == "??" or _ISO2_RE.match(t):
        return t.upper()
    t = _LOC_PREFIX_RE.sub("", t).strip().lower().rstrip(".")
    return _COUNTRY_NAMES.get(t, "??")

SOURCE_STORE = "store"

DEFAULTS = {
    "window_days": 90,
    "min_sales_for_green": 2,
    "fallback_to_active_bin": True,
    # True: venda fora dos EUA / local desconhecido NÃO entra na mediana; SKU só com
    # essas fica SEM venda (`skipped_non_us_only`). False = fallback `non_us_fallback`.
    "require_us": True,
    "file": "data/sold_reference.json",
}


def sold_ref_cfg(config: dict | None) -> dict:
    """Sub-bloco `sold_reference` do config com defaults (aceita o config inteiro
    ou só o sub-dict)."""
    c = dict(DEFAULTS)
    if isinstance(config, dict):
        sub = config.get("sold_reference") if "sold_reference" in config else config
        for k, v in (sub or {}).items():
            if v is not None:
                c[k] = v
    return c


def _as_date(x) -> date:
    if x is None:
        return datetime.utcnow().date()
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def _parse_date(s) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _price(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _item_url(item_id: str | None, url: str | None) -> str:
    """URL de item vem SEMPRE da fonte (coluna `url`/`link`); nunca montada."""
    return (url or "").strip()


_DIGITS_RE = re.compile(r"\d{6,}")


def canonical_item_id(item_id) -> str:
    """'v1|257292810881|0' (Browse API) e '257292810881' (página/CSV) são o MESMO
    item — a chave de dedupe entre fontes é o número do anúncio."""
    s = str(item_id or "")
    m = _DIGITS_RE.search(s)
    return m.group(0) if m else s


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ── 1. vendas do store (CSV do operador / página de vendidos) ────────────────
def realized_sales_from_store(records: list[dict], now, window_days: int = 90) -> list[dict]:
    """Converte registros do store em pontos de venda realizada por unidade.

    Só `match_status == "ok"` com `sku_id`, `sold_date` dentro da janela
    (inclusiva) e preço > 0. Lote: `units_in_lot` conhecido -> preço/unidade e
    `from_lot: True`; lote sem contagem -> fora (não normalizável).
    O MESMO anúncio visto em duas fontes (CSV do operador + página de vendidos, ou
    dois CSVs) é UMA venda: dedupe por número do anúncio (review 2026-10-18 — sem
    isso uma venda só virava n=2 e liberava GREEN)."""
    today = _as_date(now)
    out: list[dict] = []
    seen: set[str] = set()
    for rec in records:
        if rec.get("match_status", "ok") != "ok" or not rec.get("sku_id"):
            continue
        key = canonical_item_id(rec.get("item_id"))
        if key and key in seen:
            continue
        d = _parse_date(rec.get("sold_date"))
        if d is None or not (0 <= (today - d).days <= int(window_days)):
            continue
        usd = _price(rec.get("price_usd"))
        if usd is None:
            continue
        units = None
        from_lot = False
        if rec.get("is_lot"):
            try:
                units = int(rec.get("units_in_lot") or 0)
            except (TypeError, ValueError):
                units = 0
            if units <= 1:
                continue
            usd = round(usd / units, 2)
            from_lot = True
        if key:
            seen.add(key)
        out.append({
            "sku_id": rec["sku_id"],
            "usd": usd,
            "location": country_from_location(rec.get("location")),
            "sold_date": d.isoformat(),
            "url": _item_url(rec.get("item_id"), rec.get("link") or rec.get("url")),
            "source": SOURCE_STORE,
            "source_type": rec.get("source_type") or "",
            "from_lot": from_lot,
            "units": units if from_lot else None,
            "bids": rec.get("bids"),
            "item_id": key or str(rec.get("item_id") or ""),
        })
    return out


# ── 3. agregação ───────────────────────────────────────────────────────────
def aggregate_sold_reference(sales: list[dict], now, config: dict | None) -> dict:
    """{captured_at, window_days, min_sales_for_green, entries: {sku: {...}}}.

    Por SKU: só vendas US (`require_us`, default) — as demais são contadas em
    `n_non_us_dropped`; SKU só com venda fora dos EUA fica fora (`skipped_non_us_only`).
    Legado (`require_us: false`): sem venda US usa as demais (fallback marcado).
    `usd` = mediana (2 casas), `n`, min/max, `last_sold` (maior data conhecida),
    `url` = a VENDA datada mais recente COM link (contador de anúncio ativo só
    quando não há venda datada — o [eBay] da entrega é a venda, não a pedida),
    `sources` = contagem por origem, `thin` = n < min_sales_for_green.
    SKU cujas vendas não têm NENHUM link fica fora (`skipped_no_url`): a entrega
    exige o link e não inventamos URL."""
    c = sold_ref_cfg(config)
    today = _as_date(now)
    min_n = int(c["min_sales_for_green"])
    require_us = bool(c.get("require_us", True))
    by_sku: dict[str, list[dict]] = {}
    for s in sales:
        if s.get("sku_id") and _price(s.get("usd")) is not None:
            by_sku.setdefault(s["sku_id"], []).append(s)

    entries: dict[str, dict] = {}
    skipped_no_url: list[str] = []
    skipped_non_us_only: list[str] = []
    for sid in sorted(by_sku):
        pts = by_sku[sid]
        # Aqui `location` já vem normalizado (ISO-2 ou "??") pelas funções de coleta:
        # página de vendidos do eBay US SEM rótulo = doméstico → "US" (convenção do site,
        # `country_from_location`); nome não reconhecido = "??" → fora. Neste ponto,
        # vazio/"??" NÃO é EUA (não inventamos localização).
        us = [p for p in pts if str(p.get("location") or "").upper() == "US"]
        if us:
            used, scope, country = us, "us", "US"
        elif require_us:
            skipped_non_us_only.append(sid)
            continue
        else:
            used, scope = pts, "non_us_fallback"
            # país da venda mais recente usada
            country = _most_recent(used).get("location") or "??"
        recent = _link_anchor(used)
        if recent is None:
            skipped_no_url.append(sid)
            continue
        prices = sorted(float(p["usd"]) for p in used)
        dated = [p["sold_date"] for p in used if p.get("sold_date")]
        sources: dict[str, int] = {}
        for p in used:
            sources[p["source"]] = sources.get(p["source"], 0) + 1
        entries[sid] = {
            "usd": round(float(median(prices)), 2),
            "n": len(used),
            "usd_min": prices[0],
            "usd_max": prices[-1],
            "last_sold": max(dated) if dated else None,
            "url": recent.get("url") or "",
            "status": "ok",
            "location_scope": scope,
            "seller_country": country,
            "thin": len(used) < min_n,
            "sources": sources,
            "n_dated": len(dated),
            "n_non_us_dropped": len(pts) - len(used),
            "from_lot_n": sum(1 for p in used if p.get("from_lot")),
            "sales": [
                {k: p.get(k) for k in ("usd", "sold_date", "location", "source", "url",
                                       "from_lot", "units", "bids", "qty", "item_id")}
                for p in sorted(used, key=lambda p: p.get("sold_date") or "9999", reverse=True)
            ],
        }
    return {
        "captured_at": store.utc_now_iso(),
        "as_of": today.isoformat(),
        "window_days": int(c["window_days"]),
        "min_sales_for_green": min_n,
        "entries": entries,
        "skipped_no_url": skipped_no_url,
        "skipped_non_us_only": skipped_non_us_only,
        "require_us": require_us,
    }


def _most_recent(pts: list[dict]) -> dict:
    """Venda com maior data; sem data (contador de anúncio ativo) conta como a mais
    recente possível — é o estado de hoje."""
    return max(pts, key=lambda p: p.get("sold_date") or "9999-99-99")


def _link_anchor(pts: list[dict]) -> dict | None:
    """Ponto que dá o link da entrega: venda DATADA mais recente com URL; sem venda
    datada com URL, o contador (anúncio ativo) com URL; sem nenhum URL -> None."""
    dated = [p for p in pts if p.get("sold_date") and (p.get("url") or "").strip()]
    if dated:
        return max(dated, key=lambda p: p["sold_date"])
    undated = [p for p in pts if (p.get("url") or "").strip()]
    return undated[0] if undated else None


# ── conveniência p/ o builder ──────────────────────────────────────────────
def load_store_records(path: Path) -> list[dict]:
    recs, _bad = store.read_records(Path(path))
    return recs
