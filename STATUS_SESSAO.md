# Status da sessão — 08/07/2026 (CONCLUÍDA ✓)

Validação completa do feedback V5. Todos os itens testados e funcionando.

## Correções aplicadas nesta sessão (além do que já estava na V5)

1. **Modal "Tem certeza?" abria sozinho ao carregar a página** — `display:flex` do CSS
   sobrescrevia o atributo `hidden`. Corrigido em `static/styles.css`.
2. **Ícones dependiam de CDN externo** — quando o CDN falha, todos os botões de ícone
   somem (provável causa dos "botões faltando" no mobile). Lucide e Chart.js agora são
   servidos localmente de `static/vendor/`.
3. **secret_key aleatória deslogava todo mundo a cada reinício** — agora persiste em
   `.secret_key` na raiz (`_load_secret_key` em app.py). Verificado: sessão sobrevive
   ao restart.
4. **BUG GRAVE de centavos**: `parse_money` tratava "99.90" como "9990" (assumia
   formato pt-BR e removia pontos), mas inputs `type=number` enviam ponto decimal.
   Qualquer valor com centavos era multiplicado por 100. Corrigido em app.py: vírgula
   presente → formato pt-BR; senão, ponto é decimal. Testado: R$ 12,50 salva como 12,50.
   Dados existentes do usuário rjuan não foram afetados (só usou valores redondos).

## Testes concluídos nesta retomada
- Sidebar "Recolher" em 1280px: colapsa para só ícones (92px), persiste após reload ✓
- Contas: criação, redirecionamento com "✓ salva agora" ✓
- Configurações: mudar nome (reflete no chip do topo) e trocar senha (verificado no BD) ✓
- Console sem erros nem warnings ✓

## Pendências / observações para o futuro
- **Pasta `app/` na raiz é código morto e quebrado** (importa `app.database` e blueprints
  inexistentes). Ela conflita com `app.py` (impede `flask --app app`). Recomendado excluir.
- Dados de teste no `database.db` (usuário `teste.claude@exemplo.com`, senha atual
  `teste456`, nome "Teste Renomeado"; transações "Salário de julho" R$ 3.000 e
  "Quentinha teste centavos" R$ 12,50; meta R$ 1.200; conta Internet R$ 99,90).
  Remover quando quiser o banco limpo:
  `DELETE FROM usuarios WHERE email='teste.claude@exemplo.com'` (cascateia o resto).
- `.claude/launch.json` usa um runner em pasta temporária para rodar na porta 5099
  (a 5000 é do servidor manual do usuário; a pasta `app/` impede `flask --app app`).
  Se a pasta `app/` for excluída, dá para simplificar.
- O servidor do usuário na porta 5000 precisa ser **reiniciado** para pegar as correções
  de app.py (parse_money, secret_key) e o base.html novo (ícones locais).
