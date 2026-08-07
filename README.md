# Hércules 🦁

Assistente financeiro para quem não gosta de planilha.

A ideia é simples: o app responde as duas perguntas que importam — **quanto eu tenho** e
**quanto dá pra gastar hoje** — sem exigir que você vire contador do próprio dinheiro.

Os lançamentos entram do jeito que der: digitados, por foto do comprovante, pelo extrato
em OFX ou PDF, ou colando o texto que você copiou do app do banco. Ele aprende o que é
cada gasto e não pede a mesma coisa duas vezes.

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

São **1.284 verificações** contra um banco temporário e isolado — não encostam nos seus
dados. Cobrem as contas de dinheiro, isolamento entre usuários, fuso horário, o ciclo
da fatura, entrada hostil em todo formulário, envio duplicado, exclusão em cascata e
duas abas abertas na mesma conta.

Já pegaram bugs que teriam quebrado a instalação de todo mundo — e outros que só
apareceram porque testadores de verdade cutucaram onde eu não tinha olhado.

Se algo ficar vermelho, não suba.

E antes de subir, vale conferir se alguma dependência tem falha publicada:

```bash
pip install -r requirements-dev.txt
pip-audit -r requirements.txt
```

---

## O que o app faz

**O dinheiro do dia a dia**
- Lançar em segundos, ou trazer o extrato de uma vez: OFX, PDF ou texto colado do app
  do banco — sem lançar nada duas vezes
- Conexão automática com o banco via Open Finance (Pluggy): **construída e testada,
  hoje desligada** — veja *Sobre o Open Finance* abaixo
- Categorização automática que **aprende**: você ensina uma vez que "hops" é Bebidas e
  ele acerta pra sempre, inclusive nos lançamentos antigos
- Cartão de crédito de verdade: fatura por ciclo de fechamento, parcelas sem contar três
  vezes a mesma compra, e quanto das próximas faturas já está comprometido
- Contas a pagar, dívidas (o que você deve e o que te devem), assinaturas detectadas
  sozinhas e orçamento por categoria
- Metas com um valor que **cabe no bolso**: calculado pelo seu pior mês, não pela média
- **"Será que cabe?"** — antes de gastar, o app olha as contas que ainda vencem e o seu
  ritmo de gasto e dá um veredito, não um número: cabe, aperta ou não cabe

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

## Sobre o Open Finance

O código está pronto: widget de conexão, sincronização, saldo real, cartão, parcelas,
investimentos, renovação de consentimento. Chegou a funcionar de verdade — banco
conectado, movimentação real entrando, saldo batendo com o app do banco.

Está desligado (`OPEN_FINANCE_ABERTO = False` no `app.py`) por um motivo que não é
técnico: acesso a dado bancário no Brasil é vendido por assinatura mensal em uma faixa
de preço que um projeto de uma pessoa só não alcança. Enquanto isso, oferecer o botão
seria prometer uma porta que não abre.

Quem tiver as chaves e o acesso a produção liga numa linha. Até lá, o caminho é o extrato
e o registro manual — que é o que o app faz bem, e de graça.

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
app.py                 rotas, regras de negócio, integrações
database.py            schema e migrações (idempotentes, rodam no boot)
backup.py              cópia diária cifrada, via API de backup do SQLite
testes.py              a bateria inteira
requirements-dev.txt   ferramentas de desenvolvimento (pip-audit)
templates/             Jinja2
static/                CSS próprio, service worker, ícones locais
```

Sem CDN: Lucide e Chart.js são servidos localmente. Quando o CDN cai, é a interface
inteira que some.

---

## Segurança

Senha com `scrypt` e sal próprio. Login trava depois de 5 erros. Sessão nova a cada
entrada, cookie HttpOnly + SameSite + Secure, CSRF em todo POST. CSP, anti-clickjacking
e HSTS. Toda consulta filtra por usuário, e há teste provando que uma conta não alcança
dado de outra. Bloqueio por digital ou rosto (WebAuthn), opcional.

O que **não** existe: pentest profissional, auditoria formal, criptografia individual
dos dados em repouso, confirmação de e-mail. A tabela completa está no
**[DEPLOY.md](DEPLOY.md)**.

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

Em uso real, em teste com um grupo pequeno. Não é um produto acabado: é um app que
funciona, com as arestas anotadas.

O que ainda não existe e faz falta: recuperação de senha por e-mail (quem entra com
Google se resolve; quem usa senha e esquece, não), mais de um banco por pessoa, e aviso
antes do consentimento do Open Finance expirar.
