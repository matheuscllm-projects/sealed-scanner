# Handoff 2026-11-16 — Veredito: selados Pokémon em inglês (Liga → eBay US) não têm margem

## O que aconteceu

1. Scan da Liga classificado pelo TCGplayer Market deu 28 GREEN.
2. Operador contestou: "preço no tcg player não é referencia de venda, ebay venda muito barato".
3. A classificação do perfil Pokémon passou a usar a **venda realizada no eBay US** (mediana 90 dias, só vendas nos EUA) — PR #93, `references.classification_source: ebay_sold`.
4. Mesmo scan com a régua nova: **7 GREEN / 2 YELLOW / 70 RED** (79 produtos). Snapshot: `snapshots/scan-2026-11-16-0751.md`. Dólar a R$ 5,67.

## Análise das 9 linhas acionáveis (vendas conferidas uma a uma no store)

| Produto | Veredito | Evidência |
|---|---|---|
| Mega Evolution 3-Pack Blister [Golduck] | Sólido | 20 vendas unitárias US, mediana US$ 28,00; 30d US$ 31,47. Só 30 un. na Liga |
| Journey Together 3-Pack Blister [Yanmega] | Sólido | 16 vendas, mediana US$ 24,95; 30d US$ 26,99. Só 47 un. |
| Pitch Black 3-Pack Blister [Binacle] | Sólido | 28 vendas unitárias, mediana US$ 20,00 estável; 17 lotes puxam a referência p/ US$ 18 |
| Chaos Rising 3-Pack Blister [Charmeleon] | Fraco, caindo | 90d US$ 20,00 → 30d US$ 17,99 (margem 45% → 31%); menor ativo US$ 13,98 |
| Black Bolt Tech Sticker | No limite | mediana US$ 45,75, margem 30,2%; menor ativo US$ 32,99 já dá prejuízo |
| Destined Rivals Sleeved Booster Pack | Falso verde | as "2 vendas" são um lote e um produto diferente (checklane blister c/ promo) |
| Lumiose City Mini Tin [Meganium] | Falso verde | 2 vendas: US$ 10,99 e US$ 29,99; mediana é o meio de dois extremos |
| Team Rocket's Mewtwo ex Box | Descartar | 0 vendas; fallback num anúncio de US$ 91,99 vs TCG US$ 51,79 |
| Mega Kangaskhan ex Box | Sem dado | 0 vendas em 90d; só pedida |

## Conclusão (operador + Claude, 2026-11-16)

- **Selado em inglês comprado na Liga para vender no eBay US via Probstein não fecha a conta hoje.** Só 4 de 79 produtos passam de 30% bruto, só o Golduck tem folga (59%), e os dois melhores somam 77 unidades na Liga inteira.
- A margem é bruta: não desconta os 10% da Probstein nem o frete, e leilão costuma fechar abaixo de venda direta.
- Causa: o preço da Liga para produto em inglês já acompanha o mercado americano. O que parecia margem era a distância entre o TCGplayer Market e o que o eBay realmente paga.
- Só blister triplo passa; ETB, booster box, bundle e latas ficaram todos abaixo de 30%.
- **Decisão: pausar compra de selados em inglês, sem desmontar o scanner.** Rodar de novo em lançamento novo ou salto do dólar.

## Câmbio

Dólar mais alto aumenta a margem (compra em real, vende em dólar). No Yanmega: R$ 5,10 → 27%; R$ 5,67 → 42%; R$ 6,23 → 56%. O ganho é temporário: produto em inglês é importado, e as lojas repõem mais caro. A janela é entre a alta do dólar e o reajuste da Liga (tempo de reajuste não medido).

## Limites do veredito

- Foto de um dia; só fonte Liga (OLX/ML não rodaram).
- 149 anúncios de 15 produtos ficaram sem referência (blisters unitários de Chaos Rising, Pitch Black, Perfect Order; Hop's Zacian ex Box; First Partner Illustration Collection; Build & Battle Box) — não avaliados.
- Não vale para selados em **português** (repo `ptbr-sealed-scanner`, outra concorrência).
- Margem líquida não calculada (falta frete por unidade).

## Pendências técnicas

- `sold_reference.min_sales_for_green: 2` é frouxo: os dois falsos verdes passaram por ele. Sugestão: 5 vendas unitárias, lote não conta para o mínimo.
- Do PR #93: match errado fica no store por até 90 dias; gate de título dos vendidos mais frouxo que o da Liga; `snapshot_friendly.py` e a skill `sealed-scan` ainda descrevem o modelo antigo (TCG).

## Próximas frentes discutidas (nenhuma iniciada)

1. Cartas avulsas graduadas (CGC/PSA via Fanatics) — margem vem da nota; aguardar resultado da remessa já enviada.
2. Selados em português — aguardar venda real da remessa-teste.
3. Cartas cruas na COMC — antes, conferir os scanners de cartas contra venda realizada (eles ainda usam TCGplayer como régua).
4. Cartas chinesas × inglesas no eBay — nicho, nunca testado com compra real.
5. Descartado: importar selado dos EUA (imposto come a diferença).
