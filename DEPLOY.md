# Como colocar o Hércules na internet

O código já está preparado: gunicorn no `requirements.txt`, `Procfile`, caminhos de banco e
uploads configuráveis por variável de ambiente, cookies seguros atrás de HTTPS e PWA
instalável no celular. Abaixo, duas opções — a primeira é a recomendada para começar.

---

## Opção 1 (recomendada e grátis): PythonAnywhere

Por quê: plano gratuito sem cartão, o arquivo SQLite **persiste** (não some a cada deploy),
HTTPS automático e o site não "dorme". Perfeito para Flask + SQLite nesta fase.

### Passo a passo

1. Crie uma conta gratuita em https://www.pythonanywhere.com (plano "Beginner").

2. Envie o código. O jeito mais simples é via GitHub:
   - Crie um repositório no GitHub e envie o projeto (o `.gitignore` já protege
     `database.db`, `uploads/` e `.secret_key` de serem versionados).
   - No PythonAnywhere, abra um console **Bash** e rode:
     ```bash
     git clone https://github.com/SEU_USUARIO/hercules.git
     ```
   - (Alternativa sem GitHub: aba "Files" → upload de um .zip → `unzip` no console.)

3. Crie o ambiente virtual no console Bash:
   ```bash
   cd hercules
   python3.10 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

4. Crie o web app:
   - Aba **Web** → "Add a new web app" → **Manual configuration** → Python 3.10.
   - Em **Virtualenv**, informe: `/home/SEU_USUARIO/hercules/.venv`
   - Em **Code → WSGI configuration file**, clique no link e substitua o conteúdo por:
     ```python
     import sys
     sys.path.insert(0, "/home/SEU_USUARIO/hercules")
     from app import app as application
     ```
   - Em **Static files**, adicione: URL `/static/` → Directory
     `/home/SEU_USUARIO/hercules/static`

5. Clique em **Reload** na aba Web. Pronto: `https://SEU_USUARIO.pythonanywhere.com`

6. No celular, abra o endereço no Chrome/Safari → menu → **"Adicionar à tela inicial"**.
   O Hércules instala como app (PWA), abre em tela cheia com o ícone dele.

Para atualizar depois: `git pull` no console Bash + botão Reload.

---

## Opção 2: Render (mais automática, mas o disco persistente é pago)

Atenção: no plano gratuito do Render **não há disco persistente** — o `database.db`
seria apagado a cada deploy. Use só com o plano Starter (~US$ 7/mês) + disco.

1. Suba o projeto para o GitHub.
2. Em https://render.com: New → **Web Service** → conecte o repositório.
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app --bind 0.0.0.0:$PORT` (ou deixe o Procfile agir)
3. Adicione um **Disk** (ex.: 1 GB montado em `/var/data`).
4. Em **Environment**, defina:
   - `DATABASE_PATH` = `/var/data/database.db`
   - `UPLOAD_DIR` = `/var/data/uploads`
   - `SECRET_KEY` = uma string longa aleatória
5. Deploy. O código já detecta o Render e ativa cookies seguros sozinho.

---

## Variáveis de ambiente que o app entende

| Variável        | Para quê                                              | Padrão               |
|-----------------|--------------------------------------------------------|----------------------|
| `SECRET_KEY`    | chave de sessão (senão usa/gera o arquivo `.secret_key`) | arquivo `.secret_key` |
| `DATABASE_PATH` | caminho do SQLite                                       | `./database.db`      |
| `UPLOAD_DIR`    | pasta dos arquivos enviados                             | `./uploads`          |
| `HOST` / `PORT` | bind do servidor de desenvolvimento (`python app.py`)   | `0.0.0.0` / `5000`   |
| `FLASK_DEBUG`   | `0` desliga o debug no `python app.py`                  | `1` (dev)            |
| `ADMIN_EMAIL`   | destranca a tela **Saúde do app** pra esse e-mail        | vazio (tela não existe) |
| `BACKUP_DIR`    | onde ficam as cópias do banco                           | `backups/` ao lado do DB |
| `ERROS_LOG`     | arquivo do log de erros                                 | `./erros.log`        |
| `SQLITE_TIMEOUT`| segundos de espera quando o banco está ocupado           | `20`                 |
| `BACKUP_SENHA`  | **cifra as cópias** — sem ela o backup sai legível       | vazio (texto claro)  |
| `SECURE_COOKIES`| `0` desliga o cookie só-HTTPS (liga sozinho em hospedagem)| auto                |

---

## Cópia de segurança do banco

O app faz uma cópia por dia sozinho, na primeira visita do dia — **sem precisar
configurar nada**. Ficam as 14 mais recentes em `backups/`, compactadas.

Pra fazer na mão (ou numa tarefa agendada):

```
python backup.py
```

⚠️ **As cópias ficam no mesmo disco do servidor.** Isso protege contra migração
ruim, tabela apagada por engano e arquivo corrompido — **não** protege contra
perder o servidor inteiro. De vez em quando baixe uma cópia pro seu computador
em **Saúde do app → Baixar**.

### Cifre a cópia — ela é feita pra viajar

Defina `BACKUP_SENHA` no servidor e as cópias passam a sair cifradas. Isso importa
porque o arquivo **sai daqui**: vai pro seu computador, às vezes pra uma nuvem.
Sem cifra, quem pegar o arquivo lê os dados de todo mundo.

A senha mora no ambiente do servidor, então ela **não** protege contra alguém que
tome o servidor — essa pessoa já teria o banco. O que ela protege é o caminho de
fora, que é onde o arquivo passa a maior parte da vida.

**Guarde essa senha fora do servidor.** Sem ela, nem você abre o backup.

Pra restaurar:

```
python backup.py --restaurar backups/hercules-2026-08-02.db.gz saida.db
```

Ele decifra, descompacta e confere a integridade. Confira os dados e só então
ponha no lugar do `database.db`.

---

## Saúde do app (só pra quem mantém)

Defina `ADMIN_EMAIL` com o seu e-mail de login e a tela `/saude` aparece no menu.
Sem essa variável a rota devolve **404 pra todo mundo** — nem existe. Ela mostra
quantas pessoas voltaram nos últimos 7 dias, o estado das cópias de segurança e
as últimas quebras.

Quando um usuário toma um erro, ele vê um código curto (ex.: `A3F91C`) na tela.
Esse mesmo código está no `erros.log`, junto do traceback completo — é assim que
você acha o erro dele no meio dos outros. O log **não guarda** o que a pessoa
digitou (valor, descrição, senha), só onde quebrou.

## Segurança — o que está ligado

| Proteção | Como funciona |
|---|---|
| Senha | `scrypt` com sal próprio por senha (padrão do Werkzeug 3.x) |
| Força bruta | 5 erros por e-mail ou 20 por IP travam o login por 15 min |
| Senha fraca | mínimo 8 caracteres, recusa as óbvias, o próprio nome e o e-mail |
| Sessão | cookie HttpOnly + SameSite + Secure; sessão nova a cada login |
| CSRF | token em todo POST, trocado no login |
| Cabeçalhos | CSP, `X-Frame-Options: DENY`, `nosniff`, Referrer-Policy, HSTS |
| Isolamento | toda consulta filtra por `user_id`; testado contra acesso cruzado |
| Exclusão | apagar a conta apaga tudo em cascata, sem sobra |
| Backup | cifrado com `BACKUP_SENHA` (scrypt + Fernet), sal novo por arquivo |
| Dependências | `pip-audit -r requirements.txt` avisa de falha publicada |

O que **não** existe: pentest profissional, auditoria formal, criptografia
individual dos dados em repouso. Isso está dito sem rodeio em `/privacidade`,
dentro do app.

## Teste rápido no celular sem deploy (mesma rede Wi-Fi)

```
.venv\Scripts\python.exe app.py
```
No celular: `http://IP-DO-SEU-PC:5000` (veja o IP com `ipconfig`, campo IPv4).
