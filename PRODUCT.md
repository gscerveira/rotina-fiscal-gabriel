# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

<!-- Nota: a superfície real é um terminal (TUI). O schema não tem valor "terminal"; trate como interface de terminal, sem navegador. -->

## Stack

Python 3.14 + uv, Textual (TUI), SQLite da stdlib. Escolha confirmada pelo usuário.

## Users

Um único usuário: o sócio de uma empresa do Simples Nacional que exporta serviços. Ele emite as NFS-e livremente durante o mês no Emissor Nacional e, depois que o período fecha, abre o terminal para que o sistema processe o período encerrado com as notas que existirem.

## Product Purpose

Automatizar a rotina fiscal mensal depois da emissão da nota: apurar o PGDAS-D, gerar o DAS, enviar S-1200, S-1210 e S-1299 ao eSocial, conferir a DCTFWeb, gerar o DARF e arquivar tudo. Sucesso: o mês fechado com poucas confirmações, e a pergunta "o que foi enviado em setembro?" respondida em segundos.

## Positioning

Usa só canais oficiais com certificado e-CNPJ A1 (ADN da NFS-e, web service do eSocial, Integra Contador do Serpro). Sem navegador, sem raspagem.

## Operating Context

- Uso mensal, sessão curta, em terminal Linux.
- A emissão da NFS-e é manual e independente; o processamento usa as notas da competência encerrada (pode haver mais de uma).
- O usuário só emite as notas. Depois que o mês fecha, o programa soma as notas da competência e faz todo o resto (PGDAS-D, DAS, eSocial, DCTFWeb, DARF) em sequência.
- O pró-labore é pago no dia em que a nota é emitida; com mais de uma nota no mês, o programa pergunta a data.

## Capabilities and Constraints

- Human in the loop: uma única confirmação por fechamento, sobre um resumo completo do que será enviado (decisão do usuário em 07/10/2026). A emissão de cada nota tem a sua própria confirmação.
- Serviços do governo com janela fechada ou limite diário (como a consulta de eventos do eSocial, fechada nos dias 1 a 7) não são chamados fora da janela; o programa diz quando reabre.
- Qualquer divergência bloqueia a etapa e mostra campo, valor esperado e valor encontrado. O sistema nunca tenta adivinhar a correção.
- O valor oficial de DAS e DARF é sempre o do órgão; o cálculo local serve só para conferência.
- Pró-labore versionado por vigência; competência sem vigência (01/2027) bloqueia.
- Segredos (senha do certificado, chaves do Serpro) ficam no keyring do sistema, nunca em arquivo ou log.
- Pagamento de guias não é feito pelo sistema.

## Evidence on Hand

Cenário de teste 09/2026: faturamento R$ 13.731,88; pró-labore R$ 1.621,00; INSS R$ 178,31; líquido R$ 1.442,69. O faturamento é dado de teste e nunca vale para outro mês.

## Product Principles

1. O órgão é a fonte da verdade; o sistema confere.
2. Parar é melhor que adivinhar.
3. Toda transmissão deixa rastro: o que foi enviado, o recibo e quem confirmou.
4. O usuário só faz o que só ele pode fazer.
