"""lib/sold_importers.py — gate de título, detecção de lote e store dos VENDIDOS
do eBay (produto em INGLÊS). Porte de ptbr-sealed-scanner/lib/sold_importers.py.

A página de vendidos do eBay (build_sold_reference.py) entrega registros crus;
aqui cada título é casado com UM SKU do registry e gravado no store JSONL
(lib/analysis/history_store.py) com `match_status` — nada é descartado em silêncio.

Regras:
  - idioma: o produto é INGLÊS por AUSÊNCIA de marcador de outro idioma. Título
    com marcador (japanese/japonês, korean, chinese, portuguese/PT-BR, spanish,
    german, french, italian, thai, indonesian...) é rejeitado, inteiro OU cortado
    (o título do eBay às vezes vem truncado: "Japane");
  - graded (PSA/CGC/BGS...), digital (code card), aberto/usado: rejeitados;
  - set any · type any · requires all · exclude none (os exclude_terms do registry
    já listam os outros idiomas e os tipos concorrentes);
  - lote ("lot of", "4x", "x3", "bundle of", "set of", "case", "lote"): a linha FICA
    (is_lot=True, units_in_lot best-effort) e casa SKU; a referência só a normaliza
    por unidade quando a contagem é conhecida;
  - 0 SKUs -> no_match; 2+ -> ambiguous; 1 -> ok. Todos entram no store.
  - dedupe por (item_id, source_file): re-coletar é no-op.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from lib.analysis import history_store as store

# Marcadores de OUTRO idioma (whole-word, sem acento). Ausência de marcador = inglês.
NON_EN_LANGUAGE_TERMS = (
    "japanese", "japan", "jpn", "japones", "japonesa",
    "korean", "korea", "coreano", "coreana",
    "chinese", "chines", "chinesa", "simplified chinese", "traditional chinese",
    "portuguese", "portugues", "portuguesa", "brazilian", "pt br", "ptbr", "portugese",
    "spanish", "espanhol", "espanola", "espanol",
    "german", "deutsch", "alemao",
    "french", "francais", "frances",
    "italian", "italiano",
    "thai", "indonesian", "bahasa",
    "dutch", "polish", "russian", "turkish", "vietnamese",
)
# Prefixo truncado precisa de pelo menos isto p/ contar como marcador ("Japane").
MIN_TRUNCATED_PREFIX = 4

# Graded / digital: nunca produto selado físico comprável.
GRADED_TERMS = ("psa", "bgs", "cgc", "sgc", "graded", "ace 10")
DIGITAL_TERMS = ("code card", "code cards", "online code", "e delivery", "email delivery",
                 "ptcgo", "ptcgl", "tcg live", "tcg online", "no physical", "in game")

# Aberto/usado em EN. Whole-word.
USED_TOKENS_EN = ("opened", "open box", "empty box", "box only", "no packs", "damaged")

# ── lote ──────────────────────────────────────────────────────────────────
_NUM_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
              "nine": 9, "ten": 10}
_NUM = r"(\d{1,3}|" + "|".join(_NUM_WORDS) + r")"
_UNIT = r"(?:booster\s+)?(?:blisters?|box(?:es)?|etbs?|decks?|tins?|bundles?|displays?|collections?)"
# Padrões COM quantidade (ordem = prioridade). Aplicados no título minúsculo cru.
_LOT_QTY_PATTERNS = (
    re.compile(r"\blot of\s*\(?" + _NUM + r"\)?\b"),
    re.compile(r"\bbundle of\s*" + _NUM + r"\b"),
    re.compile(r"\bset of\s*" + _NUM + r"\b"),
    re.compile(r"\b" + _NUM + r"\s*x\b"),                    # "4x", "10 x"
    re.compile(r"\bx\s*(\d{1,3})\b"),                        # "x3"
    re.compile(r"\(\s*x?\s*([2-9]|10)\s*x?\s*\)"),           # "(2)", "(x2)"
    re.compile(r"^\(?(\d{1,3})\)?\s+pok"),                   # "4 Pokémon TCG ... Booster Packs"
    re.compile(r"\b(\d{1,3})\s*-?\s*(?:packs?|boxes|blisters?|etbs?|tins?|bundles?|decks?)\s+lot\b"),
    re.compile(r"\b" + _NUM + r"\s+" + _UNIT + r"\s+(?:set|lot)\b"),
    # "3 Booster Boxes", "2 ETBs", "four tins": plural (ou sigla ETB/etbs) com contagem >= 2
    re.compile(r"\b" + _NUM + r"\s+(?:booster\s+)?(?:boxes|etbs|tins|blisters|bundles|decks)\b"),
)
# "pair of" = 2.
_PAIR_RE = re.compile(r"\bpair of\b")
# "N packs" (plural) pode ser LOTE de packs ou DESCRIÇÃO do produto ("Booster Box 36 Packs",
# "ETB 9 packs"). Contagens nominais conhecidas = descrição; as demais = dúvida -> lote sem
# contagem (a venda fica FORA da mediana — nunca chute).
_PACKS_PLURAL_RE = re.compile(r"\b(\d{1,3})\s+packs\b")
_NOMINAL_PACK_COUNTS = frozenset({3, 4, 5, 6, 9, 10, 12, 18, 24, 36})
# Número do set, nunca quantidade ("Scarlet & Violet 151"): removido antes de ler lote.
_SET_NUMBER_RE = re.compile(r"\b151\b")
# Tokens de lote SEM quantidade. NB: "bundle" sozinho é produto (Booster Bundle);
# "3 pack blister" é produto. Só os tokens explícitos contam.
_LOT_TOKEN_RE = re.compile(r"\blot\b|\blote\b|\bcase\b|\bbundle of\b|\bset of\b")


# ── normalização: UMA semântica de matching na frota — a do scanner (whole-word,
# sem acento). Import duro de propósito (revisão v2): um fallback local silencioso
# esconderia o scanner quebrado e deixaria as duas cópias divergirem.
from sealed_arbitrage_scanner import contains_term, normalize  # noqa: E402


def non_en_marker(title: str) -> str:
    """'full' | 'truncated' | 'none' — evidência de OUTRO idioma (não-inglês) no título.

    `truncated`: o título do eBay vem cortado (~60-72 chars); se o ÚLTIMO token
    normalizado tem >= MIN_TRUNCATED_PREFIX chars e é prefixo próprio de um marcador
    ("japane" -> "japanese"), conta como marcador cortado. 'none' = inglês."""
    norm = normalize(title)
    if not norm:
        return "none"
    terms = [normalize(t) for t in NON_EN_LANGUAGE_TERMS]
    if any(contains_term(norm, t) for t in terms):
        return "full"
    last = norm.split()[-1]
    if len(last) >= MIN_TRUNCATED_PREFIX:
        for t in terms:
            if t.startswith(last) and t != last:
                return "truncated"
    return "none"


def _as_units(tok: str) -> int | None:
    tok = tok.strip().lower()
    return _NUM_WORDS.get(tok) or (int(tok) if tok.isdigit() else None)


def detect_lot(title: str) -> tuple[bool, int | None]:
    """(is_lot, units_in_lot). units None = lote SEM contagem legível (fica fora da
    mediana). Na dúvida entre lote e descrição do produto ("2 packs"), devolve
    (True, None): a venda é descartada, nunca chutada."""
    low = _SET_NUMBER_RE.sub(" ", (title or "").lower())
    if not low.strip():
        return False, None
    for pat in _LOT_QTY_PATTERNS:
        m = pat.search(low)
        if m:
            units = _as_units(m.group(1))
            if units is None or units <= 1 or units > 100:
                continue
            return True, units
    if _PAIR_RE.search(low):
        return True, 2
    m = _PACKS_PLURAL_RE.search(low)
    if m and int(m.group(1)) not in _NOMINAL_PACK_COUNTS and int(m.group(1)) > 1:
        return True, None
    if _LOT_TOKEN_RE.search(low):
        return True, None
    return False, None


# ── match ─────────────────────────────────────────────────────────────────
def _sku_terms(sku) -> dict:
    """Extrai id + termos de um SKU (dataclass/objeto com atributos OU dict cru
    do YAML com `match`)."""
    if isinstance(sku, dict):
        m = sku.get("match") or {}

        def get(k, d=None):
            return sku.get("id") if k == "id" else m.get(k, d)
    else:
        def get(k, d=None):
            return getattr(sku, k, d)
    return {
        "id": get("id"),
        "set_terms": list(get("set_terms", []) or []),
        "type_terms": list(get("type_terms", []) or []),
        "requires_terms": list(get("requires_terms", []) or []),
        "exclude_terms": list(get("exclude_terms", []) or []),
    }


def sold_title_passes_gate(title: str, sku) -> bool:
    """Gate de título p/ VENDIDOS EM INGLÊS: sem marcador de outro idioma (ausência
    = inglês) · sem graded/digital/usado · set any · type any · requires all ·
    exclude none. Lote NÃO veta (é marcado em is_lot)."""
    if not title:
        return False
    t = _sku_terms(sku)
    norm = normalize(title)
    if non_en_marker(title) != "none":
        return False
    if any(contains_term(norm, u) for u in GRADED_TERMS + DIGITAL_TERMS + USED_TOKENS_EN):
        return False
    if not any(contains_term(norm, x) for x in t["set_terms"]):
        return False
    if not any(contains_term(norm, x) for x in t["type_terms"]):
        return False
    if t["requires_terms"] and not all(
        contains_term(norm, x) or _truncated_prefix_of(norm, x) for x in t["requires_terms"]
    ):
        return False
    # Lado de VENDIDOS: lote NÃO veta (é marcado em is_lot e normalizado por unidade
    # na referência de venda) — os tokens de lote herdados do lado da Liga ficam neutros.
    if any(contains_term(norm, x) for x in t["exclude_terms"] if x not in LOT_NEUTRAL_EXCLUDES):
        return False
    return True


# exclude_terms do registry que falam de LOTE (lado da Liga: "Kit com N" fora de
# escopo). No gate de VENDIDOS são neutros: a venda de "18 Booster Pack Lot" é
# evidência de preço por unidade (lib/sold_reference normaliza por units_in_lot).
LOT_NEUTRAL_EXCLUDES = frozenset({"lot", "lote", "kit com", "combo"})


def _truncated_prefix_of(norm: str, term: str) -> bool:
    """Título do eBay vem cortado (~60-72 chars): "... 3 Pack Blister w/ LEAFEO".
    Aceita o ÚLTIMO token normalizado (>= MIN_TRUNCATED_PREFIX chars) como prefixo
    próprio do termo exigido — só no fim do título (mesmo critério do idioma)."""
    toks = norm.split()
    if not toks:
        return False
    last = toks[-1]
    tt = normalize(term).split()
    if not tt or len(last) < MIN_TRUNCATED_PREFIX:
        return False
    # Termo multi-palavra ("mega gardevoir"): o corte só vale no ÚLTIMO token do termo,
    # com os anteriores presentes, na ordem, logo antes ("… Mega Gardev" casa; "… Mega"
    # NÃO — senão um título cortado em "Mega" casaria todo SKU "mega <x>"; review 2026-10-19).
    head, tail = tt[:-1], tt[-1]
    if not (tail.startswith(last) and tail != last):
        return False
    return not head or toks[-1 - len(head):-1] == head


def match_records_to_skus(records: list[dict], skus: list) -> list[dict]:
    """Adiciona `sku_id` (str|None), `match_status` (ok|ambiguous|no_match) e
    `candidates` (ids) a cada registro. Nenhum registro é removido."""
    for rec in records:
        title = rec.get("title") or ""
        cands = [_sku_terms(s)["id"] for s in skus if sold_title_passes_gate(title, s)]
        if len(cands) == 1:
            rec["sku_id"], rec["match_status"] = cands[0], "ok"
        elif not cands:
            rec["sku_id"], rec["match_status"] = None, "no_match"
        else:
            rec["sku_id"], rec["match_status"] = None, "ambiguous"
        rec["candidates"] = cands
    return records


# ── store ─────────────────────────────────────────────────────────────────
@dataclass
class ImportStats:
    total: int = 0
    imported: int = 0
    duplicates: int = 0
    ok: int = 0
    ambiguous: int = 0
    no_match: int = 0
    lots: int = 0
    details: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.imported}/{self.total} linhas importadas · {self.duplicates} duplicadas · "
                f"{self.ok} ok · {self.ambiguous} ambíguas · {self.no_match} sem match · "
                f"{self.lots} lotes")


def append_to_store(records: list[dict], store_path: str | Path, source_type: str,
                    source_file: str, dry_run: bool = False) -> ImportStats:
    """Grava os registros no JSONL (append) sem duplicar (item_id, source_file).

    Contagens ok/ambiguous/no_match/lots são do ARQUIVO (todos os
    registros recebidos), não só do que foi gravado. `dry_run=True` calcula
    tudo e não escreve nada."""
    store_path = Path(store_path)
    stats = ImportStats(total=len(records))
    existing, _bad = store.read_records(store_path)
    keys = {(r.get("item_id"), r.get("source_file")) for r in existing}
    collected_at = store.utc_now_iso()
    to_write: list[dict] = []
    for rec in records:
        status = rec.get("match_status")
        if status == "ok":
            stats.ok += 1
        elif status == "ambiguous":
            stats.ambiguous += 1
            stats.details.append(f"ambíguo: {rec.get('title', '')[:72]} -> {rec.get('candidates')}")
        else:
            stats.no_match += 1
        if rec.get("is_lot"):
            stats.lots += 1
        key = (rec.get("item_id"), source_file)
        if key in keys:
            stats.duplicates += 1
            continue
        keys.add(key)
        full = dict(rec)
        full.setdefault("sku_id", None)
        full.setdefault("match_status", "no_match")
        full.setdefault("candidates", [])
        full["source_type"] = source_type
        full["source_file"] = source_file
        full["collected_at"] = collected_at
        to_write.append(full)
        stats.imported += 1
    if not dry_run:
        store.append_records(store_path, to_write)
    return stats
