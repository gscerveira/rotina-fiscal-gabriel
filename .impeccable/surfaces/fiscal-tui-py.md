---
version: 1
slug: "fiscal-tui-py"
primary_target: "fiscal/tui.py"
related_targets: []
---

# Surface: TUI principal (fiscal/tui.py)

Modo: Operate. Terminal, uso mensal, um usuário. Tarefas: emitir nota quando quiser (mês em aberto); fechar o período encerrado etapa por etapa com confirmação.

## Direction contract

THESIS: o tempo organiza a tela. Em cima, o mês em aberto com suas notas; embaixo, o período encerrado como agenda de vencimentos. Recusa o arranjo padrão de TUI (painéis com borda, lista à esquerda, detalhe à direita).

OWN-WORLD: agenda tributária. Fundo azul-tinta quase preto, texto cor de papel, cinza-azulado para o que espera. Um único âmbar, reservado à próxima decisão. Situações são carimbos em caixa alta com data (verde-selo concluído, vermelho bloqueado). Sem bordas de painel: só fios horizontais e a régua do mês. Colunas fixas; números alinhados à direita.

STORY: o usuário vê o que vence e quando, enxerga a única linha acesa, entra, lê o resumo completo e confirma. Nada some: cada etapa concluída fica carimbada.

FIRST VIEWPORT: linha de título; bloco "MÊS · em aberto" com notas e total, [n] nova nota à direita; bloco "MÊS · encerrado" com dias restantes à direita, régua 01–31 com hoje, 15 e 20 marcados; tabela VENCE / OBRIGAÇÃO / VALOR / SITUAÇÃO com ▸ âmbar na próxima; rodapé de teclas com a ação primária primeiro.

RAISES: sinalização de aeroporto — só a próxima decisão acende. Painel de partidas — colunas nunca se movem, a situação muda no lugar. Carteira de bilhetes — estados se carimbam, nada desaparece.

FORM: candidato 5 de 7 (agenda de obrigações). Seed 09b3be45. Code-led (sem geração de imagem).

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
