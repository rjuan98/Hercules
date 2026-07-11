# Hércules Captura (companion Android)

App mínimo que substitui o MacroDroid: lê as notificações dos apps de banco
instalados no celular e manda o texto para `/api/captura` no Hércules, que
interpreta e lança o gasto sozinho.

## Como funciona
1. Login com e-mail/senha da conta Hércules (`POST /api/token` → recebe o
   `capture_token`, guardado em SharedPreferences).
2. O usuário ativa o acesso a notificações (`Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS`).
3. `NotificationCaptureService` filtra notificações pelos pacotes de bancos
   conhecidos (`BANK_PACKAGES`) e envia título+texto para `/api/captura`.

## Build
Sem Android Studio: o workflow `.github/workflows/android-companion.yml`
compila o APK debug a cada push em `android/**` e publica como artifact.
Baixe em Actions → último run → Artifacts → `herc-companion-debug`.

Para instalar no celular: baixe o `.apk`, transfira para o telefone e abra
(o Android vai pedir para permitir "instalar de fontes desconhecidas" —
autorize só para esse arquivo).

## Adicionar mais bancos
Edite `BANK_PACKAGES` em `NotificationCaptureService.kt` com o `applicationId`
do app do banco (dá para descobrir na URL da Play Store).
