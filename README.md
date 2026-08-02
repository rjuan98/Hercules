# Hércules 🦁

Assistente financeiro para quem não gosta de planilha.

A ideia é simples: você não deveria precisar anotar cada gasto pra saber quanto pode
gastar. O Hércules puxa os lançamentos do seu banco pelo Open Finance, aprende o que é
cada gasto, e responde as duas perguntas que importam — **quanto eu tenho** e **quanto dá
pra gastar hoje**.

Feito em português do Brasil, para o Brasil: MEI, DAS, nota fiscal, DASN, prévia do IR.

---

## Como rodar

```bash
python -m venv .venv
.venv\Scripts\activate       # Windows
source .venv/bin/activate    # Linux/Mac

pip install -r requirements.txt
python app.py
```

Abra `http://127.0.0.1:5000`. Sem nenhuma variável de ambiente ele já funciona — a
conexão com o banco, o login com Google e a leitura de nota por IA ficam desligados até
você configurar as chaves.

## Antes de qualquer deploy, rode os testes

```bash
python testes.py
```

São **565 verificações** contra um banco temporário e isolado — não encostam nos seus
dados. Cobrem desde as contas de dinheiro até isolamento entre usuários, fuso horário,
concorrência e o ciclo da fatura, e já pegaram bugs que teriam quebrado a instalação de
todo mundo.

Se algo ficar vermelho, não suba.

---

## O que o app faz

**O dinheiro do dia a dia**
- Conexão com o banco via Open Finance (Pluggy) — os gastos entram sozinhos
- Importar extrato em OFX, PDF ou texto colado, para quem não quer conectar
- Categorização automática que **aprende**: você ensina uma vez que "hops" é Bebidas e
  ele acerta pra sempre, inclusive nos lançamentos antigos
- Cartão de crédito de verdade: fatura por ciclo de fechamento, parcelas sem contar três
  vezes a mesma compra, e quanto das próximas faturas já está comprometido
- Contas a pagar, dívidas (o que você deve e o que te devem), assinaturas detectadas
  sozinhas e orçamento por categoria
- Metas com um valor que **cabe no bolso**: calculado pelo seu pior mês, não pela média

**Os fechamentos**
- Recado da semana: o que mudou, não o que você já sabe
- Comparação mês a mês, ordenada pela maior mudança
- Prévia do IR com saúde e educação dedutíveis (estimativa — não substitui contador)

**Para quem tem MEI**
- Notas guardadas com anexo, alimentando o faturamento do ano
- Painel MEI: limite anual com aviso antes de estourar, DAS e DASN
- Dossiê do ano num arquivo só, pronto pro contador

**A casa em ordem**
- Backup diário do banco, automático, sem configurar nada
- Bloqueio por digital/rosto (WebAuthn)
- Modo simples, para quem quer só o essencial na tela
- Ocultar valores com um toque
- PWA: instala na tela inicial e funciona como app

---

## Configuração

Nada abaixo é obrigatório para rodar local. Cada chave liga uma funcionalidade.

| Variável | Liga o quê | Padrão |
|---|---|---|
| `SECRET_KEY` | chave de sessão | gera e salva em `.secret_key` |
| `DATABASE_PATH` | onde fica o SQLite | `./database.db` |
| `UPLOAD_DIR` | anexos das notas | `./uploads` |
| `BACKUP_DIR` | cópias do banco | `backups/` ao lado do DB |
| `BACKUP_SENHA` | cifra as cópias (elas saem do servidor) | vazio — sai legível |
| `PLUGGY_CLIENT_ID` / `PLUGGY_CLIENT_SECRET` | conexão com o banco | desligado |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | entrar com o Google | desligado |
| `ANTHROPIC_API_KEY` | ler nota fiscal por foto | desligado |
| `ADMIN_EMAIL` | tela `/saude` (manutenção) | desligado — a rota dá 404 |
| `SECURE_COOKIES` | cookie só por HTTPS | liga sozinho em hospedagem |

Deploy (PythonAnywhere e Render), backup e restauração estão no **[DEPLOY.md](DEPLOY.md)**.

---

## Como está montado

Flask com `sqlite3` puro, sem ORM. Um `app.py` grande de propósito: o projeto é de uma
pessoa só, e um arquivo que dá pra ler inteiro custa menos que uma arquitetura que
precisa ser lembrada.

```
app.py            rotas, regras de negócio, integrações
database.py       schema e migrações (idempotentes, rodam no boot)
backup.py         cópia diária, via API de backup do SQLite
testes.py         a bateria inteira
templates/        Jinja2
static/           CSS próprio, service worker, ícones locais
```

Sem CDN: Lucide e Chart.js são servidos localmente. Quando o CDN cai, é a interface
inteira que some.

---

## Privacidade

Os dados são seus. O app não vende, não compartilha e não usa seus lançamentos para nada
além de te mostrar seus lançamentos.

A senha do banco é digitada **no seu banco**, nunca no Hércules, e o acesso do Open
Finance é **somente leitura** — não dá pra transferir nem pagar nada por aqui.

Dá pra apagar a conta e tudo junto com ela, de verdade, a qualquer momento. Os limites
conhecidos estão escritos sem maquiagem em `/privacidade`, dentro do app.

---

## Status

Em uso real e em teste com um grupo pequeno. Não é um produto acabado: é um app que
funciona, com as arestas anotadas.
