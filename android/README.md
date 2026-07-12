# Hércules (app Android)

Não é um "companion" separado — é o próprio Hércules dentro de um app, com a
captura automática de notificações rodando junto. Um ícone só, uma instalação só.

## Como funciona

1. **Tela principal = o site de verdade.** `MainActivity` mostra o Hércules
   dentro de um WebView. Login por e-mail/senha funciona direto, sem nada
   especial. Assim que a home carrega, o app pega o `capture_token` da mesma
   sessão que acabou de logar (`GET /api/meu-token`, autenticado pelo cookie).

2. **"Entrar com Google" é o único caso especial.** O Google bloqueia login
   OAuth dentro de WebView comum, então esse botão é interceptado e abre uma
   aba do Chrome (Custom Tabs) só para essa etapa. Ao terminar, o site manda
   o Chrome de volta para `https://.../app/entrou?token=...&code=...` — um
   **Android App Link verificado** (`assetlinks.json`) entrega essa URL direto
   pro app (não abre outro app, não aparece nada na lista de apps do celular).
   O app pega o token da URL e usa o `code` (uso único, 2 minutos) para o
   WebView virar uma sessão logada de verdade, sem a pessoa digitar nada.

3. **Captura automática.** `NotificationCaptureService` filtra notificações
   pelos pacotes de bancos conhecidos (`BANK_PACKAGES`) e envia para
   `/api/captura`. Ativa com um toque no botão flutuante "🔔 Ativar captura
   automática", que só aparece enquanto não estiver ligado.

## Build

Sem Android Studio: o workflow `.github/workflows/android-companion.yml`
compila o APK debug a cada push em `android/**` e publica como artifact.
Baixe em Actions → último run → Artifacts → `herc-companion-debug`.

Para instalar no celular: baixe o `.apk`, transfira para o telefone e abra
(o Android vai pedir para permitir "instalar de fontes desconhecidas" —
autorize só para esse arquivo).

## Peças do App Link (não mexer sem entender as duas juntas)

- `AndroidManifest.xml`: intent-filter com `autoVerify="true"` para
  `https://rjuan98.pythonanywhere.com/app/entrou`.
- `app.py` → rota `/.well-known/assetlinks.json`: precisa do SHA-256 da
  assinatura do APK (constante `ANDROID_APP_FINGERPRINT`). Como o keystore de
  debug fica fixo em cache no CI, esse valor só muda se o keystore for
  regenerado — o passo "Print debug keystore SHA-256 fingerprint" do workflow
  mostra o valor atual.

## Adicionar mais bancos

Edite `BANK_PACKAGES` em `NotificationCaptureService.kt` com o `applicationId`
do app do banco (dá para descobrir na URL da Play Store).
