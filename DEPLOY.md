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
| `SECURE_COOKIES`| `1` força cookie apenas-HTTPS                           | auto no Render       |
| `HOST` / `PORT` | bind do servidor de desenvolvimento (`python app.py`)   | `0.0.0.0` / `5000`   |
| `FLASK_DEBUG`   | `0` desliga o debug no `python app.py`                  | `1` (dev)            |

## Teste rápido no celular sem deploy (mesma rede Wi-Fi)

```
.venv\Scripts\python.exe app.py
```
No celular: `http://IP-DO-SEU-PC:5000` (veja o IP com `ipconfig`, campo IPv4).
