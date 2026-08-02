# Histórico

As mudanças em detalhe estão no `git log`, que é onde elas ficam certas. Aqui fica só o
fio da meada — o que cada fase resolveu e por quê.

## Agosto de 2026 — pronto para outras pessoas

O app deixou de ser de uma pessoa só.

- **Backup diário automático** do banco. Antes não havia cópia nenhuma: um arquivo
  corrompido levaria tudo, inclusive as notas que um MEI guarda o ano inteiro.
- **Tela de erro de verdade**, com um código curto que a pessoa manda de volta. Antes,
  qualquer falha era uma página branca — e o silêncio de quem desiste é lido como
  "não gostou".
- **Saúde do app**: quem voltou nos últimos 7 dias, estado das cópias, últimas quebras.
  Trancada por `ADMIN_EMAIL`; sem a variável a rota dá 404 para todo mundo.
- **Recado da semana** e comparação **mês a mês** — um motivo pra voltar.
- **Ocultar valores** com um toque, com máscara de largura fixa (largura variável
  entregaria a grandeza do número).
- Correções achadas testando o app no perfil de quem ia usar, não no meu: o botão
  "Entrou dinheiro" estava quebrado por uma aspa escapada pelo Jinja, e o modo simples
  escondia o Painel MEI justamente de quem mais precisa dele.

## Julho de 2026 — o dinheiro entra sozinho

- **Open Finance via Pluggy**: saldo e lançamentos direto do banco.
- Importar extrato em **OFX, PDF ou texto colado**, para quem não conecta.
- **Categorização que aprende** com o que você ensina, e conserta o passado.
- **Cartão de crédito** com fatura por ciclo de fechamento e parcelas sem contagem dupla.
- **Painel MEI** (limite anual, DAS, DASN, dossiê) e **prévia do IR**.
- **Dívidas**, **assinaturas detectadas**, **orçamento por categoria**.
- **Bloqueio por digital**, política de privacidade em português normal e exclusão de
  conta que apaga de verdade.
- **`testes.py`**: a bateria que passou a rodar antes de cada deploy.

## Antes — V1 a V5

Login e perfis (PF, MEI, lojista, híbrido), home personalizada, saldo e projeção, notas
fiscais, metas, compromissos, clientes e serviços, painel de negócio, exportação de IR.
CSS próprio, ícones locais em vez de CDN, e a identidade do Herc.
