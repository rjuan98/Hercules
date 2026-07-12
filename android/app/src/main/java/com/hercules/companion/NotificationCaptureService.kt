package com.hercules.companion

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

/** Lê as notificações dos apps de banco e manda o texto para o Hércules anotar sozinho.
 *  Só olha os pacotes desta lista — o resto do celular é ignorado. */
class NotificationCaptureService : NotificationListenerService() {

    companion object {
        val BANK_PACKAGES = setOf(
            "com.nu.production",           // Nubank
            "com.itau",                    // Itaú
            "com.itau.investimentos",
            "br.com.intermedium",          // Banco Inter
            "com.c6bank.app",              // C6 Bank
            "com.bradesco",                // Bradesco
            "br.com.bradesco.next",        // Next
            "br.com.bb.android",           // Banco do Brasil
            "com.caixa.gov.meucaixa",      // Caixa
            "br.com.picpay",                // PicPay
            "com.original.original",        // Banco Original
            "com.neon.neon",                // Neon
            "com.btg.wl.pactualwl",         // BTG+
            "com.santander.app",            // Santander
        )
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        // Aceita bancos conhecidos OU a notificação de autoteste do próprio
        // app (botão "Testar captura") — ajuda a descobrir se o problema é o
        // pacote do banco ou o próprio recebimento de notificações no aparelho.
        val isBank = sbn.packageName in BANK_PACKAGES
        if (sbn.packageName != packageName && !isBank) return

        val extras = sbn.notification.extras
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        val text = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString().orEmpty()
        val bigText = extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString().orEmpty()
        val corpo = listOf(title, text, bigText).filter { it.isNotBlank() }.distinct().joinToString(" - ")

        val token = Prefs.getToken(applicationContext) ?: return

        if (corpo.isBlank()) {
            // Os campos de texto padrão vieram vazios — em vez de descartar em
            // silêncio (o que estava acontecendo até agora), manda um raio-x de
            // tudo que a notificação carrega para aparecer nas "pendências" do
            // site. Isso revela se o banco usa um formato de notificação diferente.
            if (isBank) {
                val dump = extras.keySet().joinToString(" | ") { key -> "$key=${extras.get(key)}" }
                ApiClient.sendCapture(token, "[DIAGNOSTICO ${sbn.packageName}] $dump".take(480))
            }
            return
        }

        ApiClient.sendCapture(token, corpo)
    }
}
